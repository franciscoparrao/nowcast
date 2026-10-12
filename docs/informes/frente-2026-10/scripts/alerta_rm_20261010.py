#!/usr/bin/env python3
"""Primera alerta de monitor@rm (ciclo 05:48Z del 11-oct-2026): qué vio cada feed en las
celdas que cruzaron, sobre los pasos congelados en evidencia/alerta_rm_20261010/."""
import json, os
import numpy as np, rasterio
E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia", "alerta_rm_20261010")
run = json.load(open(os.path.join(E, "last_run_20261012T1517Z.json")))
al = [s for s in run["steps"] if s.get("alert")]
print(f"last_run (ciclo 15:17Z 12-oct): {run['n_steps']} pasos, {run['n_alerts']} en alerta, nivel {run['alert_level']}, "
      f"pico {max(s['max_probability'] for s in run['steps']):.3f}")
fz = json.load(open(os.path.join(E, "fusion_20261012T1517Z.json")))
print("ventana de la fusión:", fz["span"], "| feeds:", {k: v["newest_real"] for k, v in fz["feeds"].items()})
CELLS = {"Lo Barnechea (-70.35,-33.35)": (-70.35, -33.35), "Lampa–Colina (-70.85,-33.15)": (-70.85, -33.15)}
print(f"\n{'paso UTC':15s} " + "".join(f"| {k[:28]:>28s} " for k in CELLS) + "| máx dominio IMERG/GOES/DGA")
for key in ("20261010T2130", "20261010T2200", "20261010T2230", "20261010T2300", "20261010T2330", "20261011T0000"):
    row = f"{key:15s} "; mx = []
    vals = {k: [] for k in CELLS}
    for feed in ("imerg", "goes", "dga"):
        with rasterio.open(os.path.join(E, f"{feed}_{key}.tif")) as d:
            a = d.read(1); a = np.where(np.isfinite(a), a, 0); mx.append(a.max())
            for k, (lon, lat) in CELLS.items():
                r, c = d.index(lon, lat); vals[k].append(float(a[r, c]))
    for k in CELLS:
        row += "| " + " ".join(f"{v:5.2f}" for v in vals[k]).rjust(28) + " "
    print(row + "| " + " ".join(f"{v:5.2f}" for v in mx) + "  (mm/30 min: IMERG GOES DGA)")
print("\nEl Colorado 330077 (archivo DMC), mm por hora UTC:",
      json.load(open(os.path.join(E, "elcolorado_330077_horario_mm.json"))))
