"""Barrido GLM-L2-LCFA GOES-19 12-oct-2026 16Z-23Z: flashes por 10 min sobre Curacautín y la Araucanía."""
import sys, json, os
sys.path.insert(0, os.path.expanduser("~/proyectos/nowcast/monitor"))
import numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import fetch_glm as G
BOXES = {"curacautin": (-72.30, -38.75, -71.50, -38.10),   # ±~0.4° en torno a la ciudad (-71.89,-38.43)
         "araucania":  (-73.60, -39.65, -70.90, -37.55)}
t0 = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc); t1 = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)
keys = []
h = t0
while h < t1:
    try:
        keys += G.list_bucket("noaa-goes19", f"{G.PRODUCT}/{h:%Y/%j/%H}/")
    except Exception as e:
        print("listado falló", h, e, flush=True)
    h += timedelta(hours=1)
keys = sorted(k for k in keys if k.endswith(".nc"))
print(f"{len(keys)} archivos", flush=True)
bins = {}
pts = {b: [] for b in BOXES}
n = 0
# descarga en hilos, parseo NetCDF SOLO en el hilo principal (HDF5 no es thread-safe: segfault 139)
with ThreadPoolExecutor(8) as ex:
    for key, raw, err in ex.map(lambda k: G.fetch_one("noaa-goes19", k), keys):
        n += 1
        if raw is None: print("falló", key, err, flush=True); continue
        lat, lon = G.read_flashes(raw)
        t = G.file_start(key); slot = t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0).strftime("%H:%M")
        for b, (x0, y0, x1, y1) in BOXES.items():
            m = (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
            c = int(m.sum())
            if c:
                bins.setdefault(slot, {}).setdefault(b, 0); bins[slot][b] += c
                pts[b] += [(float(a), float(o), t.strftime("%H:%M:%S")) for a, o in zip(lat[m], lon[m])]
        if n % 180 == 0: print(f"{n}/{len(keys)} procesados, curacautín acumulado {len(pts['curacautin'])}", flush=True)
print("\nflashes por 10 min (UTC)  araucania | curacautín")
for slot in sorted(bins):
    print(f"  {slot}  {bins[slot].get('araucania',0):5d} | {bins[slot].get('curacautin',0):4d}")
print("total", {b: len(v) for b, v in pts.items()})
json.dump({"bins": bins, "curacautin_pts": pts["curacautin"]}, open(os.path.join(os.path.dirname(__file__), "flashes_curacautin_12oct.json"), "w"))
