#!/usr/bin/env python3
"""Genera el registro HTML+PDF de "qué sabía el sistema en este momento".

Documento de constancia previa a un evento: fija con timestamp y huella de
integridad (SHA-256 sobre los datos crudos) el estado del pronóstico, la
observación y las simulaciones de flujo detrítico de la watchlist, para poder
compararlo honestamente contra lo que ocurra después — sin ajustar cifras
retroactivamente.

Requiere: el pronóstico vigente en --forecast (default ~/nowcast-forecast/
latest, generado por forecast_gfs.py + susc_overlay.py) y los runouts en
--runouts (default ~/nowcast-forecast/runouts, generados por
runout_localidad.py). Usa cartopy (con datos Natural Earth ya cacheados
offline) y adjustText para los mapas; Chrome/Chromium headless para el PDF
(se omite si no está disponible, dejando el HTML igual).

Uso:
    python3 monitor/report/build_registro.py
    python3 monitor/report/build_registro.py --forecast ~/nowcast-forecast/latest \\
        --out ~/nowcast-forecast/informes
"""
import argparse
import base64
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
import rasterio

HERE = os.path.dirname(os.path.abspath(__file__))

DOMAIN_LABELS = {
    "chile-centro-sur": "Barrido macro (Coquimbo → Los Ríos)",
    "coquimbo": "Coquimbo", "rm": "Región Metropolitana",
    "nuble-biobio": "Ñuble – Biobío", "araucania": "Araucanía",
}
DOMAIN_ORDER = ["chile-centro-sur", "coquimbo", "rm", "nuble-biobio", "araucania"]

LOC_LABELS = {
    "quilaco": ("Quilaco", "Biobío"), "santa_barbara": ("Santa Bárbara", "Biobío"),
    "tome": ("Tomé", "Biobío (costa)"), "futrono": ("Futrono", "Los Ríos"),
    "neltume": ("Neltume", "Los Ríos"), "ralco": ("Ralco", "Alto Biobío"),
    "curacautin": ("Curacautín", "Araucanía"), "las_trancas": ("Las Trancas", "Ñuble"),
    "monte_patria": ("Monte Patria", "Coquimbo"), "antuco": ("Antuco", "Biobío"),
    "melipeuco": ("Melipeuco", "Araucanía"), "san_clemente": ("San Clemente", "Maule"),
    "san_jose_maipo": ("San José de Maipo", "RM · Cajón del Maipo"),
}


def severity(g):
    if g is None or g < 0:
        return ("s-none", "SIN DATO")
    if g >= 0.8:
        return ("s-high", "ALTO")
    if g >= 0.4:
        return ("s-mid", "MEDIO")
    return ("s-low", "BAJO")


def fmt_pct(x):
    return "—" if x is None else f"{x*100:.0f}%"


def fmt_mm(x):
    return "—" if x is None else f"{x:.0f} mm"


def fmt_dist(x):
    if x is None:
        return "—"
    return f"{x} m" if x < 1000 else f"{x/1000:.1f} km"


