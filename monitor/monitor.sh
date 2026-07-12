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
# Credenciales/overrides locales NO versionados (token DMC, ntfy topic):
# config.env es parte del repo público — los secretos viven solo aquí.
LOCAL_CONFIG="${LOCAL_CONFIG:-$(dirname "$CONFIG")/config.local.env}"
# shellcheck disable=SC1090
[[ -f "$LOCAL_CONFIG" ]] && source "$LOCAL_CONFIG"
export BBOX WORK_DIR WINDOW_HOURS STALE_HOURS

mkdir -p "$WORK_DIR/out" "$WORK_DIR/state"
LOG="$WORK_DIR/monitor.log"
# Rotación simple: sobre 5 MB, conservar la mitad más reciente (nodo chico;
# el log es fuente de verdad operacional pero no archivo histórico infinito).
if [[ -f "$LOG" && $(stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 5242880 ]]; then
    tail -n 20000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

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
# IMERG es el feed primario: si su ingesta falla, el ciclo aborta (como
# siempre). Los feeds de fase A (GOES, DGA) son secundarios: su falla degrada
# la fusión y se avisa, pero el monitor sigue corriendo con lo que haya.
if [[ "${SKIP_FETCH:-0}" != "1" ]]; then
    if ! python3 "$HERE/fetch_imerg_early.py" >>"$LOG" 2>&1; then
        notify "nowcast-monitor: FETCH FALLÓ" \
            "El ciclo de ingesta IMERG Early falló; revisa $LOG en $(hostname). El monitor NO corrió." \
            high fetch_fail "${RESTALE_HOURS:-12}"
        exit 1
    fi
    if [[ "${GOES_ENABLED:-0}" == "1" ]]; then
        if ! WORK_DIR="$WORK_DIR/feeds/goes" STALE_HOURS="${GOES_STALE_HOURS:-1}" \
                GOES_BUCKET="${GOES_BUCKET:-noaa-goes19}" \
                python3 "$HERE/fetch_goes_qpe.py" >>"$LOG" 2>&1; then
            notify "nowcast-monitor: ingesta GOES falló" \
                "El fetch GOES QPE falló; el ciclo sigue sin la capa de baja latencia. Ver $LOG." \
                high goes_fetch_fail "${RESTALE_HOURS:-12}"
        fi
    fi
    if [[ "${DGA_ENABLED:-0}" == "1" ]]; then
        if ! WORK_DIR="$WORK_DIR/feeds/dga" DMC_USUARIO="${DMC_USUARIO:-}" \
                DMC_TOKEN="${DMC_TOKEN:-}" DGA_STATIONS="${DGA_STATIONS:-}" \
                python3 "$HERE/fetch_dga.py" >>"$LOG" 2>&1; then
            notify "nowcast-monitor: ingesta DGA falló" \
                "El fetch de telemetría DGA/DMC falló; el ciclo sigue sin la capa in-situ. Ver $LOG." \
                high dga_fetch_fail "${RESTALE_HOURS:-12}"
        fi
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

# --- 2a-bis. Archivo del evento (opcional) -------------------------------------
# ARCHIVE_STEPS=1 preserva la forzante completa antes de que la rotación de la
# ventana la borre (~300 KB/día por dominio): cada step nuevo de cada feed se
# copia una sola vez a archive/. Con eso el evento se puede RE-CORRER offline
# después (otros umbrales, calibración con los positivos reales, figuras).
# Pensado para ventanas de evento (ej. 15-20 jul); apagar en régimen normal.
if [[ "${ARCHIVE_STEPS:-0}" == "1" ]]; then
    for D in "$WORK_DIR" "$WORK_DIR"/feeds/*; do
        [[ -d "$D/steps" ]] || continue
        FEEDNAME=$(basename "$D"); [[ "$D" == "$WORK_DIR" ]] && FEEDNAME=primary
        mkdir -p "$WORK_DIR/archive/$FEEDNAME"
        cp -n "$D"/steps/step_*.tif "$D"/steps/gapmark_* \
            "$WORK_DIR/archive/$FEEDNAME/" 2>/dev/null
    done
fi

# --- 2b. Salud de los feeds secundarios (dead-man por feed) --------------------
# Un feed secundario caído no detiene el ciclo (la fusión degrada a los que
# queden) pero SÍ se avisa: perder GOES es perder la capa de baja latencia y
# eso el operador tiene que saberlo — silencio de feed ≠ calma, también aquí.
FUSION_NOTE=""
for FEED in goes dga; do
    EN_VAR="GOES_ENABLED"; STALE_VAR="${GOES_STALE_HOURS:-1}"
    [[ "$FEED" == "dga" ]] && { EN_VAR="DGA_ENABLED"; STALE_VAR="${DGA_STALE_HOURS:-3}"; }
    [[ "${!EN_VAR:-0}" != "1" ]] && continue
    FSTATUS="$WORK_DIR/feeds/$FEED/status.json"
    read -r F_DISABLED F_STALE F_NEWEST < <(python3 - "$FSTATUS" "$STALE_VAR" <<'EOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print(1, 0, "sin-status"); raise SystemExit
if s.get("disabled"):
    print(1, 0, "deshabilitado"); raise SystemExit
sm = s.get("stale_minutes")
sm = float("inf") if sm is None else float(sm)
print(0, int(sm > float(sys.argv[2]) * 60), s.get("newest_step") or "none")
EOF
    ) || { log "WARN: no pude leer $FSTATUS"; continue; }
    if (( F_DISABLED )); then
        log "feed $FEED no disponible ($F_NEWEST) — la fusión sigue sin él"
    elif (( F_STALE )); then
        notify "nowcast-monitor: feed $FEED CAÍDO" \
            "Último paso real de $FEED: $F_NEWEST. La fusión sigue con los feeds restantes, pero se perdió su aporte de latencia." \
            high "stale_$FEED" "${RESTALE_HOURS:-12}"
        FUSION_NOTE="$FUSION_NOTE [$FEED caído]"
    fi
done

# --- 3. Correr el motor sobre la ventana rodante (fusionada si hay feeds) ------
# prepare_fusion.py alinea los feeds sobre la unión de ventanas: la cola
# rezagada de IMERG entra como cero (no opina) y GOES/DGA cubren esas horas —
# ahí vive la ganancia de latencia de la fase A. Los rellenos van al log.
FUSION_JSON="$WORK_DIR/state/fusion.json"
python3 "$HERE/prepare_fusion.py" "$WORK_DIR/state/zerofill" \
    "primary=$WORK_DIR/steps" \
    $( [[ "${GOES_ENABLED:-0}" == "1" ]] && echo "goes=$WORK_DIR/feeds/goes/steps" ) \
    $( [[ "${DGA_ENABLED:-0}" == "1" ]] && echo "dga=$WORK_DIR/feeds/dga/steps" ) \
    > "$FUSION_JSON" 2>>"$LOG" || { log "ERROR: prepare_fusion falló"; exit 1; }
RASTERS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['feeds']['primary']['list'])" "$FUSION_JSON") \
    || { log "ERROR: fusion.json sin feed primario"; exit 1; }
mapfile -t FUSE_LISTS < <(python3 - "$FUSION_JSON" <<'EOF'
import json, sys
o = json.load(open(sys.argv[1]))
for name, f in o["feeds"].items():
    if name != "primary":
        print(f["list"])
EOF
)
FUSE_SUMMARY=$(python3 - "$FUSION_JSON" <<'EOF'
import json, sys
o = json.load(open(sys.argv[1]))
parts = [f"{n}: {f['real']} reales, {f['zerofill']} rellenos "
         f"({f['zerofill_trailing_latency']} de cola), último {f['newest_real']}"
         for n, f in o["feeds"].items() if n != "primary"]
print("; ".join(parts) if parts else "sin feeds secundarios")
EOF
)
log "fusión (${#FUSE_LISTS[@]} feed(s) secundario(s), ${COMBINE:-noisy-or}): $FUSE_SUMMARY"

SUSC_ARGS=(--uniform-susc 1.0)
[[ -n "${SUSC_TIF:-}" ]] && SUSC_ARGS=(--susc "$SUSC_TIF")
OUT_ARGS=()
[[ "${WRITE_HAZARD_TIFS:-0}" == "1" ]] && OUT_ARGS=(--out-dir "$WORK_DIR/out/hazard")
CAL_ARGS=()
[[ -n "${CALIBRATOR:-}" ]] && CAL_ARGS=(--calibrator "$CALIBRATOR")
FUSE_ARGS=()
if (( ${#FUSE_LISTS[@]} > 0 )); then
    for L in "${FUSE_LISTS[@]}"; do
        FUSE_ARGS+=(--fuse-rasters "$L")
    done
    FUSE_ARGS+=(--combine "${COMBINE:-noisy-or}")
fi

RUN_JSON="$WORK_DIR/out/last_run.json"
if [[ "${ARCHIVE_STEPS:-0}" == "1" ]]; then
    mkdir -p "$WORK_DIR/out/history"
fi
"$NOWCAST_BIN" run "${SUSC_ARGS[@]}" \
    --rain-rasters "$RASTERS" \
    "${FUSE_ARGS[@]}" \
    --dt-hours "$DT_HOURS" --max-window "$MAX_WINDOW" \
    --id-a "$ID_A" --id-b "$ID_B" --k "$K" \
    --alert-level "$ALERT_LEVEL" \
    "${CAL_ARGS[@]}" "${OUT_ARGS[@]}" \
    --format json > "$RUN_JSON" 2>>"$LOG"
RC=$?
if [[ "${ARCHIVE_STEPS:-0}" == "1" && -s "$RUN_JSON" ]]; then
    cp "$RUN_JSON" "$WORK_DIR/out/history/run_$(date -u +%Y%m%dT%H%M).json" 2>/dev/null
fi

# --- 4. Interpretar y notificar ------------------------------------------------
case $RC in
0)
    # Al volver a quiet, resetear la histéresis para que la PRÓXIMA alerta
    # notifique de inmediato (notificamos cruces, no estados).
    rm -f "$WORK_DIR/state/alert"
    log "quiet — $N_STEPS pasos, último dato $NEWEST$DEGRADED"
    ;;
2)
    DETAIL=$(python3 - "$RUN_JSON" 2>>"$LOG" <<'EOF'
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
    # Defensa en profundidad: exit 2 sin JSON parseable NO es una alerta — es
    # el motor fallando de una forma que se disfraza de alerta (p.ej. un
    # binario viejo rechazando un flag nuevo: clap salía 2 en errores de uso).
    if [[ -z "$DETAIL" ]]; then
        notify "nowcast-monitor: ERROR del motor (exit 2 sin salida)" \
            "nowcast run salió 2 pero $RUN_JSON no es parseable — probable binario desactualizado o invocación inválida. Ver $LOG en $(hostname)." \
            high engine_fail 6
        exit 1
    fi
    notify "nowcast-monitor: ALERTA I-D en el dominio" \
        "$DETAIL. Último dato IMERG: $NEWEST (~4 h de latencia)$DEGRADED$FUSION_NOTE. Feeds: $FUSE_SUMMARY. Detalle: $RUN_JSON" \
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
