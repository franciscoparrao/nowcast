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

Idempotente por ciclo (sexta etapa de analysis_all.sh): deja una marca
`.last_cycle` en --out con el ciclo del último registro generado y, si el ciclo
vigente ya está registrado, se salta —para que re-correr analysis_all no apile
un informe duplicado del mismo ciclo (mismo forecast/watchlist/runouts → misma
constancia). --forzar lo ignora. La cadencia normal (un ciclo nuevo por corrida
del timer) sí genera un registro por ciclo.

Uso:
    python3 monitor/report/build_registro.py
    python3 monitor/report/build_registro.py --forecast ~/nowcast-forecast/latest \\
        --out ~/nowcast-forecast/informes [--forzar]
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
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

HERE = os.path.dirname(os.path.abspath(__file__))

DOMAIN_LABELS = {
    "chile-centro-sur": "Barrido macro (Coquimbo → Los Ríos)",
    "atacama": "Atacama", "coquimbo": "Coquimbo", "rm": "Región Metropolitana",
    "nuble-biobio": "Ñuble – Biobío", "araucania": "Araucanía",
}
DOMAIN_ORDER = ["chile-centro-sur", "atacama", "coquimbo", "rm", "nuble-biobio", "araucania"]

def load_loc_labels(forecast_dir):
    """name -> (label, region), leída de watchlist.json (selección objetiva de
    select_watchlist.py) — ya no una lista hardcodeada. Si watchlist.json no
    existe (corridas legacy), cae a title-case del nombre del directorio."""
    p = os.path.join(forecast_dir, "watchlist.json")
    if not os.path.exists(p):
        return {}
    wl = json.load(open(p))
    labels = {s["name"]: (s["label"].title(), s["region"].title()) for s in wl["selected"]}
    for s in wl.get("regional_check_norte", {}).get("localidades", []):
        labels[s["name"]] = (s["label"], s["region"].title() + " · chequeo Norte")
    return labels


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


def f2(x):
    """Dos decimales, o '—' si el valor falta (None/NaN). Barrido 2026-09-28: el
    registro se caía formateando un gatillo None en el chequeo norte, y las
    mismas tablas formateaban directo otros cinco campos que pueden faltar."""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:.2f}"


def fmt_mm(x):
    return "—" if x is None else f"{x:.0f} mm"


def fmt_dist(x):
    if x is None:
        return "—"
    return f"{x} m" if x < 1000 else f"{x/1000:.1f} km"


SCL = ZoneInfo("America/Santiago")   # huso REAL de cada fecha (UTC−4 / UTC−3)
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
NIVEL_ALERTA = 0.5