def fmt_hour(x):
    if x is None or (isinstance(x, float) and x != x):
        return "sin cruce"
    return f"T+{int(x)}h"


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def make_maps(forecast_dir, runouts_dir, watchlist, out_img):
    """Genera mapa_panorama, mapa_overlay y los mini-mapas por localidad."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cartopy.io.shapereader as shpreader
    from shapely.ops import unary_union
    from shapely.vectorized import contains as shp_contains
    from adjustText import adjust_text
    from pyproj import Transformer

    land_shp = shpreader.natural_earth(resolution="50m", category="physical", name="land")
    LAND = unary_union(list(shpreader.Reader(land_shp).geometries()))

    def land_mask_for(bounds, shape):
        h, w = shape
        xs = np.linspace(bounds.left, bounds.right, w, endpoint=False) + (bounds.right - bounds.left) / w / 2
        ys = np.linspace(bounds.top, bounds.bottom, h, endpoint=False) - (bounds.top - bounds.bottom) / h / 2
        xx, yy = np.meshgrid(xs, ys)
        return shp_contains(LAND, xx, yy)

    HAZARD_CMAP = mcolors.LinearSegmentedColormap.from_list(
        "hazard", ["#eef1ee", "#cfe0d6", "#8fb99a", "#e8c468", "#c1652c", "#7a2015"])
    INK, PAPER, LAND_BG, OCEAN_BG, RULE, DATA_TEAL = (
        "#1b232c", "#f2f0e8", "#e4e1d4", "#dbe6e8", "#c9c3b0", "#2c5f6e")
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK,
                         "axes.edgecolor": RULE, "axes.labelcolor": INK,
                         "xtick.color": INK, "ytick.color": INK})

    def basemap(ax, extent):
        ax.set_extent(extent, crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.LAND, facecolor=LAND_BG, zorder=0)
        ax.add_feature(cfeature.OCEAN, facecolor=OCEAN_BG, zorder=0)
        ax.add_feature(cfeature.COASTLINE, linewidth=0.6, edgecolor=INK, zorder=4)
        ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor="#8a8474", zorder=4, linestyle=(0, (3, 2)))
        gl = ax.gridlines(draw_labels=True, linewidth=0.3, color=RULE, alpha=0.7,
                           xlocs=np.arange(-76, -68, 1), ylocs=np.arange(-41, -28, 1))
        gl.top_labels = False; gl.right_labels = False
        gl.xlabel_style = {"size": 7, "color": "#6b6656"}
        gl.ylabel_style = {"size": 7, "color": "#6b6656"}

    def load_masked(path):
        with rasterio.open(path) as src:
            data = src.read(1).astype("float64")
            b = src.bounds
        mask = land_mask_for(b, data.shape)
        data = np.where(mask & np.isfinite(data), data, np.nan)
        return np.ma.masked_invalid(data), b

    extent = [-74.3, -69.5, -40.4, -29.0]

    def panel(raster_path, title, subtitle, cbar_label, out_name, marker_by_hazard):
        fig = plt.figure(figsize=(7.4, 10.6), dpi=200)
        ax = plt.axes([0.09, 0.08, 0.87, 0.86], projection=ccrs.PlateCarree())
        basemap(ax, extent)
        data, b = load_masked(raster_path)
        im = ax.imshow(data, extent=[b.left, b.right, b.bottom, b.top], origin="upper",
                        cmap=HAZARD_CMAP, vmin=0, vmax=1, transform=ccrs.PlateCarree(),
                        zorder=2, alpha=0.94, interpolation="nearest")
        texts = []
        for name, r in watchlist.items():
            lon, lat = r["lon"], r["lat"]
            if marker_by_hazard:
                g = r["signal"]["hazard_max_regional"] or 0
                dist = r["proximity"]["min_distance_m"]
                near = dist is not None and dist < 600
                ax.plot(lon, lat, marker="o", markersize=7.5 if near else 5.5,
                        markerfacecolor=HAZARD_CMAP(g), markeredgecolor=INK,
                        markeredgewidth=1.2 if near else 0.7, transform=ccrs.PlateCarree(), zorder=6)
                label = name.replace("_", " ").title()
                texts.append(ax.text(lon, lat, "  " + label, fontsize=6.4, color=INK,
                                      fontweight="bold" if near else "normal",
                                      transform=ccrs.PlateCarree(), zorder=7))
            else:
                ax.plot(lon, lat, marker="+", markersize=6.5, markeredgecolor=INK,
                        markeredgewidth=1.0, transform=ccrs.PlateCarree(), zorder=6)
        if texts:
            adjust_text(texts, ax=ax, arrowprops=dict(arrowstyle="-", color=INK, lw=0.4, alpha=0.7),
                        expand=(1.15, 1.6), force_text=(0.3, 0.5))
        cax = fig.add_axes([0.14, 0.035, 0.72, 0.013])
        cb = plt.colorbar(im, cax=cax, orientation="horizontal")
        cb.set_label(cbar_label, fontsize=7.3, color=INK)
        cb.ax.tick_params(labelsize=6.3, color=INK)
        cb.outline.set_edgecolor(RULE)
        fig.text(0.09, 0.965, title, fontsize=11.5, color=INK, fontweight="bold", ha="left")
        fig.text(0.09, 0.945, subtitle, fontsize=8, color="#5a6259", ha="left")
        plt.savefig(f"{out_img}/{out_name}.png", facecolor=PAPER)
        plt.close(fig)

    hz_path = f"{forecast_dir}/chile-centro-sur/hazard_max_regional-a5.5.tif"
    xs_path = f"{forecast_dir}/chile-centro-sur/hazard_x_susc_max.tif"
    cycle = json.load(open(f"{forecast_dir}/chile-centro-sur/forecast.json"))["cycle"]
    panel(hz_path, "Excedencia del gatillo lluvia–duración pronosticada",
          f"Ciclo GFS {cycle}  ·  horizonte 120 h  ·  enmascarado a tierra",
          "Índice de peligro pronosticado (excedencia I–D, umbral regional)",
          "mapa_panorama", marker_by_hazard=True)
    panel(xs_path, "Dónde el gatillo cae sobre terreno susceptible",
          "Mosaico de capas de susceptibilidad (propias + transferencias)",
          "Gatillo climático × susceptibilidad de terreno (agregado 0.1°, MAX)",
          "mapa_overlay", marker_by_hazard=False)

    os.makedirs(f"{out_img}/localidades", exist_ok=True)
    for name, r in watchlist.items():
        fp_path = os.path.join(runouts_dir, name, "runout_footprint.tif")
        if not os.path.exists(fp_path):
            continue
        with rasterio.open(fp_path) as src:
            fp = src.read(1); b = src.bounds; crs = src.crs
        fig, ax = plt.subplots(figsize=(3.4, 3.4), dpi=180)
        ax.set_facecolor(LAND_BG)
        ax.imshow(np.ma.masked_where(fp < 1, fp), extent=[b.left, b.right, b.bottom, b.top],
                  origin="upper", cmap=mcolors.ListedColormap(["#7a2015"]), alpha=0.85, zorder=2)
        t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x0, y0 = t.transform(r["lon"], r["lat"])
        ax.plot(x0, y0, marker="*", markersize=16, markerfacecolor=DATA_TEAL,
                markeredgecolor=INK, markeredgewidth=0.8, zorder=5)
        for rad, ls in [(500, "-"), (1000, "--"), (2000, ":")]:
            ax.add_patch(plt.Circle((x0, y0), rad, fill=False, edgecolor=INK,
                                    linewidth=0.7, linestyle=ls, alpha=0.6))
        ax.set_xlim(b.left, b.right); ax.set_ylim(b.bottom, b.top)
        ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_edgecolor(RULE)
        plt.savefig(f"{out_img}/localidades/{name}.png", bbox_inches="tight",
                    facecolor=PAPER, pad_inches=0.05)
        plt.close(fig)


FONT_FACES_TPL = """
@font-face {{ font-family:'Schola'; src:url(data:font/woff2;base64,{schola_reg}) format('woff2'); font-weight:400; font-style:normal; font-display:swap; }}
@font-face {{ font-family:'Schola'; src:url(data:font/woff2;base64,{schola_bold}) format('woff2'); font-weight:700; font-style:normal; font-display:swap; }}
@font-face {{ font-family:'Schola'; src:url(data:font/woff2;base64,{schola_ital}) format('woff2'); font-weight:400; font-style:italic; font-display:swap; }}
@font-face {{ font-family:'HerosCn'; src:url(data:font/woff2;base64,{heroscn_reg}) format('woff2'); font-weight:400; font-style:normal; font-display:swap; }}
@font-face {{ font-family:'HerosCn'; src:url(data:font/woff2;base64,{heroscn_bold}) format('woff2'); font-weight:700; font-style:normal; font-display:swap; }}
@font-face {{ font-family:'MonoInstr'; src:url(data:font/woff2;base64,{mono_reg}) format('woff2'); font-weight:400; font-style:normal; font-display:swap; }}
@font-face {{ font-family:'MonoInstr'; src:url(data:font/woff2;base64,{mono_bold}) format('woff2'); font-weight:700; font-style:normal; font-display:swap; }}
"""

CSS = """
:root {
  --ink: #1a222a; --ink-soft: #545e57; --ink-faint: #7c8579;
  --paper: #eef0ee; --paper-raised: #f8f9f6; --rule: #c7cabf; --rule-strong: #9ba39a;
  --accent: #a85a24; --accent-soft: #d99a5c; --data: #2c5f6e; --data-soft: #bcd3d8;
  --sev-high-bg: #f3ded7; --sev-high-fg: #8a2f1c;
  --sev-mid-bg: #f2e7cd; --sev-mid-fg: #8c6414;
  --sev-low-bg: #e2e8dc; --sev-low-fg: #4c6042;
  --sev-none-bg: #e6e6e2; --sev-none-fg: #6b6b64;
  --shadow: 0 1px 2px rgba(26,34,42,.06), 0 4px 14px rgba(26,34,42,.05);
}
:root[data-theme="dark"] {
  --ink: #e9ebe4; --ink-soft: #b3bab0; --ink-faint: #838a80;
  --paper: #171b17; --paper-raised: #1f2420; --rule: #3a4038; --rule-strong: #4d554a;
  --accent: #dc9257; --accent-soft: #8a5a30; --data: #7fb4c2; --data-soft: #23414a;
  --sev-high-bg: #3a231c; --sev-high-fg: #e6a68f;
  --sev-mid-bg: #372e18; --sev-mid-fg: #e2c274;
  --sev-low-bg: #232b21; --sev-low-fg: #a7c197;
  --sev-none-bg: #26261f; --sev-none-fg: #9a9a8f;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 14px rgba(0,0,0,.35);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #e9ebe4; --ink-soft: #b3bab0; --ink-faint: #838a80;
    --paper: #171b17; --paper-raised: #1f2420; --rule: #3a4038; --rule-strong: #4d554a;
    --accent: #dc9257; --accent-soft: #8a5a30; --data: #7fb4c2; --data-soft: #23414a;
    --sev-high-bg: #3a231c; --sev-high-fg: #e6a68f;
    --sev-mid-bg: #372e18; --sev-mid-fg: #e2c274;
    --sev-low-bg: #232b21; --sev-low-fg: #a7c197;
    --sev-none-bg: #26261f; --sev-none-fg: #9a9a8f;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 14px rgba(0,0,0,.35);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font-family: 'Schola', Georgia, 'Times New Roman', serif; font-size: 16px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; }
::selection { background: var(--accent-soft); color: var(--ink); }
.sheet { max-width: 860px; margin: 0 auto; padding: 0 28px 80px; }
.masthead { border-bottom: 3px solid var(--ink); padding: 34px 0 18px; margin-bottom: 6px; }
.eyebrow { font-family: 'HerosCn', 'Arial Narrow', sans-serif; font-size: 12px; font-weight: 700;
  letter-spacing: .12em; text-transform: uppercase; color: var(--accent);
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 14px; }
.eyebrow::before { content: ""; display: inline-block; width: 22px; height: 2px;
  background: var(--accent); transform: translateY(-3px); }
h1.title { font-family: 'HerosCn', 'Arial Narrow', sans-serif; font-weight: 700;
  font-size: clamp(28px, 4.6vw, 40px); line-height: 1.08; letter-spacing: -.01em;
  margin: 0 0 8px; text-wrap: balance; color: var(--ink); }
.subtitle { font-family: 'Schola', serif; font-style: italic; font-size: 17px;
  color: var(--ink-soft); margin: 0 0 20px; max-width: 62ch; }
.meta-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 14px 22px; padding-top: 16px; border-top: 1px solid var(--rule); }
.meta-item .k { font-family: 'HerosCn', sans-serif; font-size: 10px; font-weight: 700;
  letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint);
  display: block; margin-bottom: 3px; }
