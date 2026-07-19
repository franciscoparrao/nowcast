#!/usr/bin/env python3
"""Flood on-demand por LOCALIDAD, alimentado por el pronóstico vigente.

Gemelo de `runout_localidad.py` para crecidas: envuelve al runner Rust
(`flood_ondemand`, crate `nowcast-hydroflux`) con lo que una corrida "en
caliente" necesita durante el evento — extrae de la fuente elegida el peor
día de lluvia líquida en el punto (`flood_ondemand` toma un único
mm/día, a diferencia de los 3 días del ABM de runout), corre el solver de
aguas someras 2D y reporta la profundidad EN RELACIÓN AL PUEBLO (fracción de
un buffer urbano simple 0.5/1/2 km inundada a >= min-depth, y distancia
mínima del agua al centro).

CABECERA DE CUENCA: el runner Rust decide, con una guardia de honestidad, si
inyecta caudal de cabecera (cuando la cuenca aportante cierra dentro de la
ventana ampliada) o corre SOLO con lluvia local (cuando no cierra — típico
de localidades sobre un río troncal grande, ej. el curso principal del
Biobío). Este wrapper NUNCA decide eso por su cuenta: lee `basin.truncated`
y `headwater_source.used` de `stats.json` y lo deja explícito en el resumen.

ROTULADO: escenario ilustrativo (Manning n constante, lluvia uniforme en la
ventana, balance hídrico instantáneo sin tránsito para la cabecera cuando
aplica) — insumo de análisis interno, no mapa de amenaza validado ni aviso
oficial.

Uso:
    python3 flood_localidad.py --name santa_barbara --lon -72.021 --lat -37.667 \
        --domain nuble-biobio [--forecast ~/nowcast-forecast/latest] \
        [--size-km 10] [--basin-window-km 50]
Salidas en ~/nowcast-forecast/floods/<name>/ (flood_depth.tif, stats.json,
resumen.json).
"""

import argparse
import glob
import json
import os
import subprocess
import sys
from collections import defaultdict

import numpy as np
import rasterio
from pyproj import Transformer

