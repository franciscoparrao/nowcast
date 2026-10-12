#!/usr/bin/env python3
"""Lluvia y peligro por modelo en celdas clave del dominio RM para un ciclo del pronóstico.
Uso: pronostico_rm.py <dir del ciclo, p.ej. ~/nowcast-forecast/20261012T06z>"""
import glob, os, sys
from datetime import datetime, timedelta
import numpy as np, rasterio
C = os.path.expanduser(sys.argv[1]) + "/rm"
CELLS = {"cicatriz SCA": (-70.475, -33.409), "Tobalaba": (-70.55, -33.45),
         "El Colorado": (-70.29, -33.35), "San José de Maipo": (-70.35, -33.64)}
def chile(k): return (datetime.strptime(k, "%Y%m%dT%H%M") - timedelta(hours=3)).strftime("%a %d %H:%M")
for m, base in (("GFS", ""), ("ECMWF", "ecmwf/"), ("ICON", "icon/")):
    files = sorted(glob.glob(f"{C}/{base}steps/step_*.tif"))
    keys = [os.path.basename(f)[5:-4] for f in files]
    dt = (datetime.strptime(keys[1], "%Y%m%dT%H%M") - datetime.strptime(keys[0], "%Y%m%dT%H%M")).total_seconds() / 3600
    hz = sorted(glob.glob(f"{C}/{base}hazard_regional-a5.5/*.tif"))
    print(f"== {m}: paso {dt:.0f} h, {len(files)} pasos, ventana {keys[0]} → {keys[-1]} UTC")
    for name, (lon, lat) in CELLS.items():
        s = []; h = []
        for f in files:
            with rasterio.open(f) as d:
                r, c = d.index(lon, lat); v = float(d.read(1)[r, c]); s.append(0.0 if not np.isfinite(v) else v)
        for f in hz:
            with rasterio.open(f) as d:
                r, c = d.index(lon, lat); v = float(d.read(1)[r, c]); h.append(0.0 if not np.isfinite(v) else v)
        s = np.array(s); i = int(s.argmax()); n24 = max(1, int(round(24 / dt)))
        r24 = max(s[j:j + n24].sum() for j in range(len(s)))
        print(f"  {name:18s} total {s.sum():5.1f} mm | máx 24 h {r24:5.1f} mm | pico {s[i]/dt:4.1f} mm/h el {chile(keys[i])} Chile"
              f" | peligro a*=5.5 máx {max(h):.2f}" if h else "")
