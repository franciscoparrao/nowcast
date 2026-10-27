#!/usr/bin/env python3
"""Análisis de nivel de cuenca — río Huasco (Vallenar, Freirina, Huasco).

A diferencia de `risk_map_localidad.py` (ventana de 6 km por pueblo), esto
muestra la cuenca COMPLETA de `hazard_x_susc_30m_06_rio_huasco.tif` sin
recortar, con los 3 poblados marcados — para ver el patrón espacial de
riesgo a lo largo de todo el valle, no solo alrededor de cada punto.

CONTEXTO 2015 (verificado, ver notas de la sesión 2026-07-17): el aluvión
del 25-mar-2015 (el mismo evento que calibra el ABM vía Copiapó/Chañaral)
también golpeó fuerte esta cuenca — 69/230/85 damnificados y 23/128/67
viviendas con daño mayor en Freirina/Huasco/Vallenar respectivamente
(fuente: Wikipedia "Efectos del temporal del norte de Chile de 2015 en
Atacama", con cifras oficiales ONEMI/MINSAL). No es una comparación con
datos de lluvia 2015 (no se tienen en el repo) — es contexto de que esta
cuenca YA tiene precedente real de aluvión severo simultáneo en los 3
poblados, del mismo evento que calibra el modelo.

Uso:
    python3 basin_analysis_huasco.py [--forecast ~/nowcast-forecast/latest]
        [--runouts ~/nowcast-forecast/runouts]
Salida: ~/nowcast-forecast/risk_maps/cuenca_huasco/mapa_cuenca.png
"""

import argparse
import json
import os

import numpy as np
import rasterio
from pyproj import Transformer

from lib_raster import decode_hazard30

TOWNS = {
    "Vallenar": (-70.76517, -28.5774, 44168),
    "Freirina": (-71.07987, -28.50991, 3061),
    "Huasco": (-71.219017, -28.467542, 6389),
}

