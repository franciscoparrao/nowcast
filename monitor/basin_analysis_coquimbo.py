#!/usr/bin/env python3
"""Análisis regional — cuencas Elqui/Limarí + transferencia Choapa (Región de
Coquimbo), análogo a `basin_analysis_huasco.py` pero multi-cuenca (no es un
valle lineal único: Elqui, Limarí y Choapa son sistemas hidrográficos
separados que solo comparten la región administrativa).

Mosaica los 3 productos hazard_x_susc_30m ya generados (Elqui, Limarí,
transferencia Choapa) en una vista regional única, con las 9 localidades
tratadas hoy marcadas + contexto histórico 2015 para todas las comunas con
daño documentado (incluidas las que no se corrieron con el ABM, ej. Vicuña).

Uso:
    python3 basin_analysis_coquimbo.py [--forecast ~/nowcast-forecast/latest]
Salida: ~/nowcast-forecast/risk_maps/cuenca_coquimbo/mapa_regional.png
        + resumen_regional.json
"""

import argparse
import json
import os

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from pyproj import Transformer

TOWNS = {
    # nombre -> (lon, lat, poblacion_urbana, cuenca)
    "Coquimbo": (-71.31288, -29.97404, 226042, "Elqui"),
    "Ovalle": (-71.19542, -30.59647, 77928, "Limarí"),
    "Punitaqui": (-71.26056, -30.82836, 5610, "Limarí"),
    "Monte Patria": (-70.96064, -30.69341, 5516, "Limarí"),
    "Combarbalá": (-71.00220, -31.17840, 5501, "Limarí"),
    "Canela": (-71.45636, -31.40210, 1965, "Choapa (transferencia)"),
    "Illapel": (-71.16873, -31.63186, 20267, "Choapa (transferencia)"),
    "Salamanca": (-70.96591, -31.78030, 12357, "Choapa (transferencia)"),
    "Los Vilos": (-71.50172, -31.91037, 13769, "Choapa (transferencia)"),
}