from susc_overlay import DOMAIN_BASINS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def point_value(path, lon, lat):
    with rasterio.open(path) as src:
        if src.crs and src.crs.to_epsg() != 4326:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            lon, lat = t.transform(lon, lat)
        return float(list(src.sample([(lon, lat)]))[0][0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--domain", required=True, choices=list(DOMAIN_BASINS))
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--size-km", type=float, default=10.0)
    ap.add_argument("--basin-window-km", type=float, default=50.0)
    ap.add_argument("--basin-pixel-m", type=float, default=90.0)
    ap.add_argument("--manning-n", type=float, default=0.035)
    ap.add_argument("--duration-hours", type=float, default=3.0)
    ap.add_argument("--min-depth", type=float, default=0.05)
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/floods"))
    ap.add_argument("--source", choices=["gfs", "ecmwf", "icon"], default="gfs",
                    help="fuente de lluvia liquida: gfs, ecmwf o icon")
    a = ap.parse_args()

    base_fdir = os.path.join(os.path.realpath(os.path.expanduser(a.forecast)), a.domain)
    fdir = base_fdir if a.source == "gfs" else os.path.join(base_fdir, a.source)
    if not os.path.isdir(fdir):
        sys.exit(f"no existe el pronóstico del dominio/fuente: {fdir}")

    # --- señal puntual del pronóstico -----------------------------------------
    signal = {}
    for key, fname in [("hazard_max_regional", "hazard_max_regional-a5.5.tif"),
                       ("first_crossing_fhour", "first_crossing_fhour_regional-a5.5.tif"),
                       ("rain_liquid_total_mm", "rain_liquid_total.tif")]:
        p = os.path.join(fdir, fname)
        signal[key] = round(point_value(p, a.lon, a.lat), 2) if os.path.exists(p) else None

    daily = defaultdict(float)
    for f in sorted(glob.glob(os.path.join(fdir, "steps", "step_*.tif"))):
        day = os.path.basename(f)[len("step_"):len("step_") + 8]
        daily[day] += point_value(f, a.lon, a.lat)
    if not daily:
        sys.exit(f"[{a.name}] sin steps de lluvia en el pronóstico — no se puede correr")
    days = sorted(daily)
    # flood_ondemand toma UN solo mm/día (lluvia constante durante
    # --duration-hours): se usa el peor día del ciclo vigente, no un
    # promedio, para no diluir el pico que dispara la crecida.
    worst_day = max(days, key=lambda d: daily[d])
    rain_mm_day = round(daily[worst_day], 1)
    meta_name = {"gfs": "forecast.json", "ecmwf": "ecmwf.json", "icon": "icon.json"}[a.source]
    meta = json.load(open(os.path.join(fdir, meta_name)))

    out = os.path.join(a.out_root, a.name)
    os.makedirs(out, exist_ok=True)
    print(f"[{a.name}] ciclo {meta['cycle']} | gatillo pto {signal['hazard_max_regional']} "
          f"| cruce f+{signal['first_crossing_fhour']}h | lluvia líquida 120h "
          f"{signal['rain_liquid_total_mm']} mm | peor día {rain_mm_day} mm/día (desde {worst_day})")

    cmd = ["cargo", "run", "--release", "-q", "-p", "nowcast-hydroflux",
           "--example", "flood_ondemand", "--",
           f"--lon={a.lon}", f"--lat={a.lat}",
           "--size-km", str(a.size_km), "--basin-window-km", str(a.basin_window_km),
           "--basin-pixel-m", str(a.basin_pixel_m),
           "--rain-mm-per-day", str(rain_mm_day),
           "--manning-n", str(a.manning_n), "--duration-hours", str(a.duration_hours),
           "--min-depth", str(a.min_depth),
           "--out", out]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        sys.exit(f"[{a.name}] runner falló: {proc.stderr[-800:]}")

    with open(os.path.join(out, "stats.json")) as f:
        stats = json.load(f)

    # --- proximidad del agua al centro del poblado (buffer urbano simple) -----
    with rasterio.open(os.path.join(out, "flood_depth.tif")) as src:
        depth = src.read(1)
        tr = src.transform
        t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x0, y0 = t.transform(a.lon, a.lat)
    wet = depth >= a.min_depth
    rows, cols = np.where(wet)
    prox = {"min_distance_m": None, "rings": {}}
    if rows.size:
        xs = tr.c + (cols + 0.5) * tr.a
        ys = tr.f + (rows + 0.5) * tr.e
        d = np.hypot(xs - x0, ys - y0)
        prox["min_distance_m"] = int(d.min())
        yy, xx = np.mgrid[0:depth.shape[0], 0:depth.shape[1]]
        dall = np.hypot(tr.c + (xx + 0.5) * tr.a - x0, tr.f + (yy + 0.5) * tr.e - y0)
        for r_km in (0.5, 1.0, 2.0):
            m = dall <= r_km * 1000
            prox["rings"][f"{r_km}km"] = round(float(wet[m].mean()), 3) if m.any() else None

    headwater = stats["headwater_source"]
    basin = stats["basin"]
    result = stats["result"]
    sanity = stats["sanity"]
    relief_m = stats["dem"]["local"]["max_m"] - stats["dem"]["local"]["min_m"]

    # --- Plausibilidad física: criterios FIJADOS ANTES de correr el experimento
    # (2026-07-14), sobre datos de Curacautín post-fix de hydroflux — la firma
    # exacta del artefacto residual documentado en
    # docs/bug-report-2026-07-boundary-slope-instability.md §7 (agua más alta en
    # promedio que el terreno seco). Dos criterios DUROS, objetivos, calculados
    # de stats.json — nunca leídos "a ojo" después de ver el resultado:
    reasons = []
    elev_dry, elev_inun = sanity.get("mean_elev_dry_m"), sanity.get("mean_elev_inundated_m")
    if elev_dry is None or elev_inun is None:
        # Sin celdas de un tipo (0% o 100% inundado): el chequeo de "agua más
        # alta que terreno seco" no es evaluable, pero 100% de una ventana de
        # 3 km inundada en 0.25 h de lluvia local es su propia señal de alarma
        # — no lo dejamos pasar por default silencioso.
        if result["inundated_fraction"] > 0.9:
            reasons.append(
                f"{result['inundated_fraction']*100:.0f}% de la ventana inundada — "
                f"sin celdas secas de referencia, y esa fracción sola es implausible "
                f"para lluvia local sin tránsito de cuenca"
            )
    else:
        elev_delta = elev_inun - elev_dry
        if elev_delta > 1.0:  # tolerancia de 1 m para ruido de muestreo del DEM
            reasons.append(
                f"agua más alta en promedio que terreno seco (+{elev_delta:.1f} m) — "
                f"firma del artefacto residual del bug report §7"
            )
    if relief_m > 0 and result["max_depth_m"] > 0.5 * relief_m:
        reasons.append(
            f"profundidad máxima ({result['max_depth_m']:.1f} m) excede el 50% del "
            f"relieve local de la ventana ({relief_m:.0f} m) — implausible para "
            f"encharcamiento pluvial sin tránsito de cuenca en {a.duration_hours} h"
        )
    plausible = len(reasons) == 0

    resumen = {
        "name": a.name, "lon": a.lon, "lat": a.lat, "domain": a.domain,
        "source": a.source,
        "cycle": meta["cycle"], "signal": signal,
        "rain_mm_day": rain_mm_day, "rain_day": worst_day,
        "headwater_used": headwater["used"],
        "headwater_reason": basin["reason"],
        "inflow_m3s": headwater["inflow_m3s"] if headwater["used"] else None,
        "basin_area_km2": basin["area_km2"] if headwater["used"] else None,
        "max_depth_m": result["max_depth_m"],
        "mean_depth_m": result["mean_depth_m"],
        "inundated_fraction": result["inundated_fraction"],
        "inundated_km2": result["inundated_km2"],
        "proximity": prox,
        "integration_truncated": stats["integration"]["truncated"],
        "integration_unstable": stats["integration"]["unstable"],
        "sanity": sanity,
        "relief_m": relief_m,
        "physically_plausible": plausible,
        "implausibility_reasons": reasons,
        "label": "escenario ilustrativo — Manning n constante, lluvia uniforme en la "
                 "ventana, balance hídrico instantáneo para la cabecera cuando aplica; "
                 "NO es aviso oficial. NO usar si physically_plausible=false.",
    }
    with open(os.path.join(out, "resumen.json"), "w") as f:
        json.dump(resumen, f, indent=1, ensure_ascii=False)

    rings = " | ".join(f"≤{k}: {v*100:.0f}%" if v is not None else f"≤{k}: n/a"
                        for k, v in prox["rings"].items())
    modo = (f"CON cabecera ({headwater['inflow_m3s']:.1f} m³/s de "
            f"{basin['area_km2']:.0f} km²)" if headwater["used"]
            else f"SOLO lluvia local ({basin['reason']})")
    print(f"[{a.name}] modo: {modo}")
    print(f"[{a.name}] profundidad máx {result['max_depth_m']:.2f} m | "
          f"{result['inundated_fraction']*100:.1f}% de la ventana inundada")
    print(f"[{a.name}] agua vs pueblo: distancia mín {prox['min_distance_m']} m | {rings}")
    veredicto = "PLAUSIBLE" if plausible else "NO PLAUSIBLE: " + "; ".join(reasons)
    print(f"[{a.name}] {veredicto}")
    print(f"[{a.name}] listo → {out}")


if __name__ == "__main__":
    main()
