#!/usr/bin/env python3
"""Autotest local del nowcast-monitor, sin red.

Fabrica en /tmp/nowcast-monitor-selftest una ventana rodante sintética de 96
pasos semihorarios (48 h) sobre una grilla 10×10 de 0.1°: 90 pasos de llovizna
y una ráfaga convectiva de 3 h (25 mm por media hora ≈ 50 mm/h) al final —
muy por sobre Caine — más un status.json fresco y un config.env apuntando al
binario recién compilado. Luego:

    SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh

debe terminar en NOTIFY[urgent] ALERTA. Borrar la ráfaga (steps de mayor
valor) y volver a correr debe dar quiet.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
from rasterio.transform import from_origin

BASE = "/tmp/nowcast-monitor-selftest"
STEPS = os.path.join(BASE, "steps")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    os.makedirs(STEPS, exist_ok=True)
    for old in os.listdir(STEPS):
        os.remove(os.path.join(STEPS, old))
    # Estado operacional limpio: sin histéresis heredada de corridas previas.
    import shutil
    shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "out"), ignore_errors=True)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    t0 = now - timedelta(hours=48)

    transform = from_origin(-70.75, -33.25, 0.1, 0.1)
    profile = dict(driver="GTiff", height=10, width=10, count=1,
                   dtype="float32", crs="EPSG:4326", transform=transform)

    t, i = t0, 0
    while t <= now:
        grid = np.full((10, 10), 0.2, dtype="float32")  # llovizna 0.4 mm/h
        if i >= 90:  # últimas ~3 h: ráfaga en el cuadrante NE
            grid[2:5, 6:9] = 25.0  # 50 mm/h sostenidos
        key = t.strftime("%Y%m%dT%H%M")
        with rasterio.open(os.path.join(STEPS, f"step_{key}.tif"), "w", **profile) as dst:
            dst.write(grid, 1)
        t += timedelta(minutes=30)
        i += 1

    status = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newest_step": now.strftime("%Y%m%dT%H%M"),
        "stale_minutes": 0.0,
        "n_steps": i,
        "n_gaps": 0,
        "gap_fraction": 0.0,
        "fetched_this_cycle": 0,
        "convert_failures": 0,
    }
    with open(os.path.join(BASE, "status.json"), "w") as f:
        json.dump(status, f)

    bin_release = os.path.join(REPO, "target", "release", "nowcast")
    bin_debug = os.path.join(REPO, "target", "debug", "nowcast")
    nowcast_bin = bin_release if os.path.exists(bin_release) else bin_debug
    with open(os.path.join(BASE, "config.env"), "w") as f:
        f.write(f"""BBOX="-70.75,-34.25,-69.75,-33.25"
WORK_DIR="{BASE}"
NOWCAST_BIN="{nowcast_bin}"
ID_A=14.82
ID_B=0.39
K=4.0
DT_HOURS=0.5
MAX_WINDOW=96
ALERT_LEVEL=0.5
CALIBRATOR=""
SUSC_TIF=""
WINDOW_HOURS=54
MIN_STEPS=96
STALE_HOURS=6
MAX_GAP_FRACTION=0.10
NTFY_URL="https://ntfy.sh"
NTFY_TOPIC=""
REALERT_HOURS=6
RESTALE_HOURS=12
WRITE_HAZARD_TIFS=0
""")
    print(f"selftest listo: {i} pasos sintéticos en {STEPS}")
    print(f"correr: SKIP_FETCH=1 CONFIG={BASE}/config.env {REPO}/monitor/monitor.sh")


if __name__ == "__main__":
    main()