.meta-item .v { font-family: 'MonoInstr', 'Courier New', monospace; font-size: 13.5px;
  color: var(--ink); font-variant-numeric: tabular-nums; }
.callout { background: var(--paper-raised); border: 1px solid var(--rule);
  border-left: 4px solid var(--accent); border-radius: 3px; padding: 22px 26px;
  margin: 30px 0; box-shadow: var(--shadow); }
.callout h2 { font-family: 'HerosCn', sans-serif; font-size: 13px; font-weight: 700;
  letter-spacing: .09em; text-transform: uppercase; color: var(--accent); margin: 0 0 12px; }
.callout p { margin: 0 0 12px; } .callout p:last-child { margin-bottom: 0; }
.callout ul { margin: 10px 0 12px; padding-left: 22px; } .callout li { margin-bottom: 6px; }
.disclaimer { background: var(--sev-none-bg); border: 1px dashed var(--rule-strong);
  border-radius: 3px; padding: 16px 20px; margin: 26px 0; font-size: 14.5px; color: var(--ink-soft); }
.disclaimer strong { color: var(--ink); }
section.doc { margin: 52px 0; }
.sec-head { display: flex; align-items: baseline; gap: 14px; border-bottom: 1px solid var(--rule-strong);
  padding-bottom: 10px; margin-bottom: 20px; }
