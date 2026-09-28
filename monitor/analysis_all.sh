#!/usr/bin/env bash
# Cadena de ANÁLISIS sobre el ciclo de pronóstico vigente: cruza el gatillo
# climático con la susceptibilidad real de terreno, lo agrega a nivel comunal
# y produce la watchlist de localidades a vigilar.
#
# Es el complemento de `forecast_all.sh`, que solo baja y procesa el
# pronóstico crudo (GFS/ECMWF/ICON). Existen separados porque fallan por
# razones distintas —el pronóstico depende de servidores externos, el
# análisis de los rásters de susceptibilidad en disco— pero deben correr
# ENCADENADOS: un ciclo con pronóstico fresco y watchlist vieja es peor que
# uno viejo entero, porque el timestamp nuevo hace creer que la selección de
# localidades también se actualizó. Ese fue exactamente el modo de falla
# encontrado el 2026-07-18 (pronóstico de 31 h y watchlist de anteayer,
# apuntando a la zona central cuando la señal ya se había corrido al norte).
#
# ACOPLE CONOCIDO: comuna_vulnerability.py y select_watchlist.py tienen la
# ruta ~/nowcast-forecast/latest HARDCODEADA (no aceptan --forecast). Por eso
# este script verifica que el workdir sea ese y aborta si no: correr con otro
# workdir produciría un análisis que mezcla dos ciclos en silencio.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG="${CONFIG:-$HERE/config.env}"
# shellcheck source=config.env
[[ -f "$CONFIG" ]] && source "$CONFIG"
LOCAL_CONFIG="${LOCAL_CONFIG:-$(dirname "$CONFIG")/config.local.env}"
# shellcheck disable=SC1090
[[ -f "$LOCAL_CONFIG" ]] && source "$LOCAL_CONFIG"

FORECAST_WORKDIR="${FORECAST_WORKDIR:-${FORECAST_DIR:-$HOME/nowcast-forecast}}"
WATCHLIST_TOP_N="${WATCHLIST_TOP_N:-20}"
LOG="$FORECAST_WORKDIR/forecast.log"

