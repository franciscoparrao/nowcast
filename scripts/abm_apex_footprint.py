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
  4. Solape con el objetivo (polígono urbano / aldea): fracción cubierta →
     banda (fuerte ≥15% / débil 5-15% / marginal <5%). Del stats.json se lee si
     el footprint es DIFUSO (no concentra cuesta abajo, delta_frac<0.08 — mismo
     criterio que runout_localidad.py). El apex ASOCIA LIMPIO si la banda no es
     marginal Y el footprint no es difuso (un footprint difuso que "cubre" el
     pueblo lo hace por esparcimiento en el valle, no por un abanico real → es
     el falso positivo que el proxy D8 del paso 2 no puede ver).
  5. Comuna: clase = del apex asociado limpio de mayor Melton (con aviso si toda
     la cobertura es débil); si solo hay footprints difusos → crecida/difuso.

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

# Bandas de cobertura del objetivo poblado por el footprint del ABM.
COVER_FUERTE = 0.15   # ≥15% del pueblo: abanico que lo alcanza de lleno
COVER_DEBIL = 0.05    # 5-15%: lo roza
# <5% = marginal.
# Difuso: el footprint no concentra cuesta abajo (mismo criterio que el guard de
# plausibilidad de monitor/runout_localidad.py). Un footprint difuso que "cubre"
# el pueblo lo hace por esparcimiento sobre el valle, no por un abanico real.
DIFUSO_DELTA_FRAC = 0.08


def cover_band(frac):
    if frac >= COVER_FUERTE:
        return "fuerte"
    if frac >= COVER_DEBIL:
        return "débil"
    return "marginal"


def read_plausibility(run_dir):
    """Del stats.json del ABM: ¿el footprint concentra cuesta abajo o es difuso?
    delta_frac = (elev media del resto de la ventana − elev media del footprint)
    / relieve. Bajo DIFUSO_DELTA_FRAC → difuso (esparcido, no canalizado)."""
    p = os.path.join(run_dir, "stats.json")
    if not os.path.exists(p):
        return {"delta_frac": None, "window_frac": None, "difuso": None}
    s = json.load(open(p))
    dem, r = s.get("dem", {}), s.get("runout", {})
    relief = (dem.get("max_m", 0) or 0) - (dem.get("min_m", 0) or 0)
    fe, we = r.get("mean_elev_footprint_m"), r.get("mean_elev_window_rest_m")
    if relief <= 0 or fe is None or we is None:
        return {"delta_frac": None, "window_frac": r.get("window_frac"), "difuso": None}
    delta_frac = (we - fe) / relief
    return {"delta_frac": round(delta_frac, 3), "window_frac": round(r.get("window_frac", 0), 3),
            "difuso": delta_frac < DIFUSO_DELTA_FRAC}


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
    # urbano y aldea son polígonos reales → se rasterizan directo (misma lógica
    # que fan_apex_localidad.target_mask, sin el disco de buffer aproximado)
    gt = (rasterize([(g.__geo_interface__, 1) for g in geoms], out_shape=sh,
                    transform=tr, fill=0, dtype=np.uint8) == 1).astype(np.uint8)
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
        run_dir = os.path.join(work, f"run_{i}")
        build_source_susc(apx["lon"], apx["lat"], size_km, susc, epsg)
        try:
            fp = run_abm(apx["lon"], apx["lat"], size_km, susc, run_dir)
            ov = target_overlap(fp, a.comuna)
        except RuntimeError as e:
            print(f"  apex {i} (R{apx['melton']}): ABM falló — {str(e)[:80]}")
            continue
        pl = read_plausibility(run_dir)
        band = cover_band(ov["cover_frac"])
        # asociación LIMPIA = cobertura no marginal Y footprint no difuso (un
        # footprint difuso que "cubre" el pueblo lo hace por esparcimiento en el
        # valle, no por un abanico — es el falso positivo que el proxy no ve).
        assoc = (band != "marginal") and (pl["difuso"] is not True)
        results.append({**{k: apx[k] for k in ("melton", "clase", "area_km2", "lon", "lat",
                                               "runout_to_reach_km")},
                        **ov, "size_km": size_km, "cover_band": band,
                        "difuso": pl["difuso"], "delta_frac": pl["delta_frac"],
                        "window_frac": pl["window_frac"], "asocia_abm": assoc})
        dif = " · DIFUSO (no canaliza)" if pl["difuso"] else ""
        verdict = "ASOCIA" if assoc else ("difuso→descartado" if pl["difuso"] else "no")
        print(f"  apex {i}: R{apx['melton']:.2f} ({apx['clase']}) · proxy-D8 "
              f"{apx['runout_to_reach_km']:.1f}km · footprint {ov['fp_km2']:.1f} km² · cubre "
              f"{ov['cover_frac']*100:.1f}% ({band}){dif} → {verdict}")

    assoc = [r for r in results if r["asocia_abm"]]
    if assoc:
        best = max(assoc, key=lambda r: r["melton"])
        strong = any(r["cover_band"] == "fuerte" for r in assoc)
        clase = best["clase"] + ("" if strong else " (cobertura débil)")
    elif any(r["difuso"] for r in results):
        clase = "crecida fluvial / footprint difuso (no abanico canalizado)"
    else:
        clase = "crecida fluvial / sin footprint que alcance"
    print(f"\n[{a.comuna}] CLASE (ABM footprint): {clase} · "
          f"{len(assoc)}/{len(results)} apex asocian limpio por footprint físico")
    out = {"comuna": a.comuna, "basin": a.basin, "epsg": epsg,
           "clase_abm": clase, "n_simulados": len(results), "n_asocian": len(assoc),
           "apex": results}
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)
        print(f"→ {a.out}")


if __name__ == "__main__":
    main()
