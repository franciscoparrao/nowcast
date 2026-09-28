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

# Un solo ciclo por WORK_DIR a la vez. Sin esto, dos corridas concurrentes
# escriben los mismos steps/ y status.json: medido el 2026-08-10, un fetcher
# manual y el del timer sobre el mismo dominio terminaron en SEGFAULT del
# proceso GOES. Pasa de verdad porque el arranque en frío (~30-40 min bajando
# la ventana completa) es MÁS LARGO que el intervalo del timer (30 min), así
# que el ciclo siguiente entra encima del anterior. systemd `Type=oneshot`
# protege de dos instancias de la misma unidad, pero no de una corrida manual.
# No-bloqueante a propósito: si ya hay un ciclo corriendo, este se salta — no
# se encolan, porque el ciclo es sin estado y el siguiente tick reintenta.
exec 9>"$WORK_DIR/state/monitor.lock"
if ! flock -n 9; then
    echo "[$(date -u +%FT%TZ)] ciclo omitido: ya hay uno corriendo sobre $WORK_DIR" \
        | tee -a "$WORK_DIR/monitor.log"
    exit 0
fi
LOG="$WORK_DIR/monitor.log"
# Rotación simple: sobre 5 MB, conservar la mitad más reciente (nodo chico;
# el log es fuente de verdad operacional pero no archivo histórico infinito).
if [[ -f "$LOG" && $(stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 5242880 ]]; then
    tail -n 20000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# notify() y mark_ok() viven en lib_notify.sh (compartidos con el pronóstico
# y el watchdog: una sola copia del contrato de histéresis).
NOTIFY_STATE_DIR="$WORK_DIR/state"
# shellcheck source=lib_notify.sh
source "$HERE/lib_notify.sh"

# --- 0. Validación de config (ANTES de todo fetch y guard de datos) ----------
# R4-M1: el guard de SUSC_TIF vivía después del exit-0 de "datos insuficientes",
# y como el arranque en frío SIEMPRE pasa por ahí (~48 h hasta llenar la
# ventana), una config rota no avisaba en 2 días. Ninguna cantidad de datos
# arregla una config mala → fail-fast, antes incluso de gastar red en el fetch.
# Barrido de hermanas: SUSC_TIF, NOWCAST_BIN y CALIBRATOR son las tres rutas de
# config que se consumían tarde (223, 241 y 228 respectivamente).
CONFIG_ERR=""
# Python AISLADO (2026-09-27): los fetchers y el paso de crecidas corren en el
# venv del monitor con versiones fijas (requirements-monitor.txt). PATH
# antepuesto, no una variable, para cubrir también los heredocs `python3 -`.
MONITOR_VENV="${MONITOR_VENV:-$HOME/nowcast-monitor/venv}"
if [[ -n "${SUSC_TIF:-}" && ! -f "$SUSC_TIF" ]]; then
    CONFIG_ERR="SUSC_TIF apunta a un ráster inexistente ($SUSC_TIF). Generarlo una vez en este nodo: python3 $HERE/make_susc_grid.py --out \"$SUSC_TIF\" — o vaciar SUSC_TIF y volver a ALERT_LEVEL=0.5 (monitor de timing). NO se cae a uniforme 1.0: el ALERT_LEVEL del config está derivado PARA la susceptibilidad y sin ella alertaría de más."
elif [[ ! -x "$NOWCAST_BIN" ]]; then
    CONFIG_ERR="NOWCAST_BIN no existe o no es ejecutable ($NOWCAST_BIN). Compilar con: cargo build --release -p nowcast-cli"
elif [[ -n "${CALIBRATOR:-}" && ! -f "$CALIBRATOR" ]]; then
    CONFIG_ERR="CALIBRATOR apunta a un JSON inexistente ($CALIBRATOR). Generarlo con 'nowcast calibrate' o vaciar la variable."
elif [[ ! -x "$MONITOR_VENV/bin/python3" ]]; then
    CONFIG_ERR="falta el venv del monitor ($MONITOR_VENV). Crearlo: python3 -m venv $MONITOR_VENV && $MONITOR_VENV/bin/pip install -r $HERE/requirements-monitor.txt — NO se cae al python3 del sistema: su entorno lo cambia cualquier otro proyecto (numpy 2.5.3 del 7-sep ya rompió su h5py)."
fi
if [[ -n "$CONFIG_ERR" ]]; then
    log "FATAL config: $CONFIG_ERR"
    notify "nowcast-monitor: CONFIG INVÁLIDA" "$CONFIG_ERR El ciclo no corrió." \
        high config_invalida.hyst 6
    exit 1
fi

export PATH="$MONITOR_VENV/bin:$PATH"

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

# R4-H1: perder el feed primario (el calibrado) sigue siendo noticia URGENTE,
# pero ya no implica "sin vigilancia": esa pregunta se responde sobre la UNIÓN
# de feeds, después de la fusión. El guard bloqueante (MIN_STEPS/DEGRADED)
# vive allá abajo — aquí solo se avisa la salud del primario.
HAY_SECUNDARIOS=0
[[ "${GOES_ENABLED:-0}" == "1" || "${DGA_ENABLED:-0}" == "1" ]] && HAY_SECUNDARIOS=1
if (( STALE_FLAG )); then
    if (( HAY_SECUNDARIOS )); then
        notify "nowcast-monitor: feed PRIMARIO atrasado" \
            "Último paso IMERG real: $NEWEST (hace $STALE_MIN_INT min > ${STALE_HOURS} h). La vigilancia sigue SI la fusión (GOES/DGA) cubre la ventana — el guard de datos decide sobre la unión; se pierde mientras tanto la forzante calibrada del umbral I-D." \
            urgent stale "${RESTALE_HOURS:-12}"
    else
        notify "nowcast-monitor: FEED CAÍDO" \
            "Último paso IMERG real: $NEWEST (hace $STALE_MIN_INT min > ${STALE_HOURS} h). Silencio ≠ calma: sin datos NO hay vigilancia." \
            urgent stale "${RESTALE_HOURS:-12}"
    fi
fi

# Arranque en frío absoluto: sin NINGÚN paso del primario no hay eje temporal
# que fusionar (prepare_fusion necesita su ráster de referencia) — se mantiene
# la salida amable de siempre en vez del error duro de prepare_fusion.
if ! compgen -G "$WORK_DIR/steps/step_*.tif" > /dev/null; then
    notify "nowcast-monitor: datos insuficientes" \
        "El feed primario aún no tiene ningún paso (arranque en frío). No se corre el motor todavía." \
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

# --- 3b. Dead-man switch sobre la UNIÓN (R4-H1) --------------------------------
# La pregunta del guard es "¿hay vigilancia posible?", y la respuesta correcta
# es la unión de feeds: un paso cuenta si AL MENOS un feed trae dato real. Con
# un solo feed la unión ES el primario → semántica de siempre, bit a bit. Con
# fase A, un GOES sano cubre la cola que IMERG no publicó y el ciclo corre
# (medido en Coelemu 31-jul: primario 78 pasos reales, unión 108). Silencio ≠
# calma se conserva donde corresponde: si NINGÚN feed cubre, no se corre.
read -r UNION_N UNION_GAP_PCT UNION_NEWEST < <(python3 - "$FUSION_JSON" <<'EOF'
import json, sys
u = json.load(open(sys.argv[1]))["union"]
print(u["real"], int(round(u["gap_fraction"] * 100)), u["newest_real"] or "none")
EOF
) || { log "ERROR: fusion.json sin bloque union"; exit 1; }

if (( UNION_N < ${MIN_STEPS:-96} )); then
    notify "nowcast-monitor: datos insuficientes" \
        "Solo $UNION_N pasos con dato real en la UNIÓN de feeds (mínimo ${MIN_STEPS:-96}; primario $N_STEPS). No se corre el motor todavía (arranque en frío o todos los feeds atrasados)." \
        default coldstart 24
    exit 0
fi

DEGRADED=""
if (( UNION_GAP_PCT > $(python3 -c "print(int(float('${MAX_GAP_FRACTION:-0.10}')*100))") )); then
    DEGRADED=" [DEGRADADO: ${UNION_GAP_PCT}% de la ventana sin dato real en NINGÚN feed]"
    log "WARN: corrida degradada (${UNION_GAP_PCT}% de huecos en la unión)"
fi

SUSC_ARGS=(--uniform-susc 1.0)
# La existencia del ráster ya se validó en el bloque 0 (fail-fast, R4-M1); si
# llegamos aquí con SUSC_TIF no vacío, el archivo existe. El "no caer a
# uniforme 1.0 en silencio" vive allá arriba, con su razón.
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

# Isoterma cero (H0) en SOMBRA: se reporta junto a cada paso y NO toca el
# peligro. h0_guard.py decide si este ciclo puede usarla; si no (serie vieja,
# dominio fuera de 30–36 °S, archivo ausente) H0 se apaga y la lluvia corre
# igual — un diagnóstico nunca puede tumbar el ciclo. Aviso una vez al día.
H0_ARGS=()
H0_NOTE=""
if [[ -n "${H0_CSV:-}" ]]; then
    read -r H0_STATE H0_DETAIL < <(python3 "$HERE/h0_guard.py" --csv "$H0_CSV" \
        --bbox="$BBOX" --fusion-json "$FUSION_JSON" \
        --stale-days "${H0_STALE_DAYS:-3}" 2>>"$LOG" || echo "off h0_guard.py falló (ver log)")
    if [[ "$H0_STATE" == "ok" ]]; then
        H0_ARGS=(--h0-csv "$H0_CSV" --start "$H0_DETAIL")
        [[ -n "${H0_ABS_M:-}" ]] && H0_ARGS+=(--h0-abs-m "$H0_ABS_M")
        rm -f "$WORK_DIR/state/h0_off.hyst"
    else
        log "WARN: H0 (sombra) apagado este ciclo: $H0_DETAIL"
        H0_NOTE=" [H0 apagado: $H0_DETAIL]"
        notify "nowcast-monitor: H0 apagado" \
            "La isoterma cero no se reporta: $H0_DETAIL. Las alertas de lluvia NO cambian (H0 va en sombra)." \
            default h0_off.hyst 24
    fi
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
    "${CAL_ARGS[@]}" "${OUT_ARGS[@]}" "${H0_ARGS[@]}" \
    --format json > "$RUN_JSON" 2>>"$LOG"
RC=$?
if [[ "${ARCHIVE_STEPS:-0}" == "1" && -s "$RUN_JSON" ]]; then
    cp "$RUN_JSON" "$WORK_DIR/out/history/run_$(date -u +%Y%m%dT%H%M).json" 2>/dev/null
fi

# --- 3.5 Path de CRECIDAS (mecanismo distinto, NO reemplaza al I-D) ------------
# Por qué existe (17-ago-2026): el aluvión reportado en Puerto Viejo (Caldera)
# ocurrió con 1.5 mm de lluvia satelital en su propia celda mientras la cabecera
# andina recibía ~30 mm a 100 km al este, y la causal oficial de SENAPRED para esa
# comuna era "Crecida". El I-D mide intensidad SOBRE LA CELDA: estructuralmente no
# puede ver agua ruteada. Este paso corre el canal correcto (producción Hortoniana
# sobre la cuenca aportante + excedencia Q/Q_c) sobre la MISMA ventana ya ingerida.
#
# Corre SIEMPRE, con I-D en alerta o en quiet — el caso que lo motivó es
# precisamente "I-D quiet y crecida en curso". Y nunca mata el ciclo: si falta un
# artefacto o el runner, degrada y avisa en el log, porque el I-D sigue siendo
# válido sin esto.
#
# DEFAULT = MODO SOMBRA (FLOOD_NOTIFY=0): calcula, loguea y escribe out/flood.json
# pero no notifica. Razón: Q_c es un cuantil de la serie SIMULADA (no hay aforo en
# estas cuencas) y el Hortoniano tiene validación event-céntrica n=3 en dos
# cuencas. Mandar eso al canal de avisos sin temporada de sombra erosiona la
# confianza del único canal que llega al teléfono. FLOOD_NOTIFY=1 lo activa.
FLOOD_NOTE=""
if [[ "${FLOOD_ENABLED:-1}" == "1" && -d "$WORK_DIR/flood_basins" ]]; then
    FLOOD_OUT=$(WORK_DIR="$WORK_DIR" FLOOD_NOTIFY="${FLOOD_NOTIFY:-0}" \
        python3 "$HERE/flood_step.py" 2>>"$LOG")
    FRC=$?
    while IFS= read -r ln; do
        [[ -n "$ln" ]] && log "$ln"
    done <<< "$FLOOD_OUT"
    case $FRC in
    3)
        log "crecidas: paso NO disponible (artefactos de cuenca o runner ausentes) — el ciclo sigue solo con el I-D"
        ;;
    2)
        FLOOD_JSON="$WORK_DIR/out/flood.json"
        FLOOD_DETAIL=$(python3 - "$FLOOD_JSON" 2>>"$LOG" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
ex = [c for c in d["cuencas"] if c["excede"]]
ex.sort(key=lambda c: -c["e_max"])
partes = [f"{c['cuenca']} E={c['e_max']:.2f} (Q {c['q_mm_dia']:.2f} vs Q_c {c['q_crit']:.2f} mm/día, "
          f"lluvia 24 h {c['lluvia_24h_mm']:.1f} mm, comunas: {', '.join(c['comunas'])})"
          for c in ex]
print(" · ".join(partes))
EOF
)
        FLOOD_NOTE=" | CRECIDA: $FLOOD_DETAIL"
        if [[ "${FLOOD_NOTIFY:-0}" == "1" ]]; then
            notify "nowcast-monitor: ALERTA CRECIDA (Q/Q_c)" \
                "$FLOOD_DETAIL. OJO: Q_c es cuantil de serie SIMULADA, sin aforo en estas cuencas — mide cuán inusual es la respuesta para este modelo, no magnitud absoluta. Detalle: $FLOOD_JSON" \
                urgent flood_alert "${REALERT_HOURS:-6}"
        else
            log "crecidas: EXCEDE pero en MODO SOMBRA (FLOOD_NOTIFY=0) — no se notifica; ver $FLOOD_JSON"
        fi
        ;;
    0)
        rm -f "$WORK_DIR/state/flood_alert"
        ;;
    *)
        log "crecidas: el paso falló (rc=$FRC) — el ciclo sigue solo con el I-D"
        ;;
    esac
