#!/usr/bin/env bash
# Recolecta desde el nodo del monitor todo lo necesario para el informe del
# evento: archive/ (forzante), out/history/ (peligro por ciclo), logs y
# status por dominio. Idempotente (rsync); correr al cierre del evento — o a
# diario durante, como respaldo incremental fuera del nodo.
#
# Uso: ./collect_event.sh [nodo] [destino]   (defaults: sentinel, ./event-data)
set -euo pipefail

NODE="${1:-sentinel}"
DEST="${2:-./event-data}"
DOMAINS=(rm nuble-biobio araucania)

for dom in "${DOMAINS[@]}"; do
    # rm usa el WORK_DIR histórico; los demás domains/<d>/data
    SRC="~/nowcast-monitor/domains/$dom/data"
    [[ "$dom" == "rm" ]] && SRC="~/nowcast-monitor"
    echo "=== $dom ==="
    mkdir -p "$DEST/$dom"
    rsync -az --info=stats1 \
        --include='archive/***' --include='out/***' \
        --include='monitor.log' --include='status.json' \
        --include='feeds/' --include='feeds/*/' --include='feeds/*/status.json' \
        --exclude='*' \
        "$NODE:$SRC/" "$DEST/$dom/" | grep -E "Number of files|total size" || true
done
echo "listo: $DEST/{rm,nuble-biobio,araucania}/{archive,out/history,monitor.log}"
echo "replay: python3 ../replay_event.py --data $DEST/<dominio> --out $DEST/<dominio>/replay"
