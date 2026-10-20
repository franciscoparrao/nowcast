#!/usr/bin/env python3
"""Inspección visual de las huellas: por grupo de puntos del catálogo, RGB
Sentinel-2 antes y después del temporal y el mapa de cambio χ, con los
componentes asociados (contorno) y los puntos encima. Salida PNG en
data/huellas_2021/<out>/chips/. Es la segunda validación (la primera es el par
de ruido de huellas.py): un componente sin firma visible de flujo en el RGB
es sospechoso aunque toque un punto.

Uso: python3 chips.py --pre 2021-01-23 --post 2021-02-02 [--tag x]
"""
import argparse, json, os
import numpy as np
import rasterio
from rasterio.warp import transform as warp_xy, transform_bounds
from rasterio.windows import from_bounds
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

H = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(H, "..", "..", "data", "huellas_2021")
GRUPOS = {"cajon_yeso_g25": (-70.30, -33.86, -70.05, -33.70),
          "las_vertientes": (-70.42, -33.70, -70.25, -33.58),
          "el_arrayan": (-70.50, -33.40, -70.30, -33.31)}


def rgb(path, win):
    with rasterio.open(path) as s:
        a = s.read([3, 2, 1], window=win).astype("float32") / 10000.0
    a = np.clip(a / np.nanpercentile(a[a > 0], 98), 0, 1) if (a > 0).any() else a
    return np.moveaxis(a, 0, -1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", required=True); ap.add_argument("--post", required=True)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    out = os.path.join(D, "out" + (f"_{a.tag}" if a.tag else ""))
    os.makedirs(os.path.join(out, "chips"), exist_ok=True)
    with rasterio.open(os.path.join(D, "s2", f"{a.pre}.tif")) as s:
        crs, T = s.crs, s.transform
    pts = json.load(open(os.path.join(D, "puntos_enero2021.geojson")))["features"]
    px, py = warp_xy("EPSG:4326", crs, [p["properties"]["LONGITUD"] for p in pts], [p["properties"]["LATITUD"] for p in pts])
    hu = json.load(open(os.path.join(out, "huellas_enero2021.geojson")))["features"]
    for nombre, bb in GRUPOS.items():
        x0, y0, x1, y1 = transform_bounds("EPSG:4326", crs, *bb)
        win = from_bounds(x0, y0, x1, y1, T).round_offsets().round_lengths()
        ext = (x0, x1, y0, y1)
        with rasterio.open(os.path.join(out, "chi_evento.tif")) as s:
            chi = s.read(1, window=win)
        fig, ax = plt.subplots(1, 3, figsize=(18, 6.5), sharex=True, sharey=True)
        ax[0].imshow(rgb(os.path.join(D, "s2", f"{a.pre}.tif"), win), extent=ext)
        ax[1].imshow(rgb(os.path.join(D, "s2", f"{a.post}.tif"), win), extent=ext)
        im = ax[2].imshow(chi, extent=ext, cmap="magma", vmin=0, vmax=np.nanpercentile(chi, 99.5))
        for k, t in enumerate((f"antes {a.pre}", f"después {a.post}", "χ de cambio")):
            ax[k].set_title(t)
            ax[k].scatter(px, py, s=14, facecolors="none", edgecolors="cyan", linewidths=0.9)
            for f in hu:
                for ring in (f["geometry"]["coordinates"] if f["geometry"]["type"] == "Polygon" else []):
                    r = np.array(ring)
                    ax[k].plot(r[:, 0], r[:, 1], color="lime", lw=0.7)
            ax[k].set_xlim(x0, x1); ax[k].set_ylim(y0, y1)
        fig.colorbar(im, ax=ax[2], shrink=0.7)
        fig.suptitle(f"{nombre}: puntos del catálogo (cian) y componentes asociados (verde)")
        fig.tight_layout()
        p = os.path.join(out, "chips", f"{nombre}.png")
        fig.savefig(p, dpi=110); plt.close(fig)
        print(p)


if __name__ == "__main__":
    main()


def mosaico_puntos(pre, post, tag, lado_m=800, n=24, orden="norte"):
    """Recortes de lado_m centrados en cada punto (antes | después | χ), a
    resolución completa, para juzgar flujo por flujo. Los puntos van ordenados
    de norte a sur; se muestran los primeros n."""
    out = os.path.join(D, "out" + (f"_{tag}" if tag else ""))
    with rasterio.open(os.path.join(D, "s2", f"{pre}.tif")) as s:
        crs, T = s.crs, s.transform
    pts = json.load(open(os.path.join(D, "puntos_enero2021.geojson")))["features"]
    pts = sorted(pts, key=lambda p: -p["properties"]["LATITUD"])[:n]
    xs, ys = warp_xy("EPSG:4326", crs, [p["properties"]["LONGITUD"] for p in pts], [p["properties"]["LATITUD"] for p in pts])
    cam = rasterio.open(os.path.join(out, "cambio_evento.tif"))
    fig, ax = plt.subplots(n, 3, figsize=(9, 3 * n))
    for i, (p, x, y) in enumerate(zip(pts, xs, ys)):
        h = lado_m / 2
        win = from_bounds(x - h, y - h, x + h, y + h, T).round_offsets().round_lengths()
        ext = (-h, h, -h, h)
        ax[i, 0].imshow(rgb(os.path.join(D, "s2", f"{pre}.tif"), win), extent=ext)
        ax[i, 1].imshow(rgb(os.path.join(D, "s2", f"{post}.tif"), win), extent=ext)
        c = cam.read(1, window=win)
        ax[i, 2].imshow(np.where(c == 255, np.nan, c), extent=ext, cmap="viridis", vmin=0, vmax=1)
        for k in range(3):
            ax[i, k].plot(0, 0, "o", mfc="none", mec="red", ms=8)
            ax[i, k].set_xticks([]); ax[i, k].set_yticks([])
        pr = p["properties"]
        ax[i, 0].set_ylabel(f"{pr['COD_RM']}\n{pr['d_NOMBRE_R'][:22]}", fontsize=7)
    ax[0, 0].set_title(f"antes {pre}", fontsize=9); ax[0, 1].set_title(f"después {post}", fontsize=9)
    ax[0, 2].set_title("cambio (amarillo) / sin dato (blanco)", fontsize=9)
    fig.tight_layout()
    path = os.path.join(out, "chips", f"puntos_{orden}.png")
    fig.savefig(path, dpi=90); plt.close(fig)
    print(path)