.sec-num { font-family: 'MonoInstr', monospace; font-size: 13px; color: var(--data);
  font-weight: 700; flex-shrink: 0; }
.sec-head h2 { font-family: 'HerosCn', sans-serif; font-weight: 700; font-size: 21px;
  letter-spacing: -.005em; margin: 0; color: var(--ink); }
h3 { font-family: 'HerosCn', sans-serif; font-weight: 700; font-size: 15.5px;
  letter-spacing: .01em; margin: 26px 0 10px; color: var(--ink); }
p { margin: 0 0 14px; max-width: 68ch; } .wide p { max-width: none; } a { color: var(--data); }
.table-wrap { overflow-x: auto; margin: 18px 0; border: 1px solid var(--rule); border-radius: 3px; }
table { width: 100%; border-collapse: collapse; font-size: 13.2px; min-width: 500px; table-layout: fixed; }
table col.c-loc { width: 24%; } table col.c-lvl { width: 12%; }
.region-tag { font-size: 11px; color: var(--ink-faint); font-family: 'HerosCn', sans-serif; letter-spacing: .01em; }
thead th { font-family: 'HerosCn', sans-serif; font-size: 10.5px; font-weight: 700;
  letter-spacing: .06em; text-transform: uppercase; color: var(--ink-faint);
  text-align: left; padding: 10px 12px; background: var(--paper-raised);
  border-bottom: 1px solid var(--rule-strong); }
thead th.num { text-align: right; }
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; } tbody tr:hover { background: var(--paper-raised); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; font-family: 'MonoInstr', monospace; font-size: 13px; }
td.mono { font-family: 'MonoInstr', monospace; font-size: 12.5px; color: var(--ink-soft); }
.chip { display: inline-block; padding: 3px 9px; border-radius: 20px; font-family: 'HerosCn', sans-serif;
  font-size: 10.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; white-space: nowrap; }
