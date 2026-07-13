#!/usr/bin/env python3
"""Runout on-demand por LOCALIDAD, alimentado por el pronóstico vigente.

Envuelve al runner Rust (`runout_ondemand`) con todo lo que una corrida "en
caliente" necesita durante el evento: extrae del ciclo GFS vigente la señal
puntual (gatillo, hora de cruce, lluvia líquida diaria — elige los 3 peores
días consecutivos, que es lo que el ABM admite — e isoterma del dominio),
selecciona el raster de susceptibilidad de cuenca que cubre el punto, corre
la simulación y reporta el footprint EN RELACIÓN AL PUEBLO (fracción alcanzada
a 0.5/1/2 km del centro y distancia mínima).

ROTULADO: escenario ilustrativo (parámetros Atacama como análogo, sedimento
constante, lluvia uniforme en la ventana, 1 seed) — insumo de análisis
interno, no mapa de amenaza validado ni aviso oficial. Si la susceptibilidad
media de la ventana es ~0 (punto fuera de las cuencas modeladas), la corrida
NO es informativa y se dice a gritos: sin semilla de terreno no hay flujo.

Uso:
    python3 runout_localidad.py --name santa_barbara --lon -72.021 --lat -37.667 \
        --domain nuble-biobio [--forecast ~/nowcast-forecast/latest] [--size-km 12]
Salidas en ~/nowcast-forecast/runouts/<name>/ (footprint, capas, resumen.json).
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

import numpy as np
import rasterio
from pyproj import Transformer

from susc_overlay import DOMAIN_BASINS, KINGSTON

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def point_value(path, lon, lat):
    with rasterio.open(path) as src:
        if src.crs and src.crs.to_epsg() != 4326:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            lon, lat = t.transform(lon, lat)
        return float(list(src.sample([(lon, lat)]))[0][0])


def pick_susceptibility(domain, lon, lat):
    """El raster de cuenca del dominio con DATO VÁLIDO en el punto.

    No basta que los bounds contengan el punto: los rectángulos de cuencas
    vecinas se solapan y un punto del Maule cae dentro del rect de Rapel,
    donde el raster es nodata — sembraría el ABM con susceptibilidad 0 y la
    corrida vacía se leería como "seguro". Se muestrea el valor real.
    """
    for rel in DOMAIN_BASINS.get(domain, []):
        path = os.path.join(KINGSTON, rel)
        if not os.path.exists(path):
            continue
        with rasterio.open(path) as src:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = t.transform(lon, lat)
            b = src.bounds
            if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                continue
            v = list(src.sample([(x, y)]))[0][0]
            if np.isfinite(v):
                return path
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--domain", required=True, choices=list(DOMAIN_BASINS))
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--size-km", type=float, default=12.0)
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    a = ap.parse_args()

    fdir = os.path.join(os.path.realpath(os.path.expanduser(a.forecast)), a.domain)
    if not os.path.isdir(fdir):
        sys.exit(f"no existe el pronóstico del dominio: {fdir}")

    # --- señal puntual del pronóstico -----------------------------------------
    signal = {}
    for key, fname in [("hazard_max_regional", "hazard_max_regional-a5.5.tif"),
                       ("first_crossing_fhour", "first_crossing_fhour_regional-a5.5.tif"),
                       ("rain_liquid_total_mm", "rain_liquid_total.tif")]:
        p = os.path.join(fdir, fname)
        signal[key] = round(point_value(p, a.lon, a.lat), 2) if os.path.exists(p) else None
    daily = defaultdict(float)
    import glob as _glob

    for f in sorted(_glob.glob(os.path.join(fdir, "steps", "step_*.tif"))):
        day = os.path.basename(f)[len("step_"):len("step_") + 8]
        daily[day] += point_value(f, a.lon, a.lat)
    days = sorted(daily)
    # peor tripleta consecutiva (el ABM cubre 72 h de patrones horarios)
    best_i, best_sum = 0, -1.0
    for i in range(max(1, len(days) - 2)):
        s = sum(daily[d] for d in days[i:i + 3])
        if s > best_sum:
            best_i, best_sum = i, s
    rain3 = [round(daily[d], 1) for d in days[best_i:best_i + 3]]
    meta = json.load(open(os.path.join(fdir, "forecast.json")))
    iso0 = meta["iso0_mean_m"]

    susc = pick_susceptibility(a.domain, a.lon, a.lat)
    if susc is None:
        sys.exit(f"SIN COBERTURA de susceptibilidad para ({a.lon},{a.lat}) en "
                 f"{a.domain} — la corrida no sería informativa (sin semilla de "
                 f"terreno no hay flujo). No se corre.")

    out = os.path.join(a.out_root, a.name)
    os.makedirs(out, exist_ok=True)
    print(f"[{a.name}] ciclo {meta['cycle']} | gatillo pto {signal['hazard_max_regional']} "
          f"| cruce f+{signal['first_crossing_fhour']}h | lluvia líquida 120h "
          f"{signal['rain_liquid_total_mm']} mm | 3 días peores {rain3} (desde {days[best_i]}) "
          f"| iso0 {iso0:.0f} m")
    print(f"[{a.name}] susceptibilidad: {os.path.relpath(susc, KINGSTON)}")

    cmd = ["cargo", "run", "--release", "-q", "-p", "nowcast-swarm",
           "--example", "runout_ondemand", "--",
           f"--lon={a.lon}", f"--lat={a.lat}", "--size-km", str(a.size_km),
           "--rain-mm-per-day", ",".join(str(v) for v in rain3),
           "--isotherm-m", str(iso0), "--sediment", "0.5",
           "--susceptibility", susc, "--out", out,
           "--agents", str(a.agents), "--seed", str(a.seed)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        sys.exit(f"runner falló: {proc.stderr[-400:]}")

    # --- proximidad del footprint al centro del poblado ------------------------
    with rasterio.open(os.path.join(out, "runout_footprint.tif")) as src:
        fp = src.read(1)
        tr = src.transform
        t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x0, y0 = t.transform(a.lon, a.lat)
    rows, cols = np.where(fp == 1)
    prox = {"min_distance_m": None, "rings": {}}
    if rows.size:
        xs = tr.c + (cols + 0.5) * tr.a
        ys = tr.f + (rows + 0.5) * tr.e
        d = np.hypot(xs - x0, ys - y0)
        prox["min_distance_m"] = int(d.min())
        yy, xx = np.mgrid[0:fp.shape[0], 0:fp.shape[1]]
        dall = np.hypot(tr.c + (xx + 0.5) * tr.a - x0, tr.f + (yy + 0.5) * tr.e - y0)
        for r_km in (0.5, 1.0, 2.0):
            m = dall <= r_km * 1000
            prox["rings"][f"{r_km}km"] = round(float(fp[m].mean()), 3)
    resumen = {"name": a.name, "lon": a.lon, "lat": a.lat, "domain": a.domain,
               "cycle": meta["cycle"], "signal": signal,
               "rain3_mm_day": rain3, "rain3_start": days[best_i],
               "iso0_m": iso0, "susceptibility": susc,
               "proximity": prox,
               "label": "escenario ilustrativo — parámetros análogos Atacama, "
                        "sin calibrar en la cuenca; NO es aviso oficial"}
    with open(os.path.join(out, "resumen.json"), "w") as f:
        json.dump(resumen, f, indent=1, ensure_ascii=False)
    rings = " | ".join(f"≤{k}: {v*100:.0f}%" for k, v in prox["rings"].items())
    print(f"[{a.name}] footprint vs pueblo: distancia mín "
          f"{prox['min_distance_m']} m | {rings}")
    print(f"[{a.name}] listo → {out}")


if __name__ == "__main__":
    main()