# LAST_MSG guarda la última línea de log: en un aborto es el MOTIVO, y es lo
# que el aviso lleva (hasta el 26-sep estas cadenas solo escribían al log y 90
# corridas fallidas seguidas pasaron sin que nadie lo supiera).
LAST_MSG=""
log() { LAST_MSG="$*"; echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

NOTIFY_STATE_DIR="$FORECAST_WORKDIR/state"
# shellcheck source=lib_notify.sh
source "$HERE/lib_notify.sh"
# Cualquier salida != 0 avisa, cubra el camino que cubra (incluido el venv
# ausente). Los WARN que solo DEGRADAN el ciclo no pasan por aquí: van al log.
on_exit() {
    local rc=$?
    if (( rc != 0 )); then
        notify "nowcast-forecast: analysis_all FALLÓ" \
            "${LAST_MSG:-salió $rc sin mensaje}. Ver $LOG en $(hostname)." \
            high forecast_fail.hyst 12
    fi
}
trap on_exit EXIT

# Python AISLADO (2026-09-26): la cadena corre en SU venv (venv-forecast, no el
# venv del monitor de los nodos) con versiones fijas
# (requirements-forecast.txt), nunca con el python3 de ~/.local/dist-packages —
# el 7-sep otro proyecto subió numpy a 2.5.3 en ~/.local y el geopandas del
# sistema rompió comuna_impact. PATH antepuesto y no una variable PY porque
# run_localidades.py lanza sus hijos como "python3" por nombre. Sin venv se
# ABORTA: caer al python del sistema es reproducir la falla en silencio.
FORECAST_VENV="${FORECAST_VENV:-$HOME/nowcast-monitor/venv-forecast}"
if [[ ! -x "$FORECAST_VENV/bin/python3" ]]; then
    log "analysis_all: ABORTA — falta el venv $FORECAST_VENV. Crearlo: python3 -m venv $FORECAST_VENV && $FORECAST_VENV/bin/pip install -r $HERE/requirements-forecast.txt"
    exit 1
fi
export PATH="$FORECAST_VENV/bin:$PATH"

if [[ "$(readlink -f "$FORECAST_WORKDIR")" != "$(readlink -f "$HOME/nowcast-forecast")" ]]; then
    log "analysis_all: ABORTA — workdir '$FORECAST_WORKDIR' != ~/nowcast-forecast, y"
    log "analysis_all: comuna_vulnerability/select_watchlist tienen esa ruta hardcodeada"
    exit 1
fi
if [[ ! -d "$FORECAST_WORKDIR/latest" ]]; then
    log "analysis_all: ABORTA — no existe $FORECAST_WORKDIR/latest (¿corrió forecast_all.sh?)"
    exit 1
fi

log "analysis_all: inicio (ciclo=$(basename "$(readlink -f "$FORECAST_WORKDIR/latest")"))"

# 1. Gatillo × susceptibilidad a 30 m, por cuenca y dominio. DURO: sin esto no
#    hay capa de terreno y todo lo de abajo queda sin sustento.
if ! python3 "$HERE/susc_overlay.py" --forecast "$FORECAST_WORKDIR/latest" >>"$LOG" 2>&1; then
    log "analysis_all: susc_overlay falló; análisis abortado (el pronóstico crudo sigue válido)"
    exit 1
fi

# 2. Agregación comunal (peor de los 3 modelos × terreno, con población 2024).
#    DURA: select_watchlist se alimenta de su geojson.
if ! python3 "$HERE/comuna_impact.py" --forecast "$FORECAST_WORKDIR/latest" >>"$LOG" 2>&1; then
    log "analysis_all: comuna_impact falló; análisis abortado"
    exit 1
fi

# 3. Índice de vulnerabilidad social (Censo 2024). BLANDA: es enriquecimiento
#    para el índice compuesto de los mapas de riesgo, no insumo de la
#    watchlist — si falla, el ciclo sigue sirviendo para vigilancia.
if ! python3 "$HERE/comuna_vulnerability.py" >>"$LOG" 2>&1; then
    log "analysis_all: WARN comuna_vulnerability falló; se sigue sin índice social"
fi

# 4. Watchlist de localidades. DURA: es la salida operacional del ciclo.
if ! python3 "$HERE/select_watchlist.py" --top-n "$WATCHLIST_TOP_N" >>"$LOG" 2>&1; then
    log "analysis_all: select_watchlist falló; el ciclo queda SIN watchlist actualizada"
    exit 1
fi

# 5. Productos por localidad (runout ABM + mapa de riesgo) para cada comuna de
#    la watchlist. BLANDA por DOS razones: (a) la watchlist ya es la salida
#    operacional, esto es enriquecimiento; una localidad que falle (sin hazard
#    en el punto, footprint vacío, costa sin modelo) no debe tumbar el ciclo.
#    (b) es CARA: ~30 corridas de ABM. LOCALIDADES_ENABLED=0 la apaga sin tocar
#    el resto; idempotente (salta las que ya son del ciclo vigente, así que
#    re-correr analysis_all no repite el cómputo).
if [[ "${LOCALIDADES_ENABLED:-1}" == "1" ]]; then
    if ! python3 "$HERE/run_localidades.py" --forecast "$FORECAST_WORKDIR/latest" >>"$LOG" 2>&1; then
        log "analysis_all: WARN run_localidades falló globalmente; watchlist y comunas quedan válidas"
    fi
else
    log "analysis_all: run_localidades DESACTIVADA (LOCALIDADES_ENABLED=0)"
fi

# 5b. Clase FAN-APEX por comuna, escrita DENTRO del resumen.json de cada
#     localidad (fenómeno: aluvión de abanico vs crecida fluvial). Va DESPUÉS de
#     la etapa 5 y ANTES de la 6 por una razón dura: run_localidades reescribe
#     cada resumen.json de cero en su corrida, así que un enriquecimiento
#     anterior se perdería; y el registro de la etapa 6 lee esos mismos archivos.
#     BLANDA e idempotente: la morfometría es estática y se cachea entre ciclos
#     (~40 comunas en segundos tras el primer llenado), una comuna que falle
#     queda con disponible=false y su motivo. FANAPEX_ENABLED=0 la apaga: el
#     ciclo queda igual que antes de cablearla, sin chip de fenómeno.
if [[ "${FANAPEX_ENABLED:-1}" == "1" ]]; then
    if ! python3 "$HERE/fanapex_localidades.py" --forecast "$FORECAST_WORKDIR/latest" >>"$LOG" 2>&1; then
        log "analysis_all: WARN fanapex_localidades falló; los resumen.json quedan sin clase fan-apex (el resto del ciclo es válido)"
    fi
else
    log "analysis_all: fanapex_localidades DESACTIVADA (FANAPEX_ENABLED=0)"
fi

# 6. Registro de constancia (HTML+PDF con hash de integridad: "qué sabía el
#    sistema en este ciclo"). BLANDA por las mismas dos razones que la etapa 5:
#    (a) es el informe, no la salida operacional —watchlist/comunas/localidades
#    ya quedaron válidas; un fallo de cartopy/Chrome no debe tumbar el ciclo.
#    (b) es caro (mapas + PDF headless). Idempotente por ciclo (marca .last_cycle
#    en el dir de informes: re-correr analysis_all no reescribe el mismo ciclo).
#    REGISTRO_ENABLED=0 la apaga sin tocar el resto.
if [[ "${REGISTRO_ENABLED:-1}" == "1" ]]; then
    if ! python3 "$HERE/report/build_registro.py" --forecast "$FORECAST_WORKDIR/latest" >>"$LOG" 2>&1; then
        log "analysis_all: WARN build_registro falló; el ciclo (watchlist/comunas/localidades) queda válido, sin informe"
    fi
else
    log "analysis_all: build_registro DESACTIVADO (REGISTRO_ENABLED=0)"
fi

# 7. Retención de ciclos. El disco NO se poda solo: 4 corridas/día × ~9 ciclos
#    de ~1.1 GB llenaron 80 GB en 19 días y dejaron la máquina al 100% (medido
#    2026-08-01). Va al FINAL a propósito: nunca debe poder tocar los productos
#    del ciclo que se acaba de generar.
#
#    PRUNE_MODE gobierna la etapa y su default es DELIBERADO:
#      report (default) — NO borra; solo dice cuánto liberaría. Así el problema
#                         es visible en cada log en vez de acumularse callado,
#                         y activarlo es cambiar una palabra.
#      apply            — poda de verdad (retención 7 d rásters 30 m / 10 d crudo)
#      off              — ni siquiera reporta
#    Borrar datos del usuario en automático es una decisión suya, no un default
#    que herede de un fix: por eso 'report' y no 'apply'.
case "${PRUNE_MODE:-report}" in
    apply)
        if ! python3 "$HERE/prune_forecast.py" --base "$FORECAST_WORKDIR" \
                --dias-30m "${PRUNE_DIAS_30M:-7}" --dias-crudo "${PRUNE_DIAS_CRUDO:-10}" \
                --dias-geojson "${PRUNE_DIAS_GEOJSON:-7}" \
                --apply >>"$LOG" 2>&1; then
            log "analysis_all: WARN prune_forecast falló; el ciclo queda válido, el disco no se podó"
        fi
        ;;
    report)
        python3 "$HERE/prune_forecast.py" --base "$FORECAST_WORKDIR" \
            --dias-30m "${PRUNE_DIAS_30M:-7}" --dias-crudo "${PRUNE_DIAS_CRUDO:-10}" \
            --dias-geojson "${PRUNE_DIAS_GEOJSON:-7}" \
            >>"$LOG" 2>&1 \
            || log "analysis_all: WARN prune_forecast (modo report) falló"
        log "analysis_all: retención en modo REPORT (no borra) — PRUNE_MODE=apply para podar"
        ;;
    *)
        log "analysis_all: retención DESACTIVADA (PRUNE_MODE=${PRUNE_MODE})"
        ;;
esac

mark_ok "$FORECAST_WORKDIR/state"   # cadena completa: lo lee watchdog.sh
log "analysis_all: fin"
