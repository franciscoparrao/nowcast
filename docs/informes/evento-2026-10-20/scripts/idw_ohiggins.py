#!/usr/bin/env python3
"""¿Cuánto del área en alerta de monitor@ohiggins (racha 16:00–18:30Z del 20-oct)
sale de UNA estación? El feed DGA (fetch_dga.py, idw_field) interpola por IDW con
potencia 2 (DGA_IDW_POWER) y SIN radio máximo: cada celda del dominio es un
promedio ponderado de las 5 EMAs. Este script lee la grilla DGA congelada del
paso de mayor lluvia, la compara con la lámina de cada estación en ese paso, y
cuenta las celdas en alerta según la distancia a la EMA más cercana y según la
estación que más pesa en su IDW."""
import glob, json, os, warnings
import numpy as np, rasterio

warnings.filterwarnings("ignore")
E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia", "monitor_ohiggins", "20261020T1600")
cfg = dict(l.strip().split("=", 1) for l in open(os.path.join(E, "config.env")) if "=" in l and not l.startswith("#"))
est = []
for tok in cfg["DGA_STATIONS"].strip('"').split(","):
    code, lon, lat, nom = tok.split(":")
    est.append((code, float(lon), float(lat), nom))
nivel = float(cfg["ALERT_LEVEL"])

pasos = sorted(glob.glob(os.path.join(E, "feeds", "dga", "step_*.tif")))
tot = {}
for p in pasos:
    with rasterio.open(p) as s:
        tot[os.path.basename(p)[5:-4]] = float(np.nansum(s.read(1)))
k = max(tot, key=tot.get)
with rasterio.open(os.path.join(E, "feeds", "dga", f"step_{k}.tif")) as s:
    g = s.read(1); T = s.transform
    lons = np.array([s.xy(0, j)[0] for j in range(g.shape[1])]); lats = np.array([s.xy(i, 0)[1] for i in range(g.shape[0])])
print(f"paso DGA de mayor lámina total: {k}Z | grilla: mín {g.min():.2f}, mediana {np.median(g):.2f}, máx {g.max():.2f} mm/30 min")
for code, lon, lat, nom in est:
    i, j = rasterio.transform.rowcol(T, lon, lat)
    if not (0 <= i < g.shape[0] and 0 <= j < g.shape[1]):  # rowcol no recorta: −1 se leería como la última
        print(f"  {nom:22s} ({lon:.3f},{lat:.3f}): fuera de la grilla (entra al IDW, no tiene celda propia)")
        continue
    print(f"  {nom:22s} celda ({lons[j]:.2f},{lats[i]:.2f}): {g[i, j]:.2f} mm")
print(f"  celdas del dominio con ≥1 mm en ese paso: {(g >= 1).sum()} de {g.size}")

# celdas en alerta en cualquier paso de la racha
alerta = np.zeros_like(g, bool)
for p in glob.glob(os.path.join(E, "hazard", "hazard_*.tif")):
    kk = os.path.basename(p)[7:-4]
    if "20261020T1600" <= kk <= "20261020T1830":
        with rasterio.open(p) as s:
            alerta |= s.read(1) >= nivel
GLON, GLAT = np.meshgrid(lons, lats)
dist = np.stack([np.hypot((GLON - lon) * 111 * np.cos(np.radians(lat)), (GLAT - lat) * 111) for _, lon, lat, _ in est])
w = 1 / np.maximum(dist, 1e-3) ** 2
dom = np.argmax(w, 0)
peso_dom = (w.max(0) / w.sum(0))
print(f"\nceldas en alerta en la racha: {alerta.sum()}")
for b0, b1 in ((0, 10), (10, 25), (25, 50), (50, 200)):
    m = alerta & (dist.min(0) >= b0) & (dist.min(0) < b1)
    print(f"  a {b0:>2d}–{b1:<3d} km de la EMA más cercana: {m.sum():3d}")
for n, (code, _, _, nom) in enumerate(est):
    m = alerta & (dom == n)
    if m.any():
        print(f"  EMA que más pesa = {nom:22s}: {m.sum():3d} celdas (peso IDW mediano {np.median(peso_dom[m]):.2f})")
