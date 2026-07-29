#!/usr/bin/env python3
"""Autotest local del nowcast-monitor, sin red.

Modo base (sin argumentos): fabrica en /tmp/nowcast-monitor-selftest una
ventana rodante sintética de 96 pasos semihorarios (48 h) sobre una grilla
10×10 de 0.1°: 90 pasos de llovizna y una ráfaga convectiva de 3 h (25 mm por
media hora ≈ 50 mm/h) al final — muy por sobre Caine — más un status.json
fresco y un config.env apuntando al binario recién compilado. Luego:

    SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh

debe terminar en NOTIFY[urgent] ALERTA. Borrar la ráfaga (steps de mayor
valor) y volver a correr debe dar quiet.

Modo fusión (`--fusion`): fabrica el escenario de la fase A — el feed primario
IMERG llega solo hasta T−5 h con llovizna, el feed GOES llega hasta T con una
ráfaga convectiva EN LAS HORAS QUE IMERG AÚN NO VE, y el feed DGA está
deshabilitado (sin token). El mismo comando debe alertar VÍA LA FUSIÓN: si se
corre con GOES_ENABLED=0 el ciclo queda quiet, que es exactamente la latencia
que la fase A recupera. También valida fetch_dga.py --selftest.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
from rasterio.transform import from_origin

BASE = "/tmp/nowcast-monitor-selftest"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Convenio de grilla IMERG del dominio piloto (idéntico a los fetchers).
TRANSFORM = from_origin(-70.80, -33.20, 0.1, 0.1)
PROFILE = dict(driver="GTiff", height=11, width=11, count=1,
               dtype="float32", crs="EPSG:4326", transform=TRANSFORM)


def write_steps(steps_dir, t_end, n_steps, burst_last=0, burst_mm=25.0):
    """n_steps semihorarios que terminan en t_end; los últimos burst_last
    llevan la ráfaga en el cuadrante NE sobre la llovizna de fondo."""
    os.makedirs(steps_dir, exist_ok=True)
    for old in os.listdir(steps_dir):
        os.remove(os.path.join(steps_dir, old))
    for i in range(n_steps):
        t = t_end - timedelta(minutes=30 * (n_steps - 1 - i))
        grid = np.full((11, 11), 0.2, dtype="float32")
        if i >= n_steps - burst_last:
            grid[2:5, 6:9] = burst_mm
        key = t.strftime("%Y%m%dT%H%M")
        with rasterio.open(os.path.join(steps_dir, f"step_{key}.tif"), "w", **PROFILE) as dst:
            dst.write(grid, 1)


def write_status(path, newest, n_steps, extra=None):
    status = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newest_step": newest.strftime("%Y%m%dT%H%M"),
        "stale_minutes": (datetime.now(timezone.utc) - newest).total_seconds() / 60,
        "n_steps": n_steps,
        "n_gaps": 0,
        "gap_fraction": 0.0,
        "fetched_this_cycle": 0,
        "convert_failures": 0,
    }
    status.update(extra or {})
    with open(path, "w") as f:
        json.dump(status, f)


def write_config(fusion):
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
GOES_ENABLED={1 if fusion else 0}
GOES_BUCKET="noaa-goes19"
GOES_STALE_HOURS=6
DGA_ENABLED={1 if fusion else 0}
DMC_USUARIO=""
DMC_TOKEN=""
DGA_STATIONS=""
DGA_STALE_HOURS=3
COMBINE="noisy-or"
NTFY_URL="https://ntfy.sh"
NTFY_TOPIC=""
REALERT_HOURS=6
RESTALE_HOURS=12
WRITE_HAZARD_TIFS=0
""")
    return nowcast_bin


