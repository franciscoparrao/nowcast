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

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

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

log "analysis_all: fin"
