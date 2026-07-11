#!/usr/bin/env python3
"""Alinea los feeds del monitor sobre un eje temporal común para la fusión.

`nowcast run --fuse-rasters` exige que todas las forzantes tengan exactamente
los mismos pasos, pero los feeds tienen latencias estructuralmente distintas:
IMERG Early llega hasta T−4/5 h, GOES QPE hasta T−minutos, DGA hasta T−1 h.
Cortar la ventana a la intersección tiraría a la basura exactamente las horas
recientes donde vive la ganancia de latencia de la fase A.

Este helper toma la UNIÓN de los pasos disponibles y completa cada feed con
rasters de CERO donde no tiene dato (la cola rezagada de IMERG, el arranque en
frío de DGA). Semántica deliberada para noisy-OR: un cero no aporta gatillo —
el feed rezagado simplemente no opina en esos pasos y los feeds rápidos sí.
Nada es silencioso: los conteos de relleno por feed van al JSON de salida y
monitor.sh los loguea y reporta.

Uso:
    prepare_fusion.py <zero_dir> primary=<dir> [<nombre>=<dir> ...]

Salida (stdout, JSON): span, n_steps, y por feed la lista de rasters (en orden
cronológico, separados por coma), pasos reales, huecos marcados del propio
feed, y pasos cero-rellenados aquí (desglosando la cola de latencia).
Exit 3 si el feed primario no tiene ningún paso real.
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

STEP = timedelta(minutes=30)


def key_time(key):
    return datetime.strptime(key, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def feed_steps(d):
    steps = {}
    for p in glob.glob(os.path.join(d, "step_*.tif")):
        steps[os.path.basename(p)[len("step_"):-len(".tif")]] = p
    marks = {
        os.path.basename(p)[len("gapmark_"):]
        for p in glob.glob(os.path.join(d, "gapmark_*"))
    }
    return steps, marks


def make_zero(ref_tif, out_tif):
    import numpy as np
    import rasterio

    with rasterio.open(ref_tif) as src:
        profile = src.profile
        shape = (src.height, src.width)
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(np.zeros(shape, dtype="float32"), 1)


def main():
    if len(sys.argv) < 3 or not sys.argv[2].startswith("primary="):
        sys.exit(__doc__)
    zero_dir = sys.argv[1]
    feeds = {}
    for spec in sys.argv[2:]:
        name, d = spec.split("=", 1)
        steps, marks = feed_steps(os.path.join(d, "steps") if not d.endswith("steps") else d)
        if steps:
            feeds[name] = (steps, marks)
        elif name == "primary":
            print(json.dumps({"error": "el feed primario no tiene pasos"}))
            sys.exit(3)
        # feed secundario vacío (p.ej. DGA deshabilitado): se omite de la fusión

    all_keys = sorted({k for steps, _ in feeds.values() for k in steps})
    first, last = key_time(all_keys[0]), key_time(all_keys[-1])
    axis = []
    t = first
    while t <= last:
        axis.append(t.strftime("%Y%m%dT%H%M"))
        t += STEP

    os.makedirs(zero_dir, exist_ok=True)
    ref = next(iter(feeds["primary"][0].values()))
    zero_tif = os.path.join(zero_dir, "zero.tif")
    make_zero(ref, zero_tif)  # se regenera siempre: si el dominio cambia, el cero lo sigue

    out = {"span": [axis[0], axis[-1]], "n_steps": len(axis), "feeds": {}}
    for name, (steps, marks) in feeds.items():
        files, zerofill, trailing = [], 0, 0
        newest_real = max((k for k in steps if k not in marks), default=None)
        for k in axis:
            if k in steps:
                files.append(steps[k])
            else:
                files.append(zero_tif)
                zerofill += 1
                if newest_real is not None and k > newest_real:
                    trailing += 1
        out["feeds"][name] = {
            "list": ",".join(files),
            "real": sum(1 for k in axis if k in steps and k not in marks),
            "feed_gapmarks": sum(1 for k in axis if k in marks),
            "zerofill": zerofill,
            "zerofill_trailing_latency": trailing,
            "newest_real": newest_real,
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