def susc_selftest():
    """El acople SUSC_TIF ↔ ALERT_LEVEL, sobre un evento MARGINAL.

    No prueba la generación del ráster (eso necesita KINGSTON montado): prueba
    lo que puede regresionar en silencio — que el umbral y la susceptibilidad
    van atados. Se fabrica un campo con la estadística real del piloto
    (mediana ~0.544, rango 0.285–0.634) y una ráfaga de 12 mm/h, apenas sobre
    la curva de Caine, que es donde vive la diferencia:

      - uniforme 1.0 + 0.5   → ALERTA (comportamiento de hoy)
      - susc real + 0.5      → QUIET: el evento se PIERDE (el fallo silencioso)
      - susc real + 0.272    → ALERTA, antes y solo en las celdas susceptibles

    Un evento extremo (50 mm/h) NO distingue los tres casos: satura el gatillo
    y alerta igual. Por eso el escenario es marginal a propósito."""
    import subprocess

    rng = np.random.default_rng(20260729)
    susc = rng.uniform(0.285, 0.634, size=(11, 11)).astype("float32")
    susc[5, 5] = 0.544                      # ancla la mediana del piloto
    susc_p = os.path.join(BASE, "susc_selftest.tif")

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6, burst_mm=6.0)  # 12 mm/h
    write_status(os.path.join(BASE, "status.json"), now, 96)
    shutil.rmtree(os.path.join(BASE, "feeds"), ignore_errors=True)
    write_config(fusion=False)
    with rasterio.open(susc_p, "w", **PROFILE) as dst:
        dst.write(susc, 1)

    base_cfg = open(os.path.join(BASE, "config.env")).read()
    casos = [("uniforme 1.0 + 0.5 (hoy)", "", 0.5, True),
             ("susc real + 0.5 (SIN re-derivar)", susc_p, 0.5, False),
             ("susc real + 0.272 (coherente)", susc_p, 0.272, True)]
    fallos = []
    for nombre, tif, nivel, debe_alertar in casos:
        cfg = os.path.join(BASE, "config_susc.env")
        with open(cfg, "w") as f:
            f.write(base_cfg + f'\nSUSC_TIF="{tif}"\nALERT_LEVEL={nivel}\n')
        shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
        subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                       env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg},
                       capture_output=True, timeout=1800)
        d = json.load(open(os.path.join(BASE, "out", "last_run.json")))
        alerta = d["n_alerts"] > 0
        ok = alerta == debe_alertar
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:34s} "
              f"{'ALERTA' if alerta else 'quiet':6s} ({d['n_alerts']} pasos, "
              f"pico {max(s['max_probability'] for s in d['steps']):.3f}) "
              f"— esperado {'ALERTA' if debe_alertar else 'quiet'}")
        if not ok:
            fallos.append(nombre)
    print(f"\n[selftest susc] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main(fusion=False):
    shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "out"), ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)

    if not fusion:
        # Modo base: un solo feed con la ráfaga al final.
        write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6)
        write_status(os.path.join(BASE, "status.json"), now, 96)
        shutil.rmtree(os.path.join(BASE, "feeds"), ignore_errors=True)
        write_config(fusion=False)
        print(f"selftest listo: 96 pasos sintéticos (ráfaga al final) en {BASE}/steps")
    else:
        # Fase A: IMERG rezagado 5 h con pura llovizna; la ráfaga vive SOLO en
        # GOES, en las horas que IMERG aún no publica. DGA sin token.
        imerg_end = now - timedelta(hours=5)
        write_steps(os.path.join(BASE, "steps"), imerg_end, 96, burst_last=0)
        write_status(os.path.join(BASE, "status.json"), imerg_end, 96)
        goes_dir = os.path.join(BASE, "feeds", "goes")
        write_steps(os.path.join(goes_dir, "steps"), now, 20, burst_last=4)
        write_status(os.path.join(goes_dir, "status.json"), now, 20,
                     {"feed": "goes-qpe:selftest"})
        dga_dir = os.path.join(BASE, "feeds", "dga")
        os.makedirs(dga_dir, exist_ok=True)
        with open(os.path.join(dga_dir, "status.json"), "w") as f:
            json.dump({"feed": "dga-dmc", "disabled": True, "reason": "selftest"}, f)
        write_config(fusion=True)
        # El selftest del parser DGA corre aquí mismo (sin red).
        import subprocess

        subprocess.run(
            [sys.executable, os.path.join(REPO, "monitor", "fetch_dga.py"), "--selftest"],
            check=True, cwd=os.path.join(REPO, "monitor"),
        )
        print(f"selftest fusión listo: IMERG hasta {imerg_end:%H:%M}Z (llovizna), "
              f"GOES hasta {now:%H:%M}Z (ráfaga solo-GOES), DGA deshabilitado")
        print("debe ALERTAR por fusión; con GOES_ENABLED=0 en el config debe quedar quiet")
    print(f"correr: SKIP_FETCH=1 CONFIG={BASE}/config.env {REPO}/monitor/monitor.sh")


if __name__ == "__main__":
    if "--susc" in sys.argv:
        sys.exit(susc_selftest())
    main(fusion="--fusion" in sys.argv)
