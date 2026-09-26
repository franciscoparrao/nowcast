#!/usr/bin/env bash
# nowcast-watchdog — ¿están CORRIENDO el monitor y el pronóstico?
#
# Por qué existe (2026-09-26): monitor@atacama y nowcast-forecast estuvieron
# 23 días caídos (3-sep → 25-sep) tras un borrado de target/. El monitor avisó
# "CONFIG INVÁLIDA" cada 6 h y nadie lo leyó como "no hay vigilancia"; el
# pronóstico no avisó nada. Un aviso que sale DESDE el ciclo tampoco cubre los
# casos en que el ciclo ni siquiera parte (timer detenido, script roto antes de
# notify, máquina apagada). Este vigía corre en su propio timer y solo mira un
# hecho: cuánto hace que cada servicio COMPLETÓ un ciclo (state/last_ok, que
# estampan monitor.sh y analysis_all.sh vía mark_ok).
#
#   · objetivo con last_ok más viejo que su umbral → URGENTE "DETENIDO desde…"
#     (histéresis 6 h: insiste mientras siga caído, no calla);
#   · objetivo que vuelve → "RECUPERADO" (una vez);
#   · sin last_ok todavía (instalación nueva) → la cuenta parte de la primera
#     vez que el vigía lo vio, para no gritar en el minuto cero.
#
# Objetivos: cada nowcast-monitor@<dominio>.timer y nowcast-monitor.timer
# HABILITADOS, más nowcast-forecast.timer si está habilitado. Para pruebas,
# WATCHDOG_TARGETS="nombre|/ruta/last_ok|horas;…" reemplaza el descubrimiento.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG="${CONFIG:-$HERE/config.env}"
# shellcheck source=config.env
[[ -f "$CONFIG" ]] && source "$CONFIG"
LOCAL_CONFIG="${LOCAL_CONFIG:-$(dirname "$CONFIG")/config.local.env}"
# shellcheck disable=SC1090
[[ -f "$LOCAL_CONFIG" ]] && source "$LOCAL_CONFIG"

WD_DIR="${WATCHDOG_DIR:-$HOME/nowcast-monitor/watchdog}"
mkdir -p "$WD_DIR"
LOG="$WD_DIR/watchdog.log"
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }
NOTIFY_STATE_DIR="$WD_DIR"
# shellcheck source=lib_notify.sh
source "$HERE/lib_notify.sh"

MON_H="${WATCHDOG_MONITOR_HOURS:-3}"     # ciclo de 30 min; arranque en frío ~40 min
FC_H="${WATCHDOG_FORECAST_HOURS:-14}"    # ciclo de 6 h: dos ciclos perdidos

# --- descubrimiento de objetivos ------------------------------------------------
timer_on() { systemctl --user is-enabled -q "$1" 2>/dev/null; }

# WORK_DIR de un config de dominio, sin contaminar este shell.
work_dir_of() { ( source "$1" >/dev/null 2>&1; echo "${WORK_DIR:-}" ); }

targets=()
if [[ -n "${WATCHDOG_TARGETS:-}" ]]; then
    IFS=';' read -r -a targets <<< "$WATCHDOG_TARGETS"
else
    if timer_on nowcast-monitor.timer; then
        targets+=("monitor|$(work_dir_of "$HERE/config.env")/state/last_ok|$MON_H")
    fi
    for cfg in "$HOME"/nowcast-monitor/domains/*/config.env; do
        [[ -f "$cfg" ]] || continue
        dom=$(basename "$(dirname "$cfg")")
        timer_on "nowcast-monitor@$dom.timer" || continue
        targets+=("monitor@$dom|$(work_dir_of "$cfg")/state/last_ok|$MON_H")
    done
    if timer_on nowcast-forecast.timer; then
        targets+=("forecast|${FORECAST_WORKDIR:-$HOME/nowcast-forecast}/state/last_ok|$FC_H")
    fi
fi
if (( ${#targets[@]} == 0 )); then
    log "sin objetivos: ningún timer de nowcast habilitado"
    exit 0
fi

# --- chequeo -------------------------------------------------------------------
now=$(date -u +%s)
for t in "${targets[@]}"; do
    IFS='|' read -r name ok_file max_h <<< "$t"
    key=${name//[^A-Za-z0-9_-]/_}
    seen="$WD_DIR/first_seen_$key"
    [[ -f "$seen" ]] || echo "$now" > "$seen"
    if [[ -f "$ok_file" ]]; then
        last=$(cat "$ok_file" 2>/dev/null || echo 0)
        desde="último ciclo completo"
    else
        last=$(cat "$seen")
        desde="nunca completó un ciclo desde que el vigía lo ve"
    fi
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    age_min=$(( (now - last) / 60 ))
    max_min=$(python3 -c "print(int(float('$max_h') * 60))")
    cuando=$(date -u -d "@$last" +%FT%RZ 2>/dev/null || echo "?")
    horas=$(python3 -c "print(f'{$age_min/60:.1f}')")

    if (( age_min > max_min )); then
        touch "$WD_DIR/down_$key"
        log "DOWN $name: $desde hace ${horas} h ($cuando) > ${max_h} h"
        notify "nowcast: $name DETENIDO" \
            "$name no completa un ciclo hace ${horas} h ($desde: $cuando; umbral ${max_h} h). Mientras siga así NO hay vigilancia. Ver: journalctl --user -u 'nowcast-*' en $(hostname)." \
            urgent "down_$key.hyst" 6
    else
        if [[ -f "$WD_DIR/down_$key" ]]; then
            notify "nowcast: $name RECUPERADO" \
                "$name volvió a completar ciclos (último: $cuando)." default
            rm -f "$WD_DIR/down_$key" "$WD_DIR/down_$key.hyst"
        fi
        log "ok $name: último ciclo hace ${horas} h"
    fi
done
exit 0