DAMAGE_2015 = {
    # damnificados, viviendas con dano mayor -- aluvion 25-mar-2015
    # (Wikipedia "Efectos del temporal del norte de Chile de 2015 en Atacama")
    "Vallenar": (85, 67),
    "Freirina": (69, 23),
    "Huasco": (230, 128),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/risk_maps"))
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    hz_path = os.path.join(forecast_dir, "atacama", "hazard_x_susc_30m_06_rio_huasco.tif")
    out = os.path.join(a.out_root, "cuenca_huasco")
    os.makedirs(out, exist_ok=True)

    with rasterio.open(hz_path) as src:
        hz = decode_hazard30(src.read(1))
        b = src.bounds
        crs = src.crs
        t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    # Ventana de despliegue: la cuenca completa incluye cabecera andina sin
    # pueblos ni relevancia (mayormente vacia) -- se recorta a un margen
    # razonable alrededor de los 3 poblados, no al raster completo.
    town_xy = [t.transform(lon, lat) for lon, lat, _ in TOWNS.values()]
    xs, ys = zip(*town_xy)
    margin = 18_000  # m
    view = (max(b.left, min(xs) - margin), max(b.bottom, min(ys) - margin - 4000),
            min(b.right, max(xs) + margin + 6000), min(b.top, max(ys) + margin))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    HAZARD_CMAP = mcolors.LinearSegmentedColormap.from_list(
        "hazard", ["#eef1ee", "#cfe0d6", "#8fb99a", "#e8c468", "#c1652c", "#7a2015"])
    INK, PAPER, RULE, DATA_TEAL = "#1b232c", "#f2f0e8", "#c9c3b0", "#2c5f6e"

    fig, ax = plt.subplots(figsize=(11, 8.5), dpi=200)
    ax.set_facecolor(PAPER)
    im = ax.imshow(np.ma.masked_invalid(hz), extent=[b.left, b.right, b.bottom, b.top],
                   origin="upper", cmap=HAZARD_CMAP, vmin=0, vmax=1, zorder=2,
                   interpolation="nearest")

    LABEL_OFFSET = {"Huasco": (-14, 55), "Freirina": (-10, -55), "Vallenar": (14, 45)}
    LABEL_HA = {"Huasco": "right", "Freirina": "center", "Vallenar": "left"}
    for name, (lon, lat, pob) in TOWNS.items():
        x, y = t.transform(lon, lat)
        ax.plot(x, y, marker="*", markersize=18, markerfacecolor="#ffd23f",
               markeredgecolor=INK, markeredgewidth=1.1, zorder=6)
        dmn, viv = DAMAGE_2015.get(name, (None, None))
        label = f"{name} · pob. {pob:,}\n2015: {dmn} damnificados, {viv} viv. daño mayor"
        ax.annotate(label, (x, y), xytext=LABEL_OFFSET[name], textcoords="offset points",
                   fontsize=7.3, color=INK, fontweight="bold", ha=LABEL_HA[name],
                   arrowprops=dict(arrowstyle="-", color=INK, lw=0.6),
                   bbox=dict(boxstyle="round,pad=0.28", fc=PAPER, ec=RULE, lw=0.6), zorder=7)

        fp_path = os.path.join(a.runouts, name.lower(), "runout_footprint.tif")
        if os.path.exists(fp_path):
            with rasterio.open(fp_path) as fsrc:
                fp = fsrc.read(1)
                fb = fsrc.bounds
            ax.imshow(np.ma.masked_where(fp < 1, fp), extent=[fb.left, fb.right, fb.bottom, fb.top],
                      origin="upper", cmap=mcolors.ListedColormap(["none", INK]),
                      vmin=0, vmax=1, alpha=0.5, zorder=4, interpolation="nearest")

    ax.set_xlim(view[0], view[2]); ax.set_ylim(view[1], view[3])
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor(RULE)

    cax = fig.add_axes([0.14, 0.04, 0.72, 0.014])
    cb = plt.colorbar(im, cax=cax, orientation="horizontal")
    cb.set_label("Gatillo climático × susceptibilidad de terreno (30 m) — cuenca completa del Huasco",
                fontsize=8.5, color=INK)
    cb.outline.set_edgecolor(RULE)

    fig.suptitle("Análisis de cuenca — Río Huasco", fontsize=14, fontweight="bold", color=INK, y=0.965)
    fig.text(0.5, 0.925,
             "Vallenar · Freirina · Huasco — footprint runout ABM (mancha oscura) sobre hazard×susceptibilidad de cuenca completa",
             fontsize=9, color="#5a6259", ha="center")
    fig.text(0.5, 0.895,
             "Contexto: los 3 poblados fueron golpeados simultáneamente en el aluvión del 25-mar-2015 (mismo evento que calibra el ABM)",
             fontsize=8, color="#8a5a30", ha="center", style="italic")

    png_path = os.path.join(out, "mapa_cuenca.png")
    plt.savefig(png_path, facecolor=PAPER, bbox_inches="tight")
    plt.close(fig)
    print(f"mapa de cuenca -> {png_path}")

    # --- resumen numerico de la cuenca completa -------------------------------
    finite = hz[np.isfinite(hz)]
    summary = {
        "cuenca": "rio_huasco",
        "basin_hazard_x_susc": {
            "mean": float(finite.mean()), "median": float(np.median(finite)),
            "p90": float(np.percentile(finite, 90)), "p99": float(np.percentile(finite, 99)),
            "frac_over_0.5": float((finite > 0.5).mean()),
        },
        "poblados": {},
    }
    for name, (lon, lat, pob) in TOWNS.items():
        rp = os.path.join(a.runouts, name.lower(), "resumen.json")
        entry = {"poblacion": pob, "damnificados_2015": DAMAGE_2015[name][0],
                 "viviendas_dano_mayor_2015": DAMAGE_2015[name][1]}
        if os.path.exists(rp):
            r = json.load(open(rp))
            entry.update({
                "rain3_mm_day": r.get("rain3_mm_day"),
                "physically_plausible": r.get("physically_plausible"),
                "affected_km2": None,
                "params_preset": r.get("params_preset"),
                "params_preset_iou": r.get("params_preset_iou"),
            })
            sp = os.path.join(a.runouts, name.lower(), "stats.json")
            if os.path.exists(sp):
                entry["affected_km2"] = json.load(open(sp))["runout"]["affected_km2"]
        summary["poblados"][name] = entry
    json_path = os.path.join(out, "resumen_cuenca.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    print(f"resumen -> {json_path}")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
