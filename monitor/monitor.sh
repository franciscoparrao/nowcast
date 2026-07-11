#!/usr/bin/env bash
# nowcast-monitor — un ciclo del monitor de eventos (diseñado para cron/systemd).
#
# Arquitectura SIN ESTADO: cada ciclo re-corre la ventana rodante completa con
# `nowcast run` (batch ≡ live bit-idéntico, así que esto es legítimo y un
# reinicio no pierde nada). El estado que sí existe es operacional: histéresis
# de notificaciones y salud del feed.
#
# Exit codes del ciclo: 0 = corrió (quiet o alerta, ya notificada), 1 = error.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG="${CONFIG:-$HERE/config.env}"
# shellcheck source=config.env
source "$CONFIG"
export BBOX WORK_DIR WINDOW_HOURS STALE_HOURS

mkdir -p "$WORK_DIR/out" "$WORK_DIR/state"
LOG="$WORK_DIR/monitor.log"

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# notify TITLE BODY PRIORITY(default|high|urgent) HYSTERESIS_FILE HOURS
# Con histéresis: no repite la misma clase de aviso antes de HOURS horas.
notify() {
    local title="$1" body="$2" prio="$3" hfile="${4:-}" hours="${5:-0}"
    if [[ -n "$hfile" ]]; then
        local now last window
        now=$(date -u +%s)
        last=$(cat "$WORK_DIR/state/$hfile" 2>/dev/null || echo 0)
        window=$(python3 -c "print(int(float('$hours') * 3600))")
        if (( now - last < window )); then
            log "notify suprimida por histéresis ($hfile): $title"
            return 0
        fi
        echo "$now" > "$WORK_DIR/state/$hfile"
    fi
    log "NOTIFY[$prio] $title — $body"
    if [[ -n "${NTFY_TOPIC:-}" ]]; then
        curl -fsS -m 20 -H "Title: $title" -H "Priority: $prio" \
            -d "$body" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null \
            || log "WARN: envío a ntfy falló (se sigue: el log es la fuente de verdad)"
    fi
}

# --- 1. Ingesta --------------------------------------------------------------
if [[ "${SKIP_FETCH:-0}" != "1" ]]; then
    if ! python3 "$HERE/fetch_imerg_early.py" >>"$LOG" 2>&1; then
        notify "nowcast-monitor: FETCH FALLÓ" \
            "El ciclo de ingesta IMERG Early falló; revisa $LOG en $(hostname). El monitor NO corrió." \
            high fetch_fail "${RESTALE_HOURS:-12}"
        exit 1
    fi
fi

# --- 2. Salud del feed (dead-man switch) --------------------------------------
# La lección del --alert-level NaN a escala de sistema: el silencio del feed
# tiene que ser distinguible de la calma meteorológica. Todo el cálculo va en
# UNA llamada a python (locale-safe: nada de printf %f ni bc, y 0.0 es un
# valor legítimo, no un falsy que dispare falsos positivos).
STATUS="$WORK_DIR/status.json"
read -r STALE_FLAG DEGRADED_FLAG STALE_MIN_INT GAP_PCT_INT N_STEPS NEWEST < <(
    STALE_HOURS="$STALE_HOURS" MAX_GAP_FRACTION="${MAX_GAP_FRACTION:-0.10}" \
    python3 - "$STATUS" <<'EOF'
import json, os, sys
s = json.load(open(sys.argv[1]))
stale_min = s.get("stale_minutes")
stale_min = float("inf") if stale_min is None else float(stale_min)
gap = float(s.get("gap_fraction", 1.0))
stale = int(stale_min > float(os.environ["STALE_HOURS"]) * 60)
degraded = int(gap > float(os.environ["MAX_GAP_FRACTION"]))
print(stale, degraded, int(min(stale_min, 9e6)), int(round(gap * 100)),
      int(s.get("n_steps", 0)), s.get("newest_step") or "none")
EOF
) || { log "ERROR: no pude leer $STATUS"; exit 1; }

if (( STALE_FLAG )); then
    notify "nowcast-monitor: FEED CAÍDO" \
        "Último paso IMERG real: $NEWEST (hace $STALE_MIN_INT min > ${STALE_HOURS} h). Silencio ≠ calma: sin datos NO hay vigilancia." \
        urgent stale "${RESTALE_HOURS:-12}"
fi

DEGRADED=""
if (( DEGRADED_FLAG )); then
    DEGRADED=" [DEGRADADO: ${GAP_PCT_INT}% de pasos son huecos rellenados con 0]"
    log "WARN: corrida degradada (${GAP_PCT_INT}% de huecos)"
fi

if (( N_STEPS < ${MIN_STEPS:-96} )); then
    notify "nowcast-monitor: datos insuficientes" \
        "Solo $N_STEPS pasos en la ventana (mínimo ${MIN_STEPS:-96}). No se corre el motor todavía (arranque en frío o feed muy atrasado)." \
        default coldstart 24
    exit 0
fi

# --- 3. Correr el motor sobre la ventana rodante -------------------------------
RASTERS=$(ls "$WORK_DIR"/steps/step_*.tif 2>/dev/null | sort | paste -sd,)
SUSC_ARGS=(--uniform-susc 1.0)
[[ -n "${SUSC_TIF:-}" ]] && SUSC_ARGS=(--susc "$SUSC_TIF")
OUT_ARGS=()
[[ "${WRITE_HAZARD_TIFS:-0}" == "1" ]] && OUT_ARGS=(--out-dir "$WORK_DIR/out/hazard")
CAL_ARGS=()
[[ -n "${CALIBRATOR:-}" ]] && CAL_ARGS=(--calibrator "$CALIBRATOR")

RUN_JSON="$WORK_DIR/out/last_run.json"
"$NOWCAST_BIN" run "${SUSC_ARGS[@]}" \
    --rain-rasters "$RASTERS" \
    --dt-hours "$DT_HOURS" --max-window "$MAX_WINDOW" \
    --id-a "$ID_A" --id-b "$ID_B" --k "$K" \
    --alert-level "$ALERT_LEVEL" \
    "${CAL_ARGS[@]}" "${OUT_ARGS[@]}" \
    --format json > "$RUN_JSON" 2>>"$LOG"
RC=$?

# --- 4. Interpretar y notificar ------------------------------------------------
case $RC in
0)
    # Al volver a quiet, resetear la histéresis para que la PRÓXIMA alerta
    # notifique de inmediato (notificamos cruces, no estados).
    rm -f "$WORK_DIR/state/alert"
    log "quiet — $N_STEPS pasos, último dato $NEWEST$DEGRADED"
    ;;
2)
    DETAIL=$(python3 - "$RUN_JSON" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
steps = [s for s in r["steps"] if s.get("alert")]
last = steps[-1]
peak = max(s["max_probability"] for s in r["steps"])
print(f"{r['n_alerts']} paso(s) en alerta (nivel {r['alert_level']}); "
      f"último: paso {last['step']} con {last['alert']['n_cells']} celda(s) "
      f"({100*last['alert']['fraction']:.0f}% del dominio), pico de la ventana {peak:.2f}")
EOF
)
    notify "nowcast-monitor: ALERTA I-D en el dominio" \
        "$DETAIL. Último dato IMERG: $NEWEST (~4 h de latencia)$DEGRADED. Detalle: $RUN_JSON" \
        urgent alert "${REALERT_HOURS:-6}"
    ;;
*)
    notify "nowcast-monitor: ERROR del motor (rc=$RC)" \
        "nowcast run falló; ver $LOG y $RUN_JSON en $(hostname)." \
        high engine_fail 6
    exit 1
    ;;
esac

exit 0