fi

# --- 4. Interpretar y notificar ------------------------------------------------
case $RC in
0)
    # Al volver a quiet, resetear la histéresis para que la PRÓXIMA alerta
    # notifique de inmediato (notificamos cruces, no estados).
    rm -f "$WORK_DIR/state/alert"
    mark_ok "$WORK_DIR/state"
    log "quiet — $UNION_N pasos reales en la unión (primario $N_STEPS, último $NEWEST; unión hasta $UNION_NEWEST)$DEGRADED$FLOOD_NOTE"
    ;;
2)
    DETAIL=$(python3 - "$RUN_JSON" 2>>"$LOG" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
steps = [s for s in r["steps"] if s.get("alert")]
last = steps[-1]
peak = max(s["max_probability"] for s in r["steps"])
msg = (f"{r['n_alerts']} paso(s) en alerta (nivel {r['alert_level']}); "
       f"último: paso {last['step']} con {last['alert']['n_cells']} celda(s) "
       f"({100*last['alert']['fraction']:.0f}% del dominio), pico de la ventana {peak:.2f}")
h = last.get("h0")
if h:  # sombra: informa el MECANISMO, no cambió el peligro
    via = {"absolute": "vía absoluta (patrón de verano)",
           "anomalous": "anomalía mensual (temporal cálido)",
           "both": "ambas vías", "quiet": "sin señal",
           "missing": "sin dato ese día"}.get(h["via"], h["via"])
    alt = f"{h['h0_m']:.0f} m, " if h.get("h0_m") is not None else ""
    racha = f", racha {h['run_days']} d" if h.get("run_days") else ""
    msg += f". H0 (sombra): {alt}{via}{racha}"
print(msg)
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
    mark_ok "$WORK_DIR/state"
    notify "nowcast-monitor: ALERTA I-D en el dominio" \
        "$DETAIL. Último dato IMERG: $NEWEST (~4 h de latencia)$DEGRADED$FUSION_NOTE$FLOOD_NOTE$H0_NOTE. Feeds: $FUSE_SUMMARY. Detalle: $RUN_JSON" \
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
