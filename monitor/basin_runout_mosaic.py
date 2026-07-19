#!/usr/bin/env python3
"""Mosaico de runout a nivel de cuenca — cobertura continua, no solo 3 puntos.

`runout_localidad.py` corre en una ventana de 12 km centrada en UN punto. Con
solo Vallenar/Freirina/Huasco corridos, los tramos del valle ENTRE esos tres
puntos (y las quebradas que no caen justo en sus ventanas) nunca se simulan
-- se pierden del todo. Este script mosaica el footprint de TODAS las
corridas disponibles (los 3 pueblos + los puntos intermedios de
`basin_runout_sweep`) sobre la grilla del hazard×susceptibilidad de la
cuenca completa, dando cobertura continua a lo largo de todo el valle.

Uso:
    python3 basin_runout_mosaic.py [--runouts ~/nowcast-forecast/runouts]
        [--forecast ~/nowcast-forecast/latest]
Salida: ~/nowcast-forecast/risk_maps/cuenca_huasco/mapa_runout_cuenca.png
        + footprint_union_cuenca.tif (georreferenciado, para GIS)
"""

import argparse
import glob
import os

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=RUNOUTS)
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/risk_maps"))
    ap.add_argument("--pattern", default="vallenar,freirina,huasco,huasco_valle_*",
                    help="nombres de corridas a mosaicar, separados por coma (soporta glob)")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    hz_path = os.path.join(forecast_dir, "atacama", "hazard_x_susc_30m_06_rio_huasco.tif")
    out = os.path.join(a.out_root, "cuenca_huasco")
    os.makedirs(out, exist_ok=True)

    with rasterio.open(hz_path) as src:
        base_shape = src.shape
        base_transform = src.transform
        base_crs = src.crs
        base_profile = src.profile.copy()

    union = np.zeros(base_shape, dtype="uint8")
    n_used = 0
    names = []
    for pat in a.pattern.split(","):
        for d in sorted(glob.glob(os.path.join(a.runouts, pat))):
            fp_path = os.path.join(d, "runout_footprint.tif")
            if not os.path.exists(fp_path):
                continue
            with rasterio.open(fp_path) as src:
                local = np.zeros(base_shape, dtype="float32")
                reproject(source=rasterio.band(src, 1), destination=local,
                          dst_transform=base_transform, dst_crs=base_crs,
                          resampling=Resampling.nearest)
            union = np.maximum(union, (local >= 1).astype("uint8"))
            n_used += 1
            names.append(os.path.basename(d))

    print(f"mosaicadas {n_used} corridas: {', '.join(names)}")
    print(f"cobertura total: {int(union.sum())} celdas · {union.sum() * 900 / 1e6:.1f} km²")

    out_tif = os.path.join(out, "footprint_union_cuenca.tif")
    prof = dict(base_profile)
    prof.update(dtype="uint8", nodata=0, compress="deflate")
    with rasterio.open(out_tif, "w", **prof) as dst:
        dst.write(union, 1)
    print(f"footprint union -> {out_tif}")

    # --- mapa ------------------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    HAZARD_CMAP = mcolors.LinearSegmentedColormap.from_list(
        "hazard", ["#eef1ee", "#cfe0d6", "#8fb99a", "#e8c468", "#c1652c", "#7a2015"])
    INK, PAPER, RULE = "#1b232c", "#f2f0e8", "#c9c3b0"

    with rasterio.open(hz_path) as src:
        hz = src.read(1)

    TOWNS = {
        "Vallenar": (-70.76517, -28.5774),
        "Freirina": (-71.07987, -28.50991),
        "Huasco": (-71.219017, -28.467542),
    }
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", base_crs, always_xy=True)
    town_xy = [t.transform(lon, lat) for lon, lat in TOWNS.values()]
    xs, ys = zip(*town_xy)
    margin = 18_000
    b = rasterio.transform.array_bounds(*base_shape, base_transform)
    view = (max(b[0], min(xs) - margin), max(b[1], min(ys) - margin - 4000),
            min(b[2], max(xs) + margin + 6000), min(b[3], max(ys) + margin))

    fig, ax = plt.subplots(figsize=(12, 8.5), dpi=200)
    ax.set_facecolor(PAPER)
    ax.imshow(np.ma.masked_invalid(hz), extent=[b[0], b[2], b[1], b[3]], origin="upper",
             cmap=HAZARD_CMAP, vmin=0, vmax=1, zorder=2, interpolation="nearest")
    ax.imshow(np.ma.masked_where(union < 1, union), extent=[b[0], b[2], b[1], b[3]],
             origin="upper", cmap=mcolors.ListedColormap(["none", INK]), vmin=0, vmax=1,
             alpha=0.55, zorder=3, interpolation="nearest")
    ax.contour(union, levels=[0.5], extent=[b[0], b[2], b[1], b[3]], origin="upper",
              colors=[INK], linewidths=0.8, zorder=4)

    for name, (lon, lat) in TOWNS.items():
        x, y = t.transform(lon, lat)
        ax.plot(x, y, marker="*", markersize=16, markerfacecolor="#ffd23f",
               markeredgecolor=INK, markeredgewidth=1.0, zorder=6)
        ax.annotate(name, (x, y), xytext=(8, 8), textcoords="offset points",
                   fontsize=9, color=INK, fontweight="bold", zorder=7,
                   bbox=dict(boxstyle="round,pad=0.25", fc=PAPER, ec=RULE, lw=0.5))

    ax.set_xlim(view[0], view[2]); ax.set_ylim(view[1], view[3])
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor(RULE)

    fig.suptitle("Cobertura continua de runout — cuenca del Huasco", fontsize=14,
                fontweight="bold", color=INK, y=0.97)
    fig.text(0.5, 0.925,
             f"{n_used} corridas del ABM mosaicadas (3 pueblos + {n_used-3} puntos intermedios a lo largo del valle) — "
             f"mancha oscura = footprint unión, no solo los 3 puntos originales",
             fontsize=8.5, color="#5a6259", ha="center")

    png_path = os.path.join(out, "mapa_runout_cuenca.png")
    plt.savefig(png_path, facecolor=PAPER, bbox_inches="tight")
    plt.close(fig)
    print(f"mapa -> {png_path}")


if __name__ == "__main__":
    main()
