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

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

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
