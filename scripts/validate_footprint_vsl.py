#!/usr/bin/env python3
"""Validación out-of-Atacama del footprint del ABM de flujo detrítico.

Primer test del ABM (`runout_ondemand`, preset `chanaral_enhanced`) contra un
evento real mapeado FUERA del dominio de calibración (Atacama): el flujo
detrítico de Villa Santa Lucía (Chaitén, Los Lagos, 2017-12-16), footprint
observado de Copernicus EMS EMSN053 → `data/vsl_mudflow_obs.geojson` (4.9 km²).

Diseño del experimento (ver docs/validacion-footprint.md):
  - No hay susceptibilidad ML en VSL (fuera de toda cuenca modelada), así que se
    fabrica una susceptibilidad de FUENTE: un disco de 150 m en el apex del
    footprint observado (su celda más alta, sobre el DEM Copernicus GLO-30). El
    preset `chanaral_enhanced` (seeding_power=0.376) concentra los agentes en la
    fuente; el terreno rutea el resto. Es un test de RUTEO/ESCALA dado un origen
    conocido, NO de la config operacional susc-driven (que en el centro-sur sí
    usa susceptibilidad ML pero carece de evento mapeado).
  - `runout_ondemand` baja el DEM Copernicus 30 m on-demand → no requiere DEM
    local. Isoterma alta (verano austral: toda lluvia líquida).

RESULTADO (2026-07-21, 6 semillas): footprint 3.98 km² vs observado 4.93 km²
→ la ESCALA generaliza fuera de Atacama (no sobredimensionado cuando se siembra
en la fuente). IoU 0.181 (det., insensible a la lluvia: el ruteo por terreno
fija el footprint, no el volumen) — nivel Copiapó in-sample (0.171), bajo
Chañaral out-of-sample (0.555): la UBICACIÓN generaliza peor, el flujo baja en
parte por una vía adyacente a la observada. El preset `de` (seeding_power=0,
sembrado uniforme) NO sirve con máscara de fuente rala (casi ningún agente cae
en el disco) → footprint vacío; es interacción de config, no falla del modelo.

Uso:
    python3 scripts/validate_footprint_vsl.py [--preset chanaral_enhanced]
        [--rain 40,80,60] [--seed 42] [--size-km 16] [--keep]
Requiere red (DEM Copernicus + build de nowcast-swarm) y el repo swarm-abm
hermano compilable. Offline-limpio NO: es una validación, no parte del core.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.mask import mask
from rasterio.transform import from_origin
from pyproj import Transformer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBS = os.path.join(REPO, "data", "vsl_mudflow_obs.geojson")
# tile Copernicus GLO-30 que cubre VSL (−43.4, −72.4)
DEM_TILE = ("/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/"
            "Copernicus_DSM_COG_10_S44_00_W073_00_DEM/"
            "Copernicus_DSM_COG_10_S44_00_W073_00_DEM.tif")
EPSG_UTM = 32718  # UTM 18S


def apex_of_observed():
    """Celda más alta (real, sin ceros-artefacto) dentro del footprint observado
    = zona de inicio del flujo."""
    obs = gpd.read_file(OBS).to_crs(4326)
    geom = obs.geometry.union_all() if hasattr(obs.geometry, "union_all") else obs.geometry.unary_union
    with rasterio.open(DEM_TILE) as src:
        out, tr = mask(src, [geom.__geo_interface__], crop=True, nodata=np.nan)
    p = out[0].astype(float)
    p = np.where(p <= 5, np.nan, p)  # 0 = nodata/agua en Copernicus DSM
    r, c = np.unravel_index(np.nanargmax(p), p.shape)
    return tr.c + (c + 0.5) * tr.a, tr.f + (r + 0.5) * tr.e, float(np.nanmax(p)), float(np.nanmin(p))


def build_source_susc(apex_lon, apex_lat, size_km, path, disk_m=150.0):
    """Susceptibilidad de fuente: disco de `disk_m` en el apex, 0 fuera, en la
    grilla/CRS que `runout_ondemand` usará (UTM 18S, 30 m, ventana `size_km`)."""
    t = Transformer.from_crs("EPSG:4326", EPSG_UTM, always_xy=True)
    ax, ay = t.transform(apex_lon, apex_lat)
    pix = 30.0
    n = int(size_km * 1000 / pix)
    half = n * pix / 2
    ox = round((ax - half) / pix) * pix
    oy = round((ay + half) / pix) * pix
    xx, yy = np.meshgrid(ox + (np.arange(n) + 0.5) * pix, oy - (np.arange(n) + 0.5) * pix)
    susc = (np.hypot(xx - ax, yy - ay) <= disk_m).astype(np.float32)
    prof = dict(driver="GTiff", height=n, width=n, count=1, dtype="float32",
                crs=f"EPSG:{EPSG_UTM}", transform=from_origin(ox, oy, pix, pix), compress="deflate")
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(susc, 1)
    return int(susc.sum())


def run_abm(lon, lat, size_km, rain, preset, seed, susc_path, out_dir):
    cmd = ["cargo", "run", "--release", "-q", "-p", "nowcast-swarm", "--example", "runout_ondemand", "--",
           "--lon", str(lon), "--lat", str(lat), "--size-km", str(size_km),
           "--rain-mm-per-day", rain, "--isotherm-m", "3000", "--sediment", "0.5",
           "--susceptibility", susc_path, "--params", preset, "--agents", "100",
           "--seed", str(seed), "--out", out_dir]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"runout_ondemand falló:\n{r.stderr[-600:]}")
    return os.path.join(out_dir, "runout_footprint.tif")


def iou_vs_observed(fp_path):
    with rasterio.open(fp_path) as src:
        pred = (src.read(1) == 1).astype(np.uint8)
        tr, crs, sh = src.transform, src.crs, pred.shape
    obs = gpd.read_file(OBS).to_crs(crs)
    gt = rasterize([(g, 1) for g in obs.geometry], out_shape=sh, transform=tr, fill=0, dtype=np.uint8)
    inter = int(((pred == 1) & (gt == 1)).sum())
    union = int(((pred == 1) | (gt == 1)).sum())
    pa, ga = int((pred == 1).sum()), int((gt == 1).sum())
    cell = abs(tr.a * tr.e) / 1e6
    return dict(iou=inter / union if union else 0.0,
                precision=inter / pa if pa else 0.0,
                recall=inter / ga if ga else 0.0,
                pred_km2=pa * cell, obs_km2=ga * cell)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="chanaral_enhanced")
    ap.add_argument("--rain", default="40,80,60")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--size-km", type=float, default=16.0)
    ap.add_argument("--keep", action="store_true", help="no borrar los rásters temporales")
    a = ap.parse_args()

    lon, lat, zmax, zmin = apex_of_observed()
    print(f"apex de inicio: lon={lon:.4f} lat={lat:.4f} (elev {zmax:.0f} m; footprint obs {zmin:.0f}–{zmax:.0f} m)")
    work = tempfile.mkdtemp(prefix="vsl_val_")
    susc = os.path.join(work, "susc_source.tif")
    nseed = build_source_susc(lon, lat, a.size_km, susc)
    print(f"susceptibilidad de fuente: disco de 150 m, {nseed} celdas semilla")
    fp = run_abm(lon, lat, a.size_km, a.rain, a.preset, a.seed, susc, os.path.join(work, "run"))
    m = iou_vs_observed(fp)
    print(f"\n=== VSL · {a.preset} · rain {a.rain} · seed {a.seed} ===")
    print(f"  footprint predicho: {m['pred_km2']:.2f} km²  |  observado: {m['obs_km2']:.2f} km²")
    print(f"  IoU={m['iou']:.3f}  precision={m['precision']:.3f}  recall={m['recall']:.3f}")
    print(f"  referencia: Chañaral out-of-sample 0.555 · Copiapó in-sample 0.171")
    if a.keep:
        print(f"  rásters en {work}")
    else:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(m))


if __name__ == "__main__":
    main()