.s-high { background: var(--sev-high-bg); color: var(--sev-high-fg); }
.s-mid  { background: var(--sev-mid-bg);  color: var(--sev-mid-fg); }
.s-low  { background: var(--sev-low-bg);  color: var(--sev-low-fg); }
.s-none { background: var(--sev-none-bg); color: var(--sev-none-fg); }
figure.map { margin: 22px 0; } figure.map img { width: 100%; display: block; border: 1px solid var(--rule); border-radius: 3px; }
figcaption { font-size: 12.5px; color: var(--ink-faint); margin-top: 8px; font-family: 'HerosCn', sans-serif; letter-spacing: .01em; }
figcaption .cap-num { color: var(--data); font-weight: 700; }
.loc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 18px; margin: 22px 0; }
.loc-card { border: 1px solid var(--rule); border-radius: 4px; overflow: hidden; background: var(--paper-raised);
  box-shadow: var(--shadow); break-inside: avoid; }
.loc-card img { width: 100%; display: block; border-bottom: 1px solid var(--rule); }
.loc-card .body { padding: 12px 14px 14px; }
.loc-card .name-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 6px; }
.loc-card .name { font-family: 'HerosCn', sans-serif; font-weight: 700; font-size: 14.5px; }
.loc-card .region { font-size: 11.5px; color: var(--ink-faint); margin-bottom: 8px; }
.loc-card .stat-row { display: flex; justify-content: space-between; gap: 8px; font-size: 12px;
  font-family: 'MonoInstr', monospace; color: var(--ink-soft); padding-top: 4px; border-top: 1px solid var(--rule);
  white-space: nowrap; }
.loc-card .stat-row + .stat-row { border-top: none; padding-top: 2px; }
.arch-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px,1fr)); gap: 16px; margin: 18px 0; }
.arch-card { border: 1px solid var(--rule); border-radius: 4px; padding: 16px 18px; background: var(--paper-raised); }
.arch-card .arch-k { font-family:'HerosCn',sans-serif; font-size:10.5px; font-weight:700; letter-spacing:.08em;
  text-transform:uppercase; color: var(--data); margin-bottom:6px; }
.arch-card p { font-size: 13.5px; margin: 0; }
footer { margin-top: 70px; padding-top: 22px; border-top: 3px solid var(--ink); display: flex; flex-wrap: wrap;
  justify-content: space-between; gap: 18px; font-size: 12px; color: var(--ink-faint); }
