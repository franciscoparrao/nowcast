# shellcheck shell=bash
# notify() compartido por monitor.sh, forecast_all.sh, analysis_all.sh y
# watchdog.sh. Una sola copia a propósito: el contrato de abajo (histéresis
# estampada SOLO tras entrega confirmada) costó un incidente aprenderlo, y una
# segunda copia es exactamente el "se arregló en una y quedó la hermana".
#
# Requiere del que lo sourcea:
#   log()              función de log del script
#   NOTIFY_STATE_DIR   directorio donde viven las marcas de histéresis
#   NTFY_TOPIC/NTFY_URL (opcionales; sin topic el aviso queda solo en el log)

# notify TITLE BODY PRIORITY(default|high|urgent) HYSTERESIS_FILE HOURS
# Con histéresis: no repite la misma clase de aviso antes de HOURS horas.
# Deja en NOTIFY_DELIVERED (0/1) si ESTE aviso salió de la máquina, para que el
# que llama pueda estampar su propio estado solo tras entrega confirmada (mismo
# contrato que la marca de histéresis; lo usa la histéresis por cruce de monitor.sh).
NOTIFY_DELIVERED=0
notify() {
    local title="$1" body="$2" prio="$3" hfile="${4:-}" hours="${5:-0}"
    local now last window entregado=0
    NOTIFY_DELIVERED=0
    now=$(date -u +%s)
    if [[ -n "$hfile" ]]; then
        last=$(cat "$NOTIFY_STATE_DIR/$hfile" 2>/dev/null || echo 0)
        window=$(python3 -c "print(int(float('$hours') * 3600))")
        if (( now - last < window )); then
            log "notify suprimida por histéresis ($hfile): $title"
            return 0
        fi
    fi
    log "NOTIFY[$prio] $title — $body"
    if [[ -n "${NTFY_TOPIC:-}" ]]; then
        if curl -fsS -m 20 -H "Title: $title" -H "Priority: $prio" \
                -d "$body" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null; then
            entregado=1
        else
            log "WARN: envío a ntfy falló — NO se estampa histéresis, se reintenta el próximo ciclo"
        fi
    else
        log "WARN: NTFY_TOPIC vacío: el aviso NO sale de la máquina — NO se estampa histéresis"
    fi
    # La marca de histéresis se estampa DESPUÉS de una entrega confirmada, nunca
    # antes (12-ago-2026). Estampar primero convertía un fallo de ENTREGA en
    # REALERT_HOURS de silencio: con NTFY_TOPIC vacío el dominio atacama anotó
    # "notificado" durante 10 h sin que un solo aviso saliera de la máquina, y
    # después, ya con el canal arreglado, la marca falsa habría tapado la alerta
    # VIVA hasta 6 h más (hubo que borrarla a mano dos veces). Es el mismo
    # patrón que el segfault de teardown de los fetchers: el camino de salida
    # reportando éxito sin haberlo tenido. Consecuencia buscada: si el canal
    # está roto, cada ciclo vuelve a intentar y a dejar el WARN en el journal
    # —ruido acotado a un par de líneas cada 30 min— en vez de silencio.
    if [[ -n "$hfile" && "$entregado" == 1 ]]; then
        mkdir -p "$NOTIFY_STATE_DIR"
        echo "$now" > "$NOTIFY_STATE_DIR/$hfile"
    fi
    NOTIFY_DELIVERED=$entregado
}

# mark_ok STATE_DIR — estampa "este ciclo se completó". Lo lee watchdog.sh: el
# silencio de un servicio que no corre tiene que distinguirse de la calma.
mark_ok() {
    mkdir -p "$1" && date -u +%s > "$1/last_ok"
}