def hora_chile(dt):
    """'HH:MM (UTC−3)' con el offset vigente en esa fecha. Hasta 2026-09-28 esto
    era `(hour-4)%24` fijo: desde el cambio de huso del 6-sep cada registro
    declaraba una hora de Chile corrida en una hora."""
    loc = dt.astimezone(SCL)
    off = int(loc.utcoffset().total_seconds() // 3600)
    return f"{loc:%H:%M} (UTC&minus;{abs(off)})"


def _utc(stamp):
    """'20260928T0700' o '2026-10-02T03:00Z' → datetime UTC (None si no parsea)."""
    for fmt, txt in (("%Y%m%dT%H%M", stamp), ("%Y-%m-%dT%H:%M", (stamp or "").rstrip("Z")[:16])):
        try:
            return datetime.strptime(txt, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def fecha_chile(dt, con_hora=False):
    loc = dt.astimezone(SCL)
    base = f"{loc.day} {_MESES[loc.month - 1]}"
    return f"{base} {loc:%H:%M}" if con_hora else base


def rango_ventana(forecast):
    """Ventana de 120 h del ciclo en fechas de Chile ('28 sep – 3 oct 2026')."""
    for d in ("chile-centro-sur", *forecast):
        v = (forecast.get(d) or {}).get("ventana") or {}
        a, b = _utc(v.get("desde")), _utc(v.get("hasta"))
        if a and b:
            return f"{fecha_chile(a)} &ndash; {fecha_chile(b)} {b.astimezone(SCL).year}"
    return "ventana de 120&nbsp;h"


def plural(n, uno, varios):
    return f"{n} {uno if n == 1 else varios}"


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
                           xlocs=np.arange(-76, -68, 1), ylocs=np.arange(-41, -25, 1))
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

    hz_path = f"{forecast_dir}/chile-centro-sur/hazard_max_worst_available.tif"
    if not os.path.exists(hz_path):
        hz_path = f"{forecast_dir}/chile-centro-sur/hazard_max_regional-a5.5.tif"
        hz_subtitle = "Ciclo GFS base · horizonte 120 h · enmascarado a tierra"
    else:
        hz_subtitle = "Peor escenario disponible entre GFS, ECMWF e ICON · horizonte 120 h"
    xs_path = f"{forecast_dir}/chile-centro-sur/hazard_x_susc_max.tif"
    cycle = json.load(open(f"{forecast_dir}/chile-centro-sur/forecast.json"))["cycle"]
    panel(hz_path, "Excedencia del gatillo lluvia–duración pronosticada",
          hz_subtitle,
          "Índice de peligro pronosticado (excedencia I–D, umbral regional)",
          "mapa_panorama", marker_by_hazard=True)
    panel(xs_path, "Dónde el gatillo cae sobre terreno susceptible",
          "Mosaico de capas de susceptibilidad (propias + transferencias)",
          "Gatillo climático × susceptibilidad de terreno (agregado 0.1°, MAX)",
          "mapa_overlay", marker_by_hazard=False)

    comuna_geojson = f"{forecast_dir}/comuna_impact/comuna_impact.geojson"
    if os.path.exists(comuna_geojson):
        import geopandas as gpd
        cg = gpd.read_file(comuna_geojson)
        # Extent propio: el de los otros paneles se corta en -29°S (diseñado
        # para el barrido macro chile-centro-sur) y dejaría fuera casi toda
        # Atacama, justo la región que se agregó a este cruce.
        extent_comuna = [-74.3, -68.7, -40.4, -25.5]
        fig = plt.figure(figsize=(7.4, 11.6), dpi=200)
        ax = plt.axes([0.09, 0.07, 0.87, 0.87], projection=ccrs.PlateCarree())
        basemap(ax, extent_comuna)
        sev_none = "#c9c6ba"
        for _, row in cg.iterrows():
            v = row["peak_worst"]
            color = sev_none if v is None or (isinstance(v, float) and np.isnan(v)) else HAZARD_CMAP(v)
            geoms = [row.geometry] if row.geometry.geom_type == "Polygon" else list(row.geometry.geoms)
            ax.add_geometries(geoms, crs=ccrs.PlateCarree(), facecolor=color,
                               edgecolor=INK, linewidth=0.12, zorder=3, alpha=0.92)
        sm = plt.cm.ScalarMappable(cmap=HAZARD_CMAP, norm=mcolors.Normalize(0, 1))
        cax = fig.add_axes([0.14, 0.035, 0.72, 0.013])
        cb = plt.colorbar(sm, cax=cax, orientation="horizontal")
        cb.set_label("Peor de los tres modelos (GFS, ECMWF, ICON) — gris = sin dato", fontsize=7.3, color=INK)
        cb.ax.tick_params(labelsize=6.3, color=INK)
        cb.outline.set_edgecolor(RULE)
        fig.text(0.09, 0.965, "Gatillo por comuna, peor escenario de los tres modelos",
                  fontsize=11.5, color=INK, fontweight="bold", ha="left")
        fig.text(0.09, 0.945, "Límite comunal oficial (Censo 2024) · III Atacama a XIV Los Ríos + Ñuble",
                  fontsize=8, color="#5a6259", ha="left")
        plt.savefig(f"{out_img}/mapa_comunas.png", facecolor=PAPER)
        plt.close(fig)

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
.chip-domain { background: var(--data-soft); color: var(--data); border: 1px dashed var(--data); }
/* fenómeno (fan-apex): contorno, no relleno — no es un nivel de severidad ni un
   aviso de calidad, es QUÉ fenómeno es. Usa las variables de tema para que
   ambos modos (claro/oscuro) queden legibles. */
.chip-fenom-aluvion { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
.chip-fenom-crecida { background: transparent; color: var(--data); border: 1px solid var(--data); }
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
.loc-card .stat-row.implausible { color: var(--sev-none-fg); font-weight: 700; border-top: 1px dashed var(--rule-strong); margin-top: 4px; padding-top: 6px; }
.loc-card .stat-row.domain-warn { color: var(--data); font-weight: 700; border-top: 1px dashed var(--rule-strong); margin-top: 4px; padding-top: 6px; }
.loc-card .implaus-reason { font-size: 10.5px; color: var(--ink-faint); line-height: 1.4; padding: 2px 0 4px; white-space: normal; }
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


DOMAIN_ENSEMBLE_LABEL = {"atacama": "Atacama", "coquimbo": "Coquimbo", "rm": "Región Metropolitana"}


def lista_chip(nombres):
    """Chip de a qué lista(s) pertenece la comuna. Las dos listas responden
    preguntas distintas y separadas a propósito (ver select_watchlist): PELIGRO
    = dónde el terreno amplifica más la señal, EXPOSICIÓN = dónde hay más gente
    bajo peligro alto. Lo que aparece SOLO en una es lo informativo."""
    if not nombres:
        return ""
    if "peligro" in nombres and "exposicion" in nombres:
        return "<span class='chip' style='background:#dfe6df;color:#33502f;'>ambas</span>"
    if "exposicion" in nombres:
        return "<span class='chip' style='background:#e7dcef;color:#5a3a78;'>exposición</span>"
    return "<span class='chip' style='background:#f0e6d2;color:#6b5418;'>peligro</span>"


def resumen_modelos(forecast, cycle):
    """Frase del resumen ejecutivo sobre GFS/ECMWF/ICON, calculada del ciclo.

    Reemplaza (2026-09-28) un párrafo fijo del evento de julio ("Ñuble, Biobío,
    Araucanía y Los Ríos… ICON levanta el Huasco") que se repitió en cada
    registro durante dos meses, dijera lo que dijera el pronóstico.
    """
    regionales = [d for d in DOMAIN_ORDER if d in forecast and d != "chile-centro-sur"]

    def pico(d, modelo):
        m = forecast[d] if modelo == "gfs" else forecast[d].get(modelo, {})
        v = (m.get("variants") or {}).get("regional-a5.5") or {}
        return v.get("peak_prob")

    def lista(ds):
        return ", ".join(f"{DOMAIN_LABELS[d]} ({pico(d, 'gfs'):.2f})" for d in ds)

    gfs_alto = [d for d in regionales if (pico(d, "gfs") or 0) >= NIVEL_ALERTA]
    macro = "chile-centro-sur" in forecast and (pico("chile-centro-sur", "gfs") or 0) >= NIVEL_ALERTA
    if gfs_alto or macro:
        cruces = [_utc(forecast[d]["variants"]["regional-a5.5"].get("first_crossing"))
                  for d in gfs_alto + (["chile-centro-sur"] if macro else [])]
        cruces = [c for c in cruces if c]
        donde = lista(gfs_alto) if gfs_alto else "el barrido macro, fuera de los dominios regionales"
        txt = (f"El pronóstico GFS del ciclo <strong>{cycle}</strong> cruza el nivel de alerta "
               f"({NIVEL_ALERTA}) del umbral lluvia&ndash;duración regional en {donde}")
        if cruces:
            txt += f"; primer cruce el {fecha_chile(min(cruces), con_hora=True)} (hora de Chile)"
        txt += "."
    else:
        top = max((d for d in forecast), key=lambda d: pico(d, "gfs") or 0)
        txt = (f"El pronóstico GFS del ciclo <strong>{cycle}</strong> no cruza el nivel de alerta "
               f"({NIVEL_ALERTA}) en ningún dominio; su pico es {(pico(top, 'gfs') or 0):.2f} en "
               f"{DOMAIN_LABELS[top]}.")
    if not any("ecmwf" in forecast[d] for d in forecast):
        txt += " ECMWF no está disponible para este ciclo."
    else:
        ec = [d for d in regionales if (pico(d, "ecmwf") or 0) >= NIVEL_ALERTA]
        txt += (f" ECMWF lo cruza en {', '.join(DOMAIN_LABELS[d] for d in ec)}." if ec
                else " ECMWF no cruza el nivel de alerta en ningún dominio regional.")
    if any("icon" in forecast[d] for d in forecast):
        solo_icon = [d for d in regionales
                     if (pico(d, "icon") or 0) >= NIVEL_ALERTA and (pico(d, "gfs") or 0) < NIVEL_ALERTA]
        if solo_icon:
            txt += (" ICON lo cruza además donde GFS no: "
                    + ", ".join(f"{DOMAIN_LABELS[d]} ({pico(d, 'icon'):.2f} vs {pico(d, 'gfs'):.2f})"
                                for d in solo_icon)
                    + " &mdash; léase como desacuerdo entre modelos (secc. 01).")
    return txt


def resumen_runouts(rows, loc_labels):
    """Frase sobre los footprints simulados, con la distancia REAL más corta."""
    dist = [(r["proximity"].get("min_distance_m"), name) for _, name, r in rows
            if (r.get("proximity") or {}).get("min_distance_m") is not None]
    if not dist:
        return ("Este ciclo no tiene footprints de flujo detrítico simulados para las localidades "
                "en vigilancia.")
    dmin, quien = min(dist)
    cerca = sum(1 for dd, _ in dist if dd < 500)
    label = loc_labels.get(quien, (quien.replace("_", " ").title(), ""))[0]
    cercania = (f"el de {label} alcanza el centro poblado" if dmin == 0 else
                f"el más cercano queda a {fmt_dist(dmin)} del centro poblado ({label})")
    return ("Simulaciones de flujo detrítico por agentes (parámetros de Atacama 2015, sin calibrar "
            f"localmente) dan footprints ilustrativos: {cercania}; "
            f"{cerca} de {len(dist)} localidades {'tiene' if cerca == 1 else 'tienen'} footprint a "
            "menos de 500&nbsp;m.")


def build_html(forecast, watchlist, images, loc_images, fonts, data_hash, now_utc, comuna=None,
               ensemble=None, gefs=None, loc_labels=None, norte_check=None, runout_ensemble=None,
               en_lista=None, dos_listas=False, solo_exposicion=None, solo_peligro=None):
    runout_ensemble = runout_ensemble or {}
    loc_labels = loc_labels or {}
    en_lista = en_lista or {}
    solo_exposicion = solo_exposicion or []
    solo_peligro = solo_peligro or []
    gen_stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = sorted(
        ((r["signal"].get("hazard_max_regional") if r["signal"].get("hazard_max_regional") is not None else -1,
          name, r) for name, r in watchlist.items()),
        key=lambda x: -x[0])

    dom_rows = ""
    any_ecmwf = any("ecmwf" in forecast[d] for d in DOMAIN_ORDER if d in forecast)
    any_icon = any("icon" in forecast[d] for d in DOMAIN_ORDER if d in forecast)
    for d in DOMAIN_ORDER:
        if d not in forecast:
            continue
        m = forecast[d]
        v = m["variants"]["regional-a5.5"]
        ecmwf_cell = ""
        if any_ecmwf:
            ev = m.get("ecmwf", {}).get("variants", {}).get("regional-a5.5")
            ecmwf_cell = f"<td class='num'>{f2(ev.get('peak_prob'))}</td>" if ev else "<td class='num'>—</td>"
        icon_cell = ""
        if any_icon:
            iv = m.get("icon", {}).get("variants", {}).get("regional-a5.5")
            icon_cell = f"<td class='num'>{f2(iv.get('peak_prob'))}</td>" if iv else "<td class='num'>—</td>"
        dom_rows += (f"<tr><td>{DOMAIN_LABELS[d]}</td><td class='num'>{m['iso0_mean_m']:.0f} m</td>"
                     f"<td class='num'>{m['snow_fraction_mean']*100:.0f}%</td>"
                     f"<td class='num'>{f2(v.get('peak_prob'))}</td>{ecmwf_cell}{icon_cell}"
                     f"<td class='num'>{v['n_crossings']}</td>"
                     f"<td class='mono'>{v['first_crossing'] or 'sin cruce'}</td></tr>")

    def plaus_chip(r):
        """Chip 'NO PLAUSIBLE' cuando runout_localidad.py marcó el footprint
        como esparcimiento difuso sin canalización real (ver guard 2026-07-17
        en runout_localidad.py) — mismo criterio de honestidad que el guard
        de flood_localidad.py. Ausencia del campo (corridas legacy) no marca
        nada, no se asume plausible ni implausible."""
        if r.get("physically_plausible") is False:
            return "<span class='chip s-none' title=\"" + \
                "; ".join(r.get("implausibility_reasons", [])).replace('"', "&quot;") + \
                "\">NO PLAUSIBLE</span>"
        return ""

    def domain_chip(r):
        """Aviso 'sin calibrar aquí' — pregunta DISTINTA de plaus_chip: no es
        si la forma del footprint es creíble, es si hay algún evento real
        contra el cual se haya validado la ESCALA fuera de Atacama (ver nota
        DOMINIO DE CALIBRACIÓN en runout_localidad.py). Se muestra aunque la
        forma sea plausible."""
        if r.get("calibration_domain_warning"):
            note = (r.get("calibration_domain_note") or "").replace('"', "&quot;")
            return f"<span class='chip chip-domain' title=\"{note}\">sin calibrar aquí</span>"
        return ""

    def sinfp_chip(r):
        """Chip 'sin footprint' — CUARTO estado, distinto de plaus/domain: aquí
        el ABM no movilizó material (footprint vacío), así que no hay forma NI
        escala que interpretar. Sin este chip un footprint vacío se lee como
        'plausible, solo que no alcanza al pueblo' (queda sin NO PLAUSIBLE y con
        distancia —), cuando en comunas costeras/planas suele significar que el
        modelo de flujo detrítico no aplica. Ver footprint_vacio en
        runout_localidad.py. Ausencia del campo (corridas legacy) no marca."""
        if r.get("footprint_vacio"):
            note = (r.get("footprint_vacio_note") or "").replace('"', "&quot;")
            return f"<span class='chip s-none' title=\"{note}\">sin footprint</span>"
        return ""

    def fenomeno_chip(r):
        """QUÉ FENÓMENO es (aluvión de abanico vs crecida fluvial), por fan-apex
        — pregunta distinta de las tres anteriores, que son todas sobre la
        CALIDAD del footprint. Esta dice si el footprint de flujo detrítico es
        siquiera el modelo correcto para esta comuna. Viene de
        fanapex_localidades.py; ausencia del bloque (corridas anteriores a su
        cableado, o comuna en cuenca transfer) no marca nada."""
        fa = r.get("fan_apex")
        if not fa or not fa.get("disponible"):
            return ""
        clase, ro = fa.get("clase") or "", fa.get("runout_a_alcanzar_km")
        base = clase.split(" /")[0]
        det = (f"fan-apex: {clase}"
               + (f", el abanico más cercano alcanza el poblado a {ro:.1f} km de "
                  f"runout ({fa.get('banda')})" if ro is not None else "")
               + f". Por centroide (sesgado al cauce del valle) sería "
                 f"'{fa.get('centroide_clase')}'.").replace('"', "&quot;")
        if base == "crecida fluvial":
            # el ABM de flujo detrítico probablemente NO es el modelo del sitio
            return f"<span class='chip chip-fenom-crecida' title=\"{det}\">crecida, no aluvión</span>"
        return f"<span class='chip chip-fenom-aluvion' title=\"{det}\">aluvión de abanico</span>"

    col_lista = "<th>Criterio</th>" if dos_listas else ""
    col_lista_grp = "<col>" if dos_listas else ""
    dos_listas_nota = ""
    if dos_listas:
        se = ", ".join(solo_exposicion) or "—"
        sp = ", ".join(solo_peligro) or "—"
        dos_listas_nota = (
            "<div class=\"callout\"><h3>Dos criterios de selección, no uno</h3>"
            "<p>La watchlist se arma con <strong>dos</strong> listas del mismo universo de comunas, "
            "porque responden preguntas distintas y colapsarlas en un solo número borra la que "
            "importa. <span class='chip' style='background:#f0e6d2;color:#6b5418;'>peligro</span> "
            "= dónde el terreno amplifica más la señal climática (sesga a valles rurales chicos). "
            "<span class='chip' style='background:#e7dcef;color:#5a3a78;'>exposición</span> = dónde "
            "hay más población bajo peligro alto (umbral de gatillo×susceptibilidad, para que no "
            "dominen ciudades planas por puro tamaño). <span class='chip' style='background:#dfe6df;"
            "color:#33502f;'>ambas</span> cuando coinciden.</p>"
            f"<p><strong>Solo por exposición</strong> (las que un ranking de peligro puro dejaría "
            f"fuera pese a su población): {se}.<br>"
            f"<strong>Solo por peligro</strong> (terreno crítico, población baja): {sp}.</p>"
            "<p style='color:var(--ink-faint);'>Este cambio nació del evento de julio 2026: La "
            "Serena (250&nbsp;mil hab, 7 desaparecidos por aluvión en Pueblo Islón) quedaba 44ª por "
            "peligro puro y nunca entraba a la lista de 20, pese a que el sistema sí tenía señal ahí. "
            "El umbral está ajustado sobre ese caso &mdash; se reporta, no se afirma como definitivo.</p>"
            "</div>")
    tbl_rows = ""
    for g, name, r in rows:
        label, region = loc_labels.get(name, (name.replace("_", " ").title(), "—"))
        cls, txt = severity(g)
        s, p = r["signal"], r["proximity"]
        celda_lista = f"<td>{lista_chip(en_lista.get(name, []))}</td>" if dos_listas else ""
        tbl_rows += (f"<tr><td><strong>{label}</strong><br><span class='region-tag'>{region}</span></td>"
                     f"<td><span class='chip {cls}'>{txt}</span> {fenomeno_chip(r)} "
                     f"{plaus_chip(r)} {sinfp_chip(r)} {domain_chip(r)}</td>"
                     f"{celda_lista}"
                     f"<td class='num'>{f2(s.get('hazard_max_regional'))}</td>"
                     f"<td class='mono'>{fmt_hour(s['first_crossing_fhour'])}</td>"
                     f"<td class='num'>{fmt_mm(s['rain_liquid_total_mm'])}</td>"
                     f"<td class='num'>{fmt_dist(p['min_distance_m'])}</td></tr>")

    def ensemble_row(name):
        """Rango de área afectada entre GFS/ECMWF/ICON (ver runout_ensemble.py)
        — un footprint determinístico único esconde el desacuerdo real entre
        fuentes; esto lo muestra en vez de ocultarlo."""
        e = runout_ensemble.get(name)
        if not e:
            return ""
        def _estado(m):
            if m.get("footprint_vacio"):
                return "sin footprint"
            return "plausible" if m["physically_plausible"] else "no plausible"
        title = "; ".join(
            f"{src}: {m['affected_km2']:.1f} km² ({_estado(m)})"
            for src, m in e["members"].items()
        )
        n_vacio = e.get("n_vacio", 0)
        vacio_row = (f"<div class='stat-row'><span>Sin footprint en</span>"
                     f"<span>{n_vacio}/{e['n_members']} fuentes</span></div>") if n_vacio else ""
        return (f"<div class='stat-row' title=\"{title}\">"
                f"<span>Área afectada (rango {e['n_members']} fuentes)</span>"
                f"<span>{e['affected_km2_min']:.0f}&ndash;{e['affected_km2_max']:.0f} km²</span></div>"
                f"<div class='stat-row'><span>Plausible en</span>"
                f"<span>{e['n_plausible']}/{e['n_members']} fuentes</span></div>"
                f"{vacio_row}")

    loc_cards = ""
    for g, name, r in rows:
        if name not in loc_images:
            continue
        label, region = loc_labels.get(name, (name.replace("_", " ").title(), "—"))
        cls, txt = severity(g)
        s, p = r["signal"], r["proximity"]
        rain3 = ", ".join(f"{v:.0f}" for v in r["rain3_mm_day"])
        implaus = ""
        if r.get("physically_plausible") is False:
            implaus = (f"<div class='stat-row implausible'><span>⚠ No plausible</span></div>"
                       f"<div class='implaus-reason'>{'; '.join(r.get('implausibility_reasons', []))}</div>")
        sinfp_note = ""
        if r.get("footprint_vacio"):
            sinfp_note = (f"<div class='stat-row implausible'><span>⚠ Sin footprint</span></div>"
                          f"<div class='implaus-reason'>{r.get('footprint_vacio_note', '')}</div>")
        domain_note = ""
        if r.get("calibration_domain_warning"):
            domain_note = (f"<div class='stat-row domain-warn'><span>⚠ Sin calibrar aquí</span></div>"
                           f"<div class='implaus-reason'>{r.get('calibration_domain_note', '')}</div>")
        fenom_row = ""
        fa = r.get("fan_apex")
        if fa and fa.get("disponible"):
            ro = fa.get("runout_a_alcanzar_km")
            alcance = (f"{ro:.1f} km &middot; {fa.get('banda')}" if ro is not None
                       else "ningún abanico alcanza")
            fenom_row = (
                f"<div class='stat-row' title=\"{fa['nota'].replace(chr(34), '&quot;')}\">"
                f"<span>Fenómeno (fan-apex)</span><span>{fa.get('clase')}</span></div>"
                f"<div class='stat-row'><span>Abanico más cercano</span><span>{alcance}</span></div>")
            if fa.get("cambia_vs_centroide"):
                # el punto de todo el trabajo de fan-apex: mostrar que el
                # centroide decía otra cosa, no reemplazarlo en silencio
                fenom_row += (f"<div class='implaus-reason'>Por centroide urbano habría "
                              f"quedado como &laquo;{fa.get('centroide_clase')}&raquo; "
                              f"(enganche al cauce del valle).</div>")
        loc_cards += (
            f"<div class='loc-card'><img src='data:image/png;base64,{loc_images[name]}' "
            f"alt='Footprint simulado de flujo detrítico en {label}' />"
            f"<div class='body'><div class='name-row'><span class='name'>{label}</span>"
            f"<span class='chip {cls}'>{txt}</span></div>"
            f"<div class='region'>{region} · ventana {r.get('size_km', 12)}&nbsp;km · "
            f"escenario 3 días: {rain3}&nbsp;mm/día</div>"
            f"<div class='stat-row'><span>Gatillo</span><span>{f2(s.get('hazard_max_regional'))}</span></div>"
            f"<div class='stat-row'><span>Dist. al footprint</span><span>{fmt_dist(p['min_distance_m'])}</span></div>"
            f"<div class='stat-row'><span>Anillo 0.5&nbsp;km cubierto</span>"
            f"<span>{fmt_pct(p['rings'].get('0.5km'))}</span></div>"
            f"<div class='stat-row' title=\"{(r.get('mass_balance_note') or '').replace(chr(34), '&quot;')}\">"
            f"<span>Techo físico agua/sedimento</span>"
            f"<span>{r.get('mass_balance_ratio', 0)*100:.0f}%</span></div>"
            f"{ensemble_row(name)}{fenom_row}"
            f"{implaus}{sinfp_note}{domain_note}</div></div>")

    n_high = sum(1 for g, _, _ in rows if g >= 0.8)
    cycle = forecast["chile-centro-sur"]["cycle"]

    comuna_section = ""
    comuna_bullet = ""
    if comuna:
        region_rows = ""
        for r in sorted(comuna["por_region"], key=lambda x: -(x["n_alto"] / max(x["n_comunas"], 1))):
            frac_alto = r["n_alto"] / max(r["n_comunas"], 1)
            if frac_alto >= 0.5:
                cls, txt = "s-high", "ALTO"
            elif frac_alto >= 0.15 or r["n_medio"] > 0:
                cls, txt = "s-mid", "MEDIO"
            else:
                cls, txt = "s-low", "BAJO"
            pico = f"{r['peak_worst_region']:.2f}" if r["peak_worst_region"] is not None else "—"
            region_rows += (
                f"<tr><td>{r['REGION'].title()}</td><td class='num'>{r['n_comunas']}</td>"
                f"<td class='num'>{r['n_alto']}</td><td class='num'>{r['n_medio']}</td>"
                f"<td class='num'>{pico}</td>"
                f"<td class='num'>{r['poblacion']:,}</td>"
                f"<td><span class='chip {cls}'>{txt}</span></td></tr>")
        any_vuln = any(r.get("indice_vulnerabilidad") is not None for r in comuna["top_alto"])
        top_rows = ""
        for r in comuna["top_alto"][:25]:
            terr = f"{r['peak_terreno']:.2f}" if r["peak_terreno"] is not None else "s/d"
            pob = f"{r['poblacion']:,}" if r["poblacion"] is not None else "—"
            vuln_cell = ""
            if any_vuln:
                iv = r.get("indice_vulnerabilidad")
                am = r.get("tasa_adulto_mayor")
                iv_s = f"{iv:.2f}" if iv is not None else "—"
                am_s = f"{am*100:.0f}%" if am is not None else "s/d"
                vuln_cell = f"<td class='num'>{iv_s}</td><td class='num'>{am_s}</td>"
            top_rows += (
                f"<tr><td><strong>{r['COMUNA'].title()}</strong><br>"
                f"<span class='region-tag'>{r['REGION'].title()}</span></td>"
                f"<td class='num'>{f2(r.get('peak_gfs'))}</td><td class='num'>{f2(r.get('peak_ecmwf'))}</td>"
                f"<td class='num'>{r.get('peak_icon', float('nan')):.2f}</td><td class='num'>{terr}</td><td class='num'>{pob}</td>{vuln_cell}</tr>")
        map_fig = ""
        if "mapa_comunas" in images:
            map_fig = f"""<figure class="map">
      <img src="data:image/png;base64,{images['mapa_comunas']}" alt="Mapa de gatillo pronosticado por comuna, peor de los dos modelos" />
      <figcaption><span class="cap-num">Figura 3.</span> Peor escenario entre GFS, ECMWF e ICON por comuna (máximo dentro del polígono, grilla 0.1&deg;). Gris = comuna fuera de cobertura de terreno modelado, no "sin riesgo".</figcaption>
    </figure>"""
        comuna_section = f"""
  <section class="doc wide">
    <div class="sec-head"><span class="sec-num">03</span><h2>Comunas potencialmente afectadas (Atacama a Los Ríos)</h2></div>
    <p>Cruce del gatillo pronosticado con el límite comunal oficial (Censo 2024, capa <code>Comunal_CPV24</code>) y la población censal 2024 por comuna, para el tramo solicitado: III Región de Atacama a XIV Región de Los Ríos, incluida Ñuble. El nivel de cada comuna es el <strong>peor de los tres modelos</strong> (GFS, ECMWF, ICON) — no un promedio ni una elección de cuál modelo "tiene razón".</p>
    <p><strong>Cómo leer esto (limitaciones duras):</strong> el valor es el máximo dentro del polígono comunal a resolución ~10&nbsp;km — una comuna extensa con una sola esquina cordillerana puede marcar ALTO sin que la mayor parte de su territorio esté afectada. La población es el total censal de la comuna, no una estimación de personas realmente expuestas: es una cota superior gruesa, útil para priorizar dónde mirar primero, no para dimensionar una respuesta.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Región</th><th class="num">Comunas</th><th class="num">Alto</th><th class="num">Medio</th><th class="num">Pico</th><th class="num">Población</th><th>Nivel</th></tr></thead>
      <tbody>{region_rows}</tbody>
    </table></div>
    <h3>Comunas en nivel ALTO (top 25 por índice, de {comuna['n_alto']} en total)</h3>
    {"<p><strong>Vulnerabilidad</strong> (Censo 2024, microdato de vivienda): promedio de 4 tasas — vivienda precaria, hacinamiento, sin agua de red pública, sin alcantarillado. Se reporta <strong>separada</strong> del peligro, nunca multiplicada: el peligro es probabilidad física, la vulnerabilidad describe qué tan grave sería la consecuencia si el peligro se concreta — dos preguntas distintas. <strong>Adulto mayor</strong> aparece \"s/d\" en varias comunas: se detectó que el campo edad del microdato viene corrupto (valores negativos, ej. −66) para 51 comunas completas del barrido — se excluyen del cálculo en vez de reportar un 0% fabricado.</p>" if any_vuln else ""}
    <div class="table-wrap"><table>
      <thead><tr><th>Comuna</th><th class="num">Pico GFS</th><th class="num">Pico ECMWF</th><th class="num">Pico ICON</th><th class="num">Terreno</th><th class="num">Población</th>{"<th class='num'>Vulnerab.</th><th class='num'>Adulto mayor</th>" if any_vuln else ""}</tr></thead>
      <tbody>{top_rows}</tbody>
    </table></div>
    {map_fig}
  </section>"""
        comuna_bullet = (
            f"<li>El cruce comunal (III Atacama&ndash;XIV Los Ríos, Censo 2024) marca "
            f"<strong>{comuna['n_alto']} comunas en nivel ALTO</strong> (población censal "
            f"{comuna['poblacion_en_comunas_alto']:,}) y {comuna['n_medio']} en MEDIO, contando el "
            f"peor de los tres modelos &mdash; cifra de cota superior gruesa, no de expuestos reales "
            f"(ver secci&oacute;n 03).</li>")

    ecmwf_th = "<th class=\"num\">Pico ECMWF</th>" if any_ecmwf else ""
    icon_th = "<th class=\"num\">Pico ICON</th>" if any_icon else ""
    ecmwf_note = (
        "<p>Se agrega, a partir del ciclo 2026&ndash;07&ndash;14 12z, el modelo determinístico "
        "<strong>ECMWF HRES</strong> (0.25&deg;, Open Data, 2 ciclos/día) corriendo en paralelo e "
        "independiente de GFS &mdash; misma ventana I&ndash;D, mismo umbral regional, lluvia líquida "
        "obtenida restando directamente el campo de nieve (<code>sf</code>) del total (<code>tp</code>), "
        "sin necesitar la aproximación por isoterma que usa la ruta GFS. Donde ambos modelos difieren "
        "sustancialmente (columna <em>Pico ECMWF</em> muy por sobre <em>Pico GFS</em>), se lee como "
        "señal de que el consenso entre modelos es bajo en este ciclo &mdash; no que uno de los dos "
        "esté \"equivocado\".</p>" if any_ecmwf else "")
    icon_note = (
        "<p>También se agrega <strong>DWD ICON global</strong> (~13&nbsp;km nativo, Open Data) como "
        "tercera fuente determinística gratuita. ICON no reemplaza a GFS/ECMWF, pero en la prueba "
        "del 2026&ndash;07&ndash;15 fue la única fuente que reprodujo montos del orden DMC en Vallenar, "
        "Freirina y Huasco. Donde <em>Pico ICON</em> diverge de GFS/ECMWF, debe leerse como "
        "incertidumbre espacial/orográfica y no como pronóstico oficial.</p>" if any_icon else "")

    ensemble_note = ""
    if ensemble:
        rows_e = "".join(
            f"<li><strong>{DOMAIN_ENSEMBLE_LABEL.get(d, d)}</strong>: {v['n_members']} miembros, "
            f"{v['min_mm']:.0f}&ndash;{v['max_mm']:.0f}&nbsp;mm (mediana {v['median_mm']:.0f}&nbsp;mm) "
            f"de lluvia líquida acumulada en 120&nbsp;h; {v['frac_over_100mm']*100:.0f}% de los miembros "
            f"sobre 100&nbsp;mm.</li>"
            for d, v in ensemble.items())
        gefs_block = ""
        if gefs:
            rows_g = "".join(
                f"<li><strong>{DOMAIN_ENSEMBLE_LABEL.get(d, d)}</strong>: {v['n_members']} miembros, "
                f"{v['min_mm']:.0f}&ndash;{v['max_mm']:.0f}&nbsp;mm (mediana {v['median_mm']:.0f}&nbsp;mm) "
                f"de precipitación total; {v['frac_over_100mm']*100:.0f}% de los miembros sobre 100&nbsp;mm.</li>"
                for d, v in gefs.items())
            gefs_block = f"""
      <h3>Tercera fuente: ensemble GEFS (NOAA)</h3>
      <p>Familia de modelo y sistema de asimilación distintos a ECMWF &mdash; 31 miembros (control
      <code>gec00</code> + 30 perturbados), NOMADS. <strong>Ojo:</strong> el producto de superficie de
      GEFS no trae isoterma 0&deg;C, así que esta cifra es precipitación <strong>total</strong>
      (agua+nieve equivalente), no partida en líquida/sólida &mdash; no comparable número a número con
      las cifras de líquido de ECMWF, pero sí como chequeo independiente de orden de magnitud.</p>
      <ul>{rows_g}</ul>"""
        # miembros POR FAMILIA (cada dominio corre los mismos miembros; sumarlos
        # entre dominios contaría 3 veces cada corrida)
        n_ec = max(v["n_members"] for v in ensemble.values())
        n_ge = max((v["n_members"] for v in (gefs or {}).values()), default=0)
        todos = list(ensemble.values()) + list((gefs or {}).values())
        seco = min(todos, key=lambda v: v["min_mm"])
        familias = f"{n_ec} de ECMWF" + (f" y {n_ge} de GEFS" if n_ge else "")
        ens_resumen = (f"Se consultaron {n_ec + n_ge} miembros ({familias}) en "
                       f"{len(ensemble)} dominio(s); el más seco deja "
                       f"{seco['min_mm']:.0f}&nbsp;mm en 120&nbsp;h.")
        ensemble_note = f"""<div class="callout" style="margin-top:22px;">
      <h2>¿Es un outlier de una sola corrida? &mdash; chequeo de ensembles (ECMWF + GEFS)</h2>
      <p>Como chequeo de robustez se corrió el <strong>ensemble</strong> de ECMWF (stream
      <code>enfo</code>, 50 miembros perturbados, mismo ciclo, acumulado a 120&nbsp;h) para los
      dominios de mayor divergencia con GFS &mdash; no el CFS (modelo estacional, ~1&deg;, no resuelve
      una tormenta de días; no es la herramienta para esta pregunta).</p>
      <ul>{rows_e}</ul>
      {gefs_block}
      <p>{ens_resumen} Esto no confirma nada sobre lo que ocurrirá &mdash; sigue siendo pronóstico,
      no observación &mdash; pero muestra cuánto del rango de las corridas queda sobre o bajo lo que
      dice la corrida determinística.</p>
    </div>"""

    norte_note = ""
    if norte_check:
        rows_n = ""
        for loc in norte_check["localidades"]:
            r = watchlist.get(loc["name"])
            g = r["signal"]["hazard_max_regional"] if r else None
            rain = r["signal"]["rain_liquid_total_mm"] if r else None
            agg = loc.get("peak_worst_comuna_agregado")
            agg_txt = f" (vs. {agg:.2f} agregado a nivel comuna)" if agg is not None else ""
            if g is None:
                # la localidad del chequeo no tiene corrida en runouts/ (p.ej. un
                # ciclo viejo re-generado): se dice, en vez de tumbar el registro
                rows_n += (f"<li><strong>{loc['label']}</strong>: sin corrida de runout para "
                           f"este ciclo{agg_txt}.</li>")
                continue
            rain_txt = f" &mdash; lluvia líquida 120&nbsp;h {rain:.1f}&nbsp;mm" if rain is not None else ""
            rows_n += (f"<li><strong>{loc['label']}</strong>: gatillo en el punto exacto del pueblo "
                       f"{g:.2f}{agg_txt}{rain_txt}.</li>")
        norte_note = f"""<div class="callout" style="margin-top:22px;">
      <h2>¿Y el Norte? &mdash; por qué Atacama no entra al top-20 (con una corrección propia)</h2>
      <p>{norte_check['motivo']}</p>
      <p>{norte_check.get('correccion', '')}</p>
      <ul>{rows_n}</ul>
      <p>{norte_check['resultado']}</p>
    </div>"""

    ventana = rango_ventana(forecast)
    gfs_bullet = resumen_modelos(forecast, cycle)
    runout_bullet = resumen_runouts(rows, loc_labels)

    body = f"""
<div class="sheet">
  <div class="masthead">
    <div class="eyebrow">Registro técnico interno · sistema nowcast</div>
    <h1 class="title">Vigilancia de remociones en masa y flujos detríticos<br>Ciclo {cycle} &middot; {ventana}</h1>
    <p class="subtitle">Estado del pronóstico, observación y simulación al momento de generar este documento — dejado como constancia previa a lo que ocurra en la ventana pronosticada.</p>
    <div class="meta-strip">
      <div class="meta-item"><span class="k">Generado</span><span class="v">{gen_stamp}</span></div>
      <div class="meta-item"><span class="k">Hora Chile</span><span class="v">{hora_chile(now_utc)}</span></div>
      <div class="meta-item"><span class="k">Ciclo GFS base</span><span class="v">{cycle}</span></div>
      <div class="meta-item"><span class="k">Dominios activos</span><span class="v">{len(forecast)}</span></div>
      <div class="meta-item"><span class="k">Localidades en vigilancia</span><span class="v">{len(rows)}</span></div>
    </div>
  </div>

  <div class="callout">
    <h2>Resumen ejecutivo</h2>
    <p>Este documento fija en el tiempo el estado de un sistema experimental de nowcasting de geopeligros (motor <em>nowcast</em>) corriendo de forma autónoma, en modo sombra, sobre la ventana pronosticada de 120&nbsp;h ({ventana}). Su propósito es doble: servir de base para comparar el pronóstico contra lo efectivamente ocurrido una vez cerrada la ventana, y dejar constancia verificable — con huella de integridad y timestamp — de qué se sabía y cuándo.</p>
    <ul>
      <li>{gfs_bullet}</li>
      <li>El cruce con los modelos de susceptibilidad de terreno identifica <strong>{plural(n_high, "localidad", "localidades")} en nivel ALTO</strong> de vigilancia combinada.</li>
      <li>{runout_bullet}</li>
      {comuna_bullet}
      <li><strong>Nada de este documento es un aviso oficial.</strong> Las fuentes de verdad para decisiones de protección civil son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.</li>
    </ul>
  </div>

  <div class="disclaimer"><strong>Sobre la naturaleza de este registro.</strong> Todos los números provienen de un sistema experimental, sin validación operacional, con susceptibilidad de terreno agregada por analogía (transferencia entre cuencas en dos capas) y parámetros de simulación calibrados para un régimen árido distinto (Atacama). Se documentan como fueron calculados el {gen_stamp}, antes de que transcurriera la ventana pronosticada.</div>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">01</span><h2>Qué pronosticaba el sistema en este momento</h2></div>
    <p>El motor consume el pronóstico determinístico GFS (0.25&deg;, NOAA/NCEP) cuatro veces al día, particiona la precipitación en líquida y sólida según la altura de la isoterma 0&deg;C del propio modelo contra su propia orografía, y corre el umbral lluvia&ndash;duración regional (intercepto <em>a</em>=5.5, calibrado contra el inventario histórico del río Maipo) sobre una ventana de 120&nbsp;h. ECMWF Open Data e ICON global se procesan en paralelo cuando están disponibles, con la misma ventana y umbral.</p>
    {ecmwf_note}
    {icon_note}
    <div class="table-wrap"><table>
      <thead><tr><th>Dominio</th><th class="num">Isoterma 0&deg;C</th><th class="num">Fracción nieve</th><th class="num">Pico GFS</th>{ecmwf_th}{icon_th}<th class="num">Cruces</th><th>Primer cruce (UTC)</th></tr></thead>
      <tbody>{dom_rows}</tbody>
    </table></div>
    <figure class="map">
      <img src="data:image/png;base64,{images['mapa_panorama']}" alt="Mapa de excedencia del gatillo lluvia-duración pronosticado" />
      <figcaption><span class="cap-num">Figura 1.</span> Índice de excedencia del umbral I&ndash;D regional pronosticado para el horizonte completo de 120&nbsp;h (ciclo {cycle}).</figcaption>
    </figure>
    {ensemble_note}
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">02</span><h2>Dónde el terreno amplifica la señal climática</h2></div>
    <p>El índice anterior describe sólo la mitad climática del peligro. Se cruza celda a celda con modelos de susceptibilidad de remociones en masa entrenados por el autor sobre <strong>15 cuencas nacionales</strong> (paper1): el algoritmo se elige por cuenca según validación cruzada — XGBoost en 8 (incl. Elqui, Limarí, Huasco, Bueno), RandomForest en 4 (incl. Maipo, Biobío) y Ridge en 3 (incl. <strong>Copiapó</strong> y <strong>Maule</strong>), con AUC de validación entre 0.61 y 0.91 según cuenca. Dos zonas sin modelo propio (Araucanía y la Cordillera de la Costa Valparaíso&ndash;Los Ríos) se completaron por <strong>transferencia</strong> del modelo del Biobío — decisión metodológica explícita, rotulada en los metadatos como <em>transferencia sin validar</em>.</p>
    <figure class="map">
      <img src="data:image/png;base64,{images['mapa_overlay']}" alt="Mapa de cruce entre gatillo climático y susceptibilidad de terreno" />
      <figcaption><span class="cap-num">Figura 2.</span> Producto del índice de gatillo climático por la susceptibilidad de terreno agregada a 0.1&deg;.</figcaption>
    </figure>
  </section>
{comuna_section}
  <section class="doc wide">
    <div class="sec-head"><span class="sec-num">04</span><h2>Localidades bajo vigilancia</h2></div>
    {dos_listas_nota}
    <p>Localidades seleccionadas <strong>objetivamente</strong> (no a criterio del autor) dentro del barrido III&ndash;XIV, restringidas a las que tienen poblado censado (Censo 2024) con dato de susceptibilidad válido en el punto exacto &mdash; ver <code>select_watchlist.py</code>. Para cada una se corrió una simulación de flujo detrítico por agentes (modelo <code>debris-flow</code> sobre <code>swarm-abm</code>, parámetros de Atacama 2015 — análogo ilustrativo, no predicción calibrada) forzada con la lluvia y la isoterma del ciclo vigente. El ABM está calibrado sobre terreno de abanico aluvial abrupto; en localidades de fondo de valle plano sin frente montañoso real cerca del punto, el footprint puede esparcirse de forma difusa en vez de canalizarse — un guard de plausibilidad física marca esos casos como <span class="chip s-none">NO PLAUSIBLE</span> y no deben leerse con el mismo peso que los canalizados. Aparte de la forma, la <strong>escala</strong> del footprint (km² afectados) tampoco está validada fuera de Atacama: el preset por defecto se ajustó contra el footprint real de Copiapó 2015 con un ajuste flojo (IoU 0.171 en su propio evento) y no existe un evento real mapeado en esta región contra el cual calibrar el tamaño — eventos reales documentados aquí (SERNAGEOMIN, sistema frontal ene-2021, Malloa/San Vicente/Coinco) se localizaron a sectores puntuales, no a la extensión que muestran estos mapas. Esas corridas llevan <span class="chip chip-domain">sin calibrar aquí</span>.</p>
    <p>Antes de leer un footprint conviene preguntarse si el <strong>fenómeno</strong> es el correcto. La razón de Melton evaluada en el centroide urbano de la comuna sesga sistemáticamente hacia <em>crecida fluvial</em>, porque el centroide cae en el fondo de valle y engancha al río grande, no a la quebrada empinada que produce el aluvión &mdash; con ese criterio, sitios de aluviones mortales (Alto del Carmen 2015) salen clasificados como crecida. Por eso cada comuna se representa además como su <strong>conjunto de abanicos</strong>: se detectan los <em>fan apex</em> (bocas de quebradas empinadas, razón de Melton de su cuenca propia) y se reporta a qué distancia de runout el flujo de cada uno alcanza el área poblada censada. Las comunas donde ese análisis reconoce un abanico que alcanza al poblado llevan <span class="chip chip-fenom-aluvion">aluvión de abanico</span>; aquellas donde ningún abanico alcanza llevan <span class="chip chip-fenom-crecida">crecida, no aluvión</span> &mdash; y ahí el footprint de flujo detrítico de esta sección es probablemente el modelo equivocado, aunque pase los guards de forma y escala. El detector no inventa abanicos donde no los hay (los pueblos de valle y costa planos quedan correctamente sin ellos), pero está validado sobre pocos casos y con una ruta de flujo simplificada: es <strong>diagnóstico</strong>, no un rótulo operacional. Las comunas en cuencas sin modelo hidrológico propio (transferencia) no llevan chip: no es que sean crecida, es que no son clasificables aquí.</p>
    {norte_note}
    <div class="table-wrap"><table>
      <colgroup><col class="c-loc"><col class="c-lvl">{col_lista_grp}<col><col><col><col></colgroup>
      <thead><tr><th>Localidad</th><th>Nivel</th>{col_lista}<th class="num">Gatillo</th><th>Cruce</th><th class="num">Lluvia 120h</th><th class="num">Footprint</th></tr></thead>
      <tbody>{tbl_rows}</tbody>
    </table></div>
    <h3>Footprint simulado por localidad</h3>
    <p>Ventana de 12&nbsp;km simulada, con el centro del poblado (estrella) y anillos de referencia a 0.5 / 1 / 2&nbsp;km. El área sombreada es el footprint del flujo simulado.</p>
    <div class="loc-grid">{loc_cards}</div>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">05</span><h2>Arquitectura del sistema</h2></div>
    <p>El sistema opera en tres capas independientes, cada una con su propia cadencia y grado de validación.</p>
    <div class="arch-grid">
      <div class="arch-card"><div class="arch-k">Observación (cada 30 min)</div><p>Fusión por noisy&ndash;OR de IMERG Early NRT, GOES&ndash;East QPE y pluviómetros DMC, sobre 5 dominios geográficos, replicada en dos nodos independientes.</p></div>
      <div class="arch-card"><div class="arch-k">Pronóstico (4&times;/día)</div><p>GFS 0.25&deg; + ECMWF Open Data 0.25&deg; + DWD ICON global ~13&nbsp;km cuando están disponibles; horizonte 120&nbsp;h y dos variantes de umbral.</p></div>
      <div class="arch-card"><div class="arch-k">Simulación (bajo demanda)</div><p>Modelo de agentes de flujo detrítico sobre DEM Copernicus y susceptibilidad real, disparado sobre localidades de interés.</p></div>
    </div>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">06</span><h2>Metodología y limitaciones honestas</h2></div>
    <p>Ninguna capa de este sistema ha pasado por un ciclo de validación operacional:</p>
    <ul>
      <li><strong>Pronóstico determinístico, sin ensamble.</strong> No hay medida de incertidumbre ni probabilidad calibrada.</li>
      <li><strong>Umbral regional calibrado en una sola cuenca</strong> (Maipo, 1979&ndash;2016) y extrapolado al país.</li>
      <li><strong>Dos capas de susceptibilidad por transferencia sin validar</strong>, rotuladas explícitamente en los metadatos.</li>
      <li><strong>Simulaciones con parámetros de un régimen distinto</strong> (Atacama árido) aplicados a cuencas templadas/húmedas.</li>
      <li><strong>Resolución del forzante</strong> (~25&nbsp;km GFS/ECMWF y ~13&nbsp;km ICON global) sigue por debajo de la escala de las quebradas reales.</li>
      <li><strong>Sin verificación operacional a la fecha de este documento.</strong></li>
      <li><strong>ECMWF HRES e ICON global son también determinísticos</strong> (sin ensamble), igual que GFS — su inclusión da puntos de vista independientes, no una probabilidad calibrada ni un "voto de desempate"; cuando difieren, ninguno se descarta por defecto.</li>
      <li><strong>El cruce comunal (secc. 03) es administrativo, no de exposición fina</strong>: máximo a ~10&nbsp;km dentro del polígono, población total censal de la comuna (no expuestos reales), y con huecos de modelo de terreno en varias comunas (Valparaíso costero, O'Higgins, partes de Los Ríos) donde no hay cuenca propia ni transferencia calculada.</li>
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
    return f"<title>Registro del ciclo {cycle} — vigilancia de remociones en masa</title>\n<style>\n{font_faces}\n{CSS}\n</style>\n{body}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-forecast/informes"))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--forzar", action="store_true",
                    help="regenerar aunque ya exista un registro del ciclo vigente")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    runouts_dir = os.path.realpath(os.path.expanduser(a.runouts))
    os.makedirs(a.out, exist_ok=True)

    # Idempotencia por ciclo: el ciclo vigente es el basename del target de
    # `latest` (mismo criterio que run_localidades). Si ya se generó un registro
    # para él, no hay dato nuevo que registrar — se salta salvo --forzar.
    cycle = os.path.basename(forecast_dir)
    marker = os.path.join(a.out, ".last_cycle")
    if not a.forzar and os.path.exists(marker):
        try:
            if open(marker).read().strip() == cycle:
                print(f"registro del ciclo {cycle} ya existe; se salta (--forzar para regenerar)")
                return
        except OSError:
            pass

    forecast = {}
    for d in DOMAIN_ORDER:
        p = f"{forecast_dir}/{d}/forecast.json"
        if os.path.exists(p):
            forecast[d] = json.load(open(p))
            ep = f"{forecast_dir}/{d}/ecmwf/ecmwf.json"
            if os.path.exists(ep):
                forecast[d]["ecmwf"] = json.load(open(ep))
            ip = f"{forecast_dir}/{d}/icon/icon.json"
            if os.path.exists(ip):
                forecast[d]["icon"] = json.load(open(ip))
    if "chile-centro-sur" not in forecast:
        sys.exit(f"falta el dominio macro chile-centro-sur en {forecast_dir}")

    # Filtrado a la seleccion VIGENTE de watchlist.json — runouts_dir acumula
    # corridas de ciclos/watchlists anteriores (nombres que ya no estan en la
    # seleccion objetiva actual) que de otro modo se colaban en el reporte
    # mezclados con la seleccion de hoy, contradiciendo el propio texto del
    # informe ("las 20 comunas..."). Ver docs/auditoria-motor.md, hallazgo
    # 2026-07-17.
    wl_names_p = os.path.join(forecast_dir, "watchlist.json")
    current_names = None
    if os.path.exists(wl_names_p):
        current_names = {s["name"] for s in json.load(open(wl_names_p))["selected"]}

    watchlist = {}
    skipped_stale = []
    for d in sorted(glob.glob(os.path.join(runouts_dir, "*"))):
        name = os.path.basename(d)
        p = os.path.join(d, "resumen.json")
        if not os.path.exists(p):
            continue
        if current_names is not None and name not in current_names:
            skipped_stale.append(name)
            continue
        watchlist[name] = json.load(open(p))
    if skipped_stale:
        print(f"runouts fuera de la watchlist vigente, omitidos del reporte: "
              f"{', '.join(skipped_stale)}")
    if not watchlist:
        sys.exit(f"sin corridas en {runouts_dir} — correr runout_localidad.py primero")

    comuna_p = f"{forecast_dir}/comuna_impact/comuna_impact.json"
    comuna = json.load(open(comuna_p)) if os.path.exists(comuna_p) else None
    vuln_p = f"{forecast_dir}/comuna_impact/comuna_vulnerability.json"
    if comuna and os.path.exists(vuln_p):
        vuln = json.load(open(vuln_p))["por_comuna"]
        for r in comuna["top_alto"]:
            v = vuln.get(str(int(r["CUT"])))
            r["indice_vulnerabilidad"] = v["indice_vulnerabilidad"] if v else None
            r["tasa_adulto_mayor"] = v["tasa_adulto_mayor"] if v else None

    ensemble_p = f"{forecast_dir}/ecmwf_ensemble_check.json"
    ensemble = json.load(open(ensemble_p)) if os.path.exists(ensemble_p) else None

    gefs_p = f"{forecast_dir}/gefs_ensemble_check.json"
    gefs = json.load(open(gefs_p)) if os.path.exists(gefs_p) else None

    runout_ens_p = f"{forecast_dir}/runout_ensemble.json"
    runout_ensemble = json.load(open(runout_ens_p)) if os.path.exists(runout_ens_p) else {}

    data_hash = hashlib.sha256(
        (json.dumps(forecast, sort_keys=True) + json.dumps(watchlist, sort_keys=True)
         + json.dumps(comuna, sort_keys=True) + json.dumps(ensemble, sort_keys=True)
         + json.dumps(gefs, sort_keys=True) + json.dumps(runout_ensemble, sort_keys=True)).encode()
    ).hexdigest()

    work = os.path.join(a.out, ".build")
    img_dir = os.path.join(work, "img")
    os.makedirs(img_dir, exist_ok=True)
    print("generando mapas...")
    make_maps(forecast_dir, runouts_dir, watchlist, img_dir)

    img_names = ["mapa_panorama", "mapa_overlay"]
    if os.path.exists(f"{img_dir}/mapa_comunas.png"):
        img_names.append("mapa_comunas")
    images = {n: b64(f"{img_dir}/{n}.png") for n in img_names}
    loc_images = {name: b64(f"{img_dir}/localidades/{name}.png")
                  for name in watchlist if os.path.exists(f"{img_dir}/localidades/{name}.png")}
    fonts = {n.replace("-", "_"): b64(f"{HERE}/fonts/{n}.woff2") for n in
             ("schola-reg", "schola-bold", "schola-ital", "heroscn-reg", "heroscn-bold", "mono-reg", "mono-bold")}

    now = datetime.now(timezone.utc)
    loc_labels = load_loc_labels(forecast_dir)
    wl_p = os.path.join(forecast_dir, "watchlist.json")
    wl_meta = json.load(open(wl_p)) if os.path.exists(wl_p) else {}
    norte_check = wl_meta.get("regional_check_norte")
    # name -> ["peligro"|"exposicion", ...] desde el watchlist.json de dos listas
    # (select_watchlist >= 2026-07-20). Para watchlist.json viejas (sin en_lista)
    # queda vacío y la columna simplemente no aparece.
    en_lista = {s["name"]: s.get("en_lista", []) for s in wl_meta.get("selected", [])}
    dos_listas = bool(wl_meta.get("lista_exposicion")) and any(en_lista.values())
    html = build_html(forecast, watchlist, images, loc_images, fonts, data_hash, now, comuna, ensemble,
                      gefs, loc_labels, norte_check, runout_ensemble,
                      en_lista=en_lista, dos_listas=dos_listas,
                      solo_exposicion=wl_meta.get("solo_exposicion", []),
                      solo_peligro=wl_meta.get("solo_peligro", []))

    stamp = now.strftime("%Y%m%dT%H%MZ")
    html_path = os.path.join(a.out, f"registro-evento-{stamp}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"HTML: {html_path} ({os.path.getsize(html_path)/1024:.0f} KB)")
    print(f"hash de integridad: {data_hash}")

    # Marca de idempotencia: recién ahora que el registro quedó en disco se
    # anota el ciclo, para que un fallo previo no deje la marca adelantada.
    with open(marker, "w") as f:
        f.write(cycle + "\n")

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
