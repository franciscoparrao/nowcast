#!/usr/bin/env bash
# Ejecuta el pronóstico determinístico completo: GFS obligatorio, ECMWF/ICON opcionales.
# Pensado para systemd/cron: una falla de fuente secundaria degrada el ciclo,
# pero no borra ni invalida el forecast base.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG="${CONFIG:-$HERE/config.env}"
# shellcheck source=config.env
[[ -f "$CONFIG" ]] && source "$CONFIG"
LOCAL_CONFIG="${LOCAL_CONFIG:-$(dirname "$CONFIG")/config.local.env}"
# shellcheck disable=SC1090
[[ -f "$LOCAL_CONFIG" ]] && source "$LOCAL_CONFIG"

FORECAST_WORKDIR="${FORECAST_WORKDIR:-${FORECAST_DIR:-$HOME/nowcast-forecast}}"
NOWCAST_BIN="${NOWCAST_BIN:-$HOME/nowcast-monitor/bin/nowcast}"
ECMWF_HOURS="${ECMWF_HOURS:-120}"
ICON_HOURS="${ICON_HOURS:-120}"
mkdir -p "$FORECAST_WORKDIR"
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
        notify "nowcast-forecast: forecast_all FALLÓ" \
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
    log "forecast_all: ABORTA — falta el venv $FORECAST_VENV. Crearlo: python3 -m venv $FORECAST_VENV && $FORECAST_VENV/bin/pip install -r $HERE/requirements-forecast.txt"
    exit 1
fi
export PATH="$FORECAST_VENV/bin:$PATH"

log "forecast_all: inicio (workdir=$FORECAST_WORKDIR)"
if ! python3 "$HERE/forecast_gfs.py" --workdir "$FORECAST_WORKDIR" --nowcast-bin "$NOWCAST_BIN" >>"$LOG" 2>&1; then
    log "forecast_all: GFS falló; ciclo abortado"
    exit 1
fi

if [[ "${ECMWF_ENABLED:-1}" == "1" ]]; then
    if ! python3 "$HERE/fetch_ecmwf.py" --workdir "$FORECAST_WORKDIR" --nowcast-bin "$NOWCAST_BIN" --hours "$ECMWF_HOURS" >>"$LOG" 2>&1; then
        log "forecast_all: WARN ECMWF falló; se conserva GFS y se sigue con ICON"
    fi
fi

if [[ "${ICON_ENABLED:-1}" == "1" ]]; then
    if ! python3 "$HERE/fetch_icon.py" --workdir "$FORECAST_WORKDIR" --nowcast-bin "$NOWCAST_BIN" --hours "$ICON_HOURS" >>"$LOG" 2>&1; then
        log "forecast_all: WARN ICON falló; ciclo degradado sin tercera fuente"
    fi
fi

log "forecast_all: fin"
