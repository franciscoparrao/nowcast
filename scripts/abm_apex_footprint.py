#!/usr/bin/env python3
"""Paso 3: asociación apex→comuna por FOOTPRINT FÍSICO DEL ABM (abanico 2D).

Reemplaza el proxy del paso 2 (thalweg D8 de una línea + buffer de 400 m) por el
footprint real del modelo de flujo detrítico: siembra el ABM (`runout_ondemand`,
preset `chanaral_enhanced`) con un disco de fuente en cada fan-apex detectado y
comprueba si el footprint resultante SOLAPA el objetivo poblado de la comuna. La
receta de siembra está validada en el paso 1 (scripts/validate_footprint_vsl.py:
disco en el apex + chanaral_enhanced reproduce la escala del footprint real).

Es el fin del cap de runout arbitrario: la extensión del abanico la fija la
física del ruteo por terreno, no un umbral de distancia.

Flujo:
  1. fan_apex_localidad.classify() → fan-apexes con lon/lat + runout-a-alcanzar.
  2. Se simulan los N apexes que alcanzan a menor runout (los más plausibles).
  3. Por apex: disco de fuente en su UTM → runout_ondemand centrado para incluir
     apex y pueblo → footprint.
  4. Solape con el objetivo (polígono urbano / aldea): fracción del objetivo
     cubierta + área del footprint. El apex ASOCIA si cubre ≥ `min_cover` del
     objetivo (o toca, según --touch).
  5. Comuna: unión de footprints que asocian; clase = del apex asociado de mayor
     Melton.

Uso:
    python3 scripts/abm_apex_footprint.py --comuna "ALTO DEL CARMEN" \
        --basin 06_rio_huasco --lon -70.4859 --lat -28.75741 [--top 3]
Requiere red (DEM Copernicus on-demand) y el crate nowcast-swarm compilable.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_origin
from pyproj import Transformer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "monitor"))
import fan_apex_localidad as FA  # noqa: E402


def utm_epsg(lon, lat):
    """EPSG UTM (WGS84) para el punto (Chile: zonas 18S/19S)."""
    zone = int((lon + 180) / 6) + 1
    return (32700 if lat < 0 else 32600) + zone


def build_source_susc(apex_lon, apex_lat, size_km, path, epsg, disk_m=150.0, pix=30.0):
    """Susceptibilidad de fuente: disco de `disk_m` en el apex, 0 fuera, en la
    grilla UTM que usará runout_ondemand (misma receta que validate_footprint_vsl)."""
    ax, ay = Transformer.from_crs("EPSG:4326", epsg, always_xy=True).transform(apex_lon, apex_lat)
    n = int(size_km * 1000 / pix)
    half = n * pix / 2
    ox = round((ax - half) / pix) * pix
    oy = round((ay + half) / pix) * pix
    xx, yy = np.meshgrid(ox + (np.arange(n) + 0.5) * pix, oy - (np.arange(n) + 0.5) * pix)
    susc = (np.hypot(xx - ax, yy - ay) <= disk_m).astype(np.float32)
    prof = dict(driver="GTiff", height=n, width=n, count=1, dtype="float32",
                crs=f"EPSG:{epsg}", transform=from_origin(ox, oy, pix, pix), compress="deflate")
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(susc, 1)
    return int(susc.sum())


def run_abm(lon, lat, size_km, susc_path, out_dir, rain="40,80,60", preset="chanaral_enhanced",
            seed=42, agents=100, isotherm=3000):
    cmd = ["cargo", "run", "--release", "-q", "-p", "nowcast-swarm", "--example", "runout_ondemand",
           "--", "--lon", str(lon), "--lat", str(lat), "--size-km", str(size_km),
           "--rain-mm-per-day", rain, "--isotherm-m", str(isotherm), "--sediment", "0.5",
           "--susceptibility", susc_path, "--params", preset, "--agents", str(agents),
           "--seed", str(seed), "--out", out_dir]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"runout_ondemand falló:\n{r.stderr[-500:]}")
    return os.path.join(out_dir, "runout_footprint.tif")


def target_overlap(fp_path, comuna):
    """Fracción del objetivo poblado cubierta por el footprint + áreas (km²)."""
    with rasterio.open(fp_path) as src:
        fp = (src.read(1) == 1).astype(np.uint8)
        tr, crs, sh = src.transform, src.crs, fp.shape
        cell_km2 = abs(tr.a * tr.e) / 1e6
    geoms, kind = FA.target_geoms(comuna, crs)
    gt = rasterize([(g.__geo_interface__, 1) for g in geoms], out_shape=sh,
                   transform=tr, fill=0, dtype=np.uint8) if kind == "urbano" else None
    if gt is None:  # aldea: puntos → disco buffer
        gt = np.zeros(sh, np.uint8)
        rad = int(FA.ALDEA_BUFFER_M / abs(tr.a))
        yy, xx = np.mgrid[0:sh[0], 0:sh[1]]
        for g in geoms:
            c = int((g.centroid.x - tr.c) / tr.a); r = int((g.centroid.y - tr.f) / tr.e)
            if 0 <= r < sh[0] and 0 <= c < sh[1]:
                gt |= (np.hypot(yy - r, xx - c) <= rad).astype(np.uint8)
    inter = int(((fp == 1) & (gt == 1)).sum())
    ga = int((gt == 1).sum())
    return dict(cover_frac=(inter / ga if ga else 0.0), fp_km2=int((fp == 1).sum()) * cell_km2,
                target_cells=ga, inter_cells=inter)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--comuna", required=True)
    ap.add_argument("--basin", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--top", type=int, default=3, help="cuántos apexes (menor runout) simular")
    ap.add_argument("--min-cover", type=float, default=0.01,
                    help="fracción mínima del objetivo cubierta para ASOCIAR (touch=cualquier solape)")
    ap.add_argument("--touch", action="store_true", help="asociar con cualquier solape (>0)")
    ap.add_argument("--out")
    a = ap.parse_args()

    print(f"[{a.comuna}] detectando fan-apexes…")
    cl = FA.classify(a.comuna, a.basin, a.lon, a.lat, hydro_source="surtgis")
    if "error" in cl:
        sys.exit(f"[{a.comuna}] {cl['error']}")
    reaching = cl["apex_que_alcanzan"][:a.top]
    if not reaching:
        print(f"[{a.comuna}] sin fan-apex que alcance (proxy D8) → nada que simular (crecida)")
        return
    epsg = utm_epsg(a.lon, a.lat)
    print(f"[{a.comuna}] simulando ABM en {len(reaching)} apex (UTM {epsg})…")

    results = []
    work = tempfile.mkdtemp(prefix="abm_apex_")
    for i, apx in enumerate(reaching, 1):
        # ventana centrada en el apex, cubre apex→pueblo con margen
        size_km = max(12.0, 2.0 * apx["runout_to_reach_km"] + 6.0)
        susc = os.path.join(work, f"src_{i}.tif")
        build_source_susc(apx["lon"], apx["lat"], size_km, susc, epsg)
        try:
            fp = run_abm(apx["lon"], apx["lat"], size_km, susc, os.path.join(work, f"run_{i}"))
            ov = target_overlap(fp, a.comuna)
        except RuntimeError as e:
            print(f"  apex {i} (R{apx['melton']}): ABM falló — {str(e)[:80]}")
            continue
        assoc = ov["cover_frac"] > 0 if a.touch else ov["cover_frac"] >= a.min_cover
        results.append({**{k: apx[k] for k in ("melton", "clase", "area_km2", "lon", "lat",
                                               "runout_to_reach_km")},
                        **ov, "size_km": size_km, "asocia_abm": assoc})
        print(f"  apex {i}: R{apx['melton']:.2f} ({apx['clase']}) · proxy-D8 {apx['runout_to_reach_km']:.1f}km "
              f"· footprint {ov['fp_km2']:.1f} km² · cubre {ov['cover_frac']*100:.1f}% del pueblo "
              f"→ {'ASOCIA' if assoc else 'no'}")

    assoc = [r for r in results if r["asocia_abm"]]
    if assoc:
        best = max(assoc, key=lambda r: r["melton"])
        clase = best["clase"]
    else:
        clase = "crecida fluvial / sin footprint que alcance"
    print(f"\n[{a.comuna}] CLASE (ABM footprint): {clase} · "
          f"{len(assoc)}/{len(results)} apex asocian por footprint físico")
    out = {"comuna": a.comuna, "basin": a.basin, "epsg": epsg,
           "clase_abm": clase, "n_simulados": len(results), "n_asocian": len(assoc),
           "apex": results}
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)
        print(f"→ {a.out}")


if __name__ == "__main__":
    main()