# damnificados, viviendas con dano mayor -- aluvion 25-mar-2015 (Wikipedia,
# cifras oficiales ONEMI). Incluye comunas SIN corrida ABM (contexto).
DAMAGE_2015 = {
    "Vicuña": (208, 50), "Combarbalá": (80, 20), "Coquimbo": (84, 18),
    "Andacollo": (46, 9), "Salamanca": (22, 9), "Punitaqui": (11, 2),
    "Canela": (8, 3), "Paihuano": (37, 4), "La Serena": (2, 7), "Illapel": (2, 0),
}

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")
RISK_MAPS = os.path.expanduser("~/nowcast-forecast/risk_maps")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--out-root", default=RISK_MAPS)
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    cdir = os.path.join(forecast_dir, "coquimbo")
    sources = [
        ("Elqui", os.path.join(cdir, "hazard_x_susc_30m_07_rio_elqui.tif")),
        ("Limarí", os.path.join(cdir, "hazard_x_susc_30m_08_rio_limari.tif")),
        ("Choapa (transferencia)", os.path.join(cdir, "hazard_x_susc_30m_18_choapa_transfer_limari.tif")),
    ]

    out = os.path.join(a.out_root, "cuenca_coquimbo")
    os.makedirs(out, exist_ok=True)

    # --- mosaico recortado a la zona poblada real (no al rectangulo crudo) --
    # Los rasters de origen (en particular 08_rio_limari) tienen relleno FUERA
    # de la cuenca real delineada -- un artefacto de borde en TWI/flow
    # accumulation (linea diagonal de enrutamiento de flujo corrido sobre el
    # rectangulo completo, no sobre la cuenca) contamina esa franja con
    # valores altisimos espurios. Hallazgo 2026-07-17, documentado aparte para
    # el paper1 -- aqui la mitigacion es recortar a un margen razonable
    # alrededor de las 9 localidades reales, igual que basin_analysis_huasco.
    with rasterio.open(sources[1][1]) as ref:
        ref_shape, ref_transform, ref_crs, ref_profile = ref.shape, ref.transform, ref.crs, ref.profile.copy()

    bounds_all = []
    for _, p in sources:
        with rasterio.open(p) as s:
            bounds_all.append(s.bounds)
    raw_left = min(b.left for b in bounds_all)
    raw_bottom = min(b.bottom for b in bounds_all)
    raw_right = max(b.right for b in bounds_all)
    raw_top = max(b.top for b in bounds_all)

    t_pts = Transformer.from_crs("EPSG:4326", ref_crs, always_xy=True)
    town_xy = {name: t_pts.transform(lon, lat) for name, (lon, lat, _, _) in TOWNS.items()}
    xs, ys = zip(*town_xy.values())
    margin = 20_000  # m
    left = max(raw_left, min(xs) - margin)
    bottom = max(raw_bottom, min(ys) - margin)
    right = min(raw_right, max(xs) + margin)
    top = min(raw_top, max(ys) + margin)
    res = 30.0
    width = int((right - left) / res)
    height = int((top - bottom) / res)
    canvas_transform = rasterio.transform.from_origin(left, top, res, res)

    # ademas del recorte al rectangulo poblado, cada capa fuente se enmascara
    # a un radio de 20 km alrededor de SUS PROPIAS localidades -- las 3 capas
    # traen relleno fuera de la cuenca real delineada (mismo patron que el
    # artefacto de Limari: el DEM/factores se calculan sobre el rectangulo
    # completo, no sobre la cuenca), y ese relleno puede caer dentro de la
    # ventana compartida (que si necesita llegar hasta Coquimbo/Los Vilos).
    # R=20km fue elegido por inspeccion: excluye por completo la franja
    # afectada de Limari sin perder cobertura real entre localidades vecinas.
    SOURCE_TOWNS = {
        "Elqui": [n for n, (_, _, _, c) in TOWNS.items() if c == "Elqui"],
        "Limarí": [n for n, (_, _, _, c) in TOWNS.items() if c.startswith("Limarí")],
        "Choapa (transferencia)": [n for n, (_, _, _, c) in TOWNS.items() if c == "Choapa (transferencia)"],
    }
    RADIUS_M = 20_000
    row_idx, col_idx = np.mgrid[0:height, 0:width]
    px_x = left + (col_idx + 0.5) * res
    px_y = top - (row_idx + 0.5) * res

    canvas = np.full((height, width), np.nan, "float32")
    for label, p in sources:
        with rasterio.open(p) as s:
            local = np.full((height, width), np.nan, "float32")
            reproject(source=rasterio.band(s, 1), destination=local,
                      dst_transform=canvas_transform, dst_crs=ref_crs,
                      resampling=Resampling.nearest)
        mindist = np.full((height, width), np.inf, "float32")
        for name in SOURCE_TOWNS[label]:
            tx, ty = town_xy[name]
            mindist = np.minimum(mindist, np.hypot(px_x - tx, px_y - ty))
        local = np.where(mindist <= RADIUS_M, local, np.nan)
        canvas = np.where(np.isfinite(local), np.fmax(np.nan_to_num(canvas, nan=-1), local), canvas)

    out_tif = os.path.join(out, "hazard_x_susc_regional.tif")
    prof = dict(ref_profile)
    prof.update(height=height, width=width, transform=canvas_transform, dtype="float32",
               nodata=np.nan, tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(out_tif, "w", **prof) as dst:
        dst.write(canvas, 1)
    log_msg = f"mosaico regional -> {out_tif} ({height}x{width})"
    print(log_msg)

    # --- mapa ----------------------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    HAZARD_CMAP = mcolors.LinearSegmentedColormap.from_list(
        "hazard", ["#eef1ee", "#cfe0d6", "#8fb99a", "#e8c468", "#c1652c", "#7a2015"])
    INK, PAPER, RULE, DATA_TEAL = "#1b232c", "#f2f0e8", "#c9c3b0", "#2c5f6e"

    t = Transformer.from_crs("EPSG:4326", ref_crs, always_xy=True)
    fig, ax = plt.subplots(figsize=(11, 13), dpi=200)
    ax.set_facecolor(PAPER)
    ax.imshow(np.ma.masked_invalid(canvas), extent=[left, right, bottom, top], origin="upper",
             cmap=HAZARD_CMAP, vmin=0, vmax=1, zorder=2, interpolation="nearest")

    LABEL_OFFSET = {
        "Coquimbo": (10, 8), "Ovalle": (10, -10), "Punitaqui": (-70, 6),
        "Monte Patria": (10, 8), "Combarbalá": (10, 8), "Canela": (-60, -10),
        "Illapel": (10, 8), "Salamanca": (10, -10), "Los Vilos": (-70, 8),
    }
    for name, (lon, lat, pob, cuenca) in TOWNS.items():
        x, y = t.transform(lon, lat)
        fp_path = os.path.join(RUNOUTS, name.lower().replace(" ", "_").replace("á", "a").replace("é", "e"), "runout_footprint.tif")
        ax.plot(x, y, marker="*", markersize=15, markerfacecolor="#ffd23f",
               markeredgecolor=INK, markeredgewidth=0.9, zorder=6)
        ax.annotate(name, (x, y), xytext=LABEL_OFFSET.get(name, (10, 8)), textcoords="offset points",
                   fontsize=7.5, color=INK, fontweight="bold", zorder=7,
                   bbox=dict(boxstyle="round,pad=0.2", fc=PAPER, ec=RULE, lw=0.5))

    ax.set_xlim(left, right); ax.set_ylim(bottom, top)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor(RULE)

    cax = fig.add_axes([0.14, 0.035, 0.72, 0.012])
    cb = plt.colorbar(plt.cm.ScalarMappable(cmap=HAZARD_CMAP, norm=mcolors.Normalize(0, 1)),
                      cax=cax, orientation="horizontal")
    cb.set_label("Gatillo climático × susceptibilidad de terreno (30 m) — Elqui + Limarí + transferencia Choapa",
                fontsize=8, color=INK)
    cb.outline.set_edgecolor(RULE)

    fig.suptitle("Región de Coquimbo — cuencas Elqui, Limarí y provincia del Choapa",
                fontsize=13.5, fontweight="bold", color=INK, y=0.975)
    fig.text(0.5, 0.945, "9 localidades con tratamiento completo (ABM + mapa de riesgo + exposición/vulnerabilidad)",
             fontsize=8.5, color="#5a6259", ha="center")

    png_path = os.path.join(out, "mapa_regional.png")
    plt.savefig(png_path, facecolor=PAPER, bbox_inches="tight")
    plt.close(fig)
    print(f"mapa -> {png_path}")

    # --- resumen json ----------------------------------------------------
    summary = {"towns": {}}
    for name, (lon, lat, pob, cuenca) in TOWNS.items():
        slug = name.lower().replace(" ", "_").replace("á", "a").replace("é", "e")
        entry = {"poblacion": pob, "cuenca": cuenca, "damnificados_2015": None, "viviendas_2015": None}
        for k in DAMAGE_2015:
            if k.lower() in name.lower() or name.lower() in k.lower():
                entry["damnificados_2015"], entry["viviendas_2015"] = DAMAGE_2015[k]
        riesgo_p = os.path.join(RISK_MAPS, slug, "riesgo.json")
        if os.path.exists(riesgo_p):
            entry["riesgo"] = json.load(open(riesgo_p))
        summary["towns"][name] = entry
    summary["contexto_sin_tratamiento_completo"] = {
        k: {"damnificados_2015": v[0], "viviendas_2015": v[1]}
        for k, v in DAMAGE_2015.items() if k not in ("Combarbalá", "Coquimbo", "Salamanca", "Punitaqui", "Canela", "Illapel")
    }
    json_path = os.path.join(out, "resumen_regional.json")
    json.dump(summary, open(json_path, "w"), indent=1, ensure_ascii=False)
    print(f"resumen -> {json_path}")


if __name__ == "__main__":
    main()