footer .hash { font-family: 'MonoInstr', monospace; font-size: 11px; word-break: break-all; max-width: 420px; }
footer strong { color: var(--ink-soft); }
@media (max-width: 600px) { .sheet { padding: 0 16px 60px; } .meta-strip { grid-template-columns: repeat(2, 1fr); } }
@media print {
  body { background: #fff; font-size: 12px; } .sheet { max-width: none; padding: 0 4mm; }
  section.doc { margin: 22px 0; } section.doc h2, .sec-head { break-after: avoid; }
  .loc-card, .callout, figure.map, .arch-card { break-inside: avoid; }
  table, tbody tr { break-inside: avoid; } .table-wrap { overflow: visible; }
  table { font-size: 10.5px; min-width: 0; } thead th, tbody td { padding: 6px 7px; }
  .loc-grid { grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .loc-card .stat-row { font-size: 10.5px; } .loc-card .region { font-size: 10px; }
  a { color: inherit; text-decoration: none; } .masthead { border-bottom-color: #000; }
  .callout, .disclaimer, .arch-card, .loc-card { box-shadow: none; }
}
"""


def build_html(forecast, watchlist, images, loc_images, fonts, data_hash, now_utc):
    gen_stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = sorted(
        ((r["signal"].get("hazard_max_regional") if r["signal"].get("hazard_max_regional") is not None else -1,
          name, r) for name, r in watchlist.items()),
        key=lambda x: -x[0])

    dom_rows = ""
    for d in DOMAIN_ORDER:
        if d not in forecast:
            continue
        m = forecast[d]
        v = m["variants"]["regional-a5.5"]
        dom_rows += (f"<tr><td>{DOMAIN_LABELS[d]}</td><td class='num'>{m['iso0_mean_m']:.0f} m</td>"
                     f"<td class='num'>{m['snow_fraction_mean']*100:.0f}%</td>"
                     f"<td class='num'>{v['peak_prob']:.2f}</td><td class='num'>{v['n_crossings']}</td>"
                     f"<td class='mono'>{v['first_crossing'] or 'sin cruce'}</td></tr>")

    tbl_rows = ""
    for g, name, r in rows:
        label, region = LOC_LABELS.get(name, (name.replace("_", " ").title(), "—"))
        cls, txt = severity(g)
        s, p = r["signal"], r["proximity"]
        tbl_rows += (f"<tr><td><strong>{label}</strong><br><span class='region-tag'>{region}</span></td>"
                     f"<td><span class='chip {cls}'>{txt}</span></td>"
                     f"<td class='num'>{s['hazard_max_regional']:.2f}</td>"
                     f"<td class='mono'>{fmt_hour(s['first_crossing_fhour'])}</td>"
                     f"<td class='num'>{fmt_mm(s['rain_liquid_total_mm'])}</td>"
                     f"<td class='num'>{fmt_dist(p['min_distance_m'])}</td></tr>")

    loc_cards = ""
    for g, name, r in rows:
        if name not in loc_images:
            continue
        label, region = LOC_LABELS.get(name, (name.replace("_", " ").title(), "—"))
        cls, txt = severity(g)
        s, p = r["signal"], r["proximity"]
        rain3 = ", ".join(f"{v:.0f}" for v in r["rain3_mm_day"])
        loc_cards += (
            f"<div class='loc-card'><img src='data:image/png;base64,{loc_images[name]}' "
            f"alt='Footprint simulado de flujo detrítico en {label}' />"
            f"<div class='body'><div class='name-row'><span class='name'>{label}</span>"
            f"<span class='chip {cls}'>{txt}</span></div>"
            f"<div class='region'>{region} · ventana {r.get('size_km', 12)}&nbsp;km · "
            f"escenario 3 días: {rain3}&nbsp;mm/día</div>"
            f"<div class='stat-row'><span>Gatillo</span><span>{s['hazard_max_regional']:.2f}</span></div>"
            f"<div class='stat-row'><span>Dist. al footprint</span><span>{fmt_dist(p['min_distance_m'])}</span></div>"
            f"<div class='stat-row'><span>Anillo 0.5&nbsp;km cubierto</span>"
            f"<span>{fmt_pct(p['rings'].get('0.5km'))}</span></div></div></div>")

    n_high = sum(1 for g, _, _ in rows if g >= 0.8)
    cycle = forecast["chile-centro-sur"]["cycle"]

    body = f"""
<div class="sheet">
  <div class="masthead">
    <div class="eyebrow">Registro técnico interno · sistema nowcast</div>
    <h1 class="title">Vigilancia de remociones en masa y flujos detríticos<br>Evento hidrometeorológico del 15&ndash;20 de julio de 2026</h1>
    <p class="subtitle">Estado del pronóstico, observación y simulación al momento de generar este documento — dejado como constancia previa al desarrollo del evento.</p>
    <div class="meta-strip">
      <div class="meta-item"><span class="k">Generado</span><span class="v">{gen_stamp}</span></div>
      <div class="meta-item"><span class="k">Hora Chile</span><span class="v">{(now_utc.hour-4)%24:02d}:{now_utc.minute:02d} (UTC&minus;4)</span></div>
      <div class="meta-item"><span class="k">Ciclo GFS base</span><span class="v">{cycle}</span></div>
      <div class="meta-item"><span class="k">Dominios activos</span><span class="v">5 (2 nodos)</span></div>
      <div class="meta-item"><span class="k">Localidades en vigilancia</span><span class="v">{len(rows)}</span></div>
    </div>
  </div>

  <div class="callout">
    <h2>Resumen ejecutivo</h2>
    <p>Este documento fija en el tiempo el estado de un sistema experimental de nowcasting de geopeligros (motor <em>nowcast</em>) corriendo de forma autónoma sobre dos nodos del cluster doméstico del autor, en modo sombra, ante el pronóstico de un sistema frontal con río atmosférico extremo afectando Chile centro-sur. Su propósito es doble: servir de base para comparar el pronóstico contra lo efectivamente ocurrido una vez cerrado el evento, y dejar constancia verificable — con huella de integridad y timestamp — de qué se sabía y cuándo.</p>
    <ul>
      <li>El pronóstico GFS del ciclo <strong>{cycle}</strong> muestra excedencia sostenida del umbral lluvia&ndash;duración regional, concentrada en la precordillera y cordillera de Ñuble, Biobío, Araucanía y Los Ríos.</li>
      <li>El cruce con los modelos de susceptibilidad de terreno identifica <strong>{n_high} localidades en nivel ALTO</strong> de vigilancia combinada.</li>
      <li>Simulaciones de flujo detrítico por agentes (parámetros de Atacama 2015, sin calibrar localmente) muestran footprints ilustrativos que en algunos casos alcanzan a menos de 20&nbsp;m del centro poblado.</li>
      <li><strong>Nada de este documento es un aviso oficial.</strong> Las fuentes de verdad para decisiones de protección civil son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.</li>
    </ul>
  </div>

  <div class="disclaimer"><strong>Sobre la naturaleza de este registro.</strong> Todos los números provienen de un sistema experimental, sin validación operacional, con susceptibilidad de terreno agregada por analogía (transferencia entre cuencas en dos capas) y parámetros de simulación calibrados para un régimen árido distinto (Atacama). Se documentan como fueron calculados el {gen_stamp}, antes de que el evento se desarrollara.</div>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">01</span><h2>Qué pronosticaba el sistema en este momento</h2></div>
    <p>El motor consume el pronóstico determinístico GFS (0.25&deg;, NOAA/NCEP) cuatro veces al día, particiona la precipitación en líquida y sólida según la altura de la isoterma 0&deg;C del propio modelo contra su propia orografía, y corre el umbral lluvia&ndash;duración regional (intercepto <em>a</em>=5.5, calibrado contra el inventario histórico del río Maipo) sobre una ventana de 120&nbsp;h.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Dominio</th><th class="num">Isoterma 0&deg;C</th><th class="num">Fracción nieve</th><th class="num">Pico de índice</th><th class="num">Cruces</th><th>Primer cruce (UTC)</th></tr></thead>
      <tbody>{dom_rows}</tbody>
    </table></div>
    <figure class="map">
      <img src="data:image/png;base64,{images['mapa_panorama']}" alt="Mapa de excedencia del gatillo lluvia-duración pronosticado" />
      <figcaption><span class="cap-num">Figura 1.</span> Índice de excedencia del umbral I&ndash;D regional pronosticado para el horizonte completo de 120&nbsp;h (ciclo {cycle}).</figcaption>
    </figure>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">02</span><h2>Dónde el terreno amplifica la señal climática</h2></div>
    <p>El índice anterior describe sólo la mitad climática del peligro. Se cruza celda a celda con modelos de susceptibilidad de remociones en masa entrenados por el autor (RandomForest / XGBoost, 15 cuencas nacionales). Dos cuencas sin modelo propio se completaron por <strong>transferencia</strong> del modelo del Biobío — decisión metodológica explícita, rotulada en los metadatos como <em>transferencia sin validar</em>.</p>
    <figure class="map">
      <img src="data:image/png;base64,{images['mapa_overlay']}" alt="Mapa de cruce entre gatillo climático y susceptibilidad de terreno" />
      <figcaption><span class="cap-num">Figura 2.</span> Producto del índice de gatillo climático por la susceptibilidad de terreno agregada a 0.1&deg;.</figcaption>
    </figure>
  </section>

  <section class="doc wide">
    <div class="sec-head"><span class="sec-num">03</span><h2>Localidades bajo vigilancia</h2></div>
    <p>Localidades seleccionadas por criterio del autor, ordenadas por índice de gatillo climático puntual. Para cada una se corrió una simulación de flujo detrítico por agentes (modelo <code>debris-flow</code> sobre <code>swarm-abm</code>, parámetros de Atacama 2015 — análogo ilustrativo, no predicción calibrada) forzada con la lluvia y la isoterma del ciclo vigente.</p>
    <div class="table-wrap"><table>
      <colgroup><col class="c-loc"><col class="c-lvl"><col><col><col><col></colgroup>
      <thead><tr><th>Localidad</th><th>Nivel</th><th class="num">Gatillo</th><th>Cruce</th><th class="num">Lluvia 120h</th><th class="num">Footprint</th></tr></thead>
      <tbody>{tbl_rows}</tbody>
    </table></div>
    <h3>Footprint simulado por localidad</h3>
    <p>Ventana de 12&nbsp;km simulada, con el centro del poblado (estrella) y anillos de referencia a 0.5 / 1 / 2&nbsp;km. El área sombreada es el footprint del flujo simulado.</p>
    <div class="loc-grid">{loc_cards}</div>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">04</span><h2>Arquitectura del sistema</h2></div>
    <p>El sistema opera en tres capas independientes, cada una con su propia cadencia y grado de validación.</p>
    <div class="arch-grid">
      <div class="arch-card"><div class="arch-k">Observación (cada 30 min)</div><p>Fusión por noisy&ndash;OR de IMERG Early NRT, GOES&ndash;East QPE y pluviómetros DMC, sobre 5 dominios geográficos, replicada en dos nodos independientes.</p></div>
      <div class="arch-card"><div class="arch-k">Pronóstico (4&times;/día)</div><p>GFS 0.25&deg; + partición por isoterma 0&deg;C, horizonte 120&nbsp;h, dos variantes de umbral. Es la capa que sustenta este informe.</p></div>
      <div class="arch-card"><div class="arch-k">Simulación (bajo demanda)</div><p>Modelo de agentes de flujo detrítico sobre DEM Copernicus y susceptibilidad real, disparado sobre localidades de interés.</p></div>
    </div>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">05</span><h2>Metodología y limitaciones honestas</h2></div>
    <p>Ninguna capa de este sistema ha pasado por un ciclo de validación operacional:</p>
    <ul>
      <li><strong>Pronóstico determinístico, sin ensamble.</strong> No hay medida de incertidumbre ni probabilidad calibrada.</li>
      <li><strong>Umbral regional calibrado en una sola cuenca</strong> (Maipo, 1979&ndash;2016) y extrapolado al país.</li>
      <li><strong>Dos capas de susceptibilidad por transferencia sin validar</strong>, rotuladas explícitamente en los metadatos.</li>
      <li><strong>Simulaciones con parámetros de un régimen distinto</strong> (Atacama árido) aplicados a cuencas templadas/húmedas.</li>
      <li><strong>Resolución del forzante</strong> (~25&nbsp;km GFS) muy por debajo de la escala de las quebradas reales.</li>
      <li><strong>Sin verificación operacional a la fecha de este documento.</strong></li>
    </ul>
    <p>Este documento se declara explícitamente como <strong>material de registro y de investigación personal</strong>, no como un producto de alerta temprana operacional.</p>
  </section>

  <footer>
    <div><div><strong>Documento generado automáticamente</strong> a partir del estado en vivo del sistema <code>nowcast</code>.</div>
    <div>Fuentes de aviso oficial: SENAPRED &middot; SERNAGEOMIN &middot; Dirección Meteorológica de Chile.</div></div>
    <div><div><strong>Huella de integridad de los datos (SHA&ndash;256)</strong></div><div class="hash">{data_hash}</div></div>
  </footer>
</div>
"""
    font_faces = FONT_FACES_TPL.format(**fonts)
    return f"<title>Vigilancia de remociones en masa — Evento 15–20 jul 2026</title>\n<style>\n{font_faces}\n{CSS}\n</style>\n{body}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-forecast/informes"))
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    runouts_dir = os.path.realpath(os.path.expanduser(a.runouts))
    os.makedirs(a.out, exist_ok=True)

    forecast = {}
    for d in DOMAIN_ORDER:
        p = f"{forecast_dir}/{d}/forecast.json"
        if os.path.exists(p):
            forecast[d] = json.load(open(p))
    if "chile-centro-sur" not in forecast:
        sys.exit(f"falta el dominio macro chile-centro-sur en {forecast_dir}")

    watchlist = {}
    for d in sorted(glob.glob(os.path.join(runouts_dir, "*"))):
        p = os.path.join(d, "resumen.json")
        if os.path.exists(p):
            watchlist[os.path.basename(d)] = json.load(open(p))
    if not watchlist:
        sys.exit(f"sin corridas en {runouts_dir} — correr runout_localidad.py primero")

    data_hash = hashlib.sha256(
        (json.dumps(forecast, sort_keys=True) + json.dumps(watchlist, sort_keys=True)).encode()
    ).hexdigest()

    work = os.path.join(a.out, ".build")
    img_dir = os.path.join(work, "img")
    os.makedirs(img_dir, exist_ok=True)
    print("generando mapas...")
    make_maps(forecast_dir, runouts_dir, watchlist, img_dir)

    images = {n: b64(f"{img_dir}/{n}.png") for n in ("mapa_panorama", "mapa_overlay")}
    loc_images = {name: b64(f"{img_dir}/localidades/{name}.png")
                  for name in watchlist if os.path.exists(f"{img_dir}/localidades/{name}.png")}
    fonts = {n.replace("-", "_"): b64(f"{HERE}/fonts/{n}.woff2") for n in
             ("schola-reg", "schola-bold", "schola-ital", "heroscn-reg", "heroscn-bold", "mono-reg", "mono-bold")}

    now = datetime.now(timezone.utc)
    html = build_html(forecast, watchlist, images, loc_images, fonts, data_hash, now)

    stamp = now.strftime("%Y%m%dT%H%MZ")
    html_path = os.path.join(a.out, f"registro-evento-{stamp}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"HTML: {html_path} ({os.path.getsize(html_path)/1024:.0f} KB)")
    print(f"hash de integridad: {data_hash}")

    shutil.rmtree(work, ignore_errors=True)

    if not a.no_pdf:
        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        if chrome:
            pdf_path = os.path.join(a.out, f"registro-evento-{stamp}.pdf")
            subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                            f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
                            f"file://{html_path}"], capture_output=True)
            if os.path.exists(pdf_path):
                print(f"PDF: {pdf_path} ({os.path.getsize(pdf_path)/1024:.0f} KB)")
        else:
            print("(sin Chrome/Chromium disponible — solo se generó el HTML)")


if __name__ == "__main__":
    main()
