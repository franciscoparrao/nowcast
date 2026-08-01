#!/usr/bin/env python3
"""Cruce comunal: gatillo pronosticado (GFS + ECMWF + ICON) × comuna administrativa,
de la Región de Atacama (III) a la de Los Ríos (XIV) — el barrido pedido tras
el hallazgo de divergencia GFS/ECMWF/ICON.

Fuentes (todas ya en disco, sin descargas nuevas):
  - Límite comunal oficial: `Comunal_CPV24` del geopackage cartográfico del
    Censo 2024 (345 comunas, CUT/COD_REGION/REGION reales — no un geojson
    genérico de terceros).
  - Población: microdato de personas del Censo 2024 (`personas_censo2024.
    parquet`, 18.5M filas), agregado por comuna (columna `comuna` = CUT) vía
    DuckDB — no un archivo de "población por comuna" ya resumido, para no
    depender de que exista.
  - Peligro: `hazard_max_regional-a5.5.tif` de GFS/ECMWF y `hazard_max.tif`
    (caine-global) de ICON por dominio (ya corridos) — variante distinta por
    fuente porque ICON no diluye picos como el bilineal de GFS/ECMWF, ver
    susc_overlay.py — más `hazard_x_susc_max.tif` (terreno) donde exista.

MAPEO DOMINIO→REGIÓN: la Región de Atacama (III) la cubre el dominio propio
`atacama`; las regiones IV (Coquimbo) a XIV (Los Ríos) + Ñuble las cubre el
barrido macro `chile-centro-sur` (mismo raster, sin mosaico necesario —
evita contar dos veces en los dominios chicos que se solapan con el macro).

HONESTIDAD DEL CRUCE (leer antes de citar números):
  - El gatillo es el MÁXIMO dentro del polígono comunal a resolución 0.1°
    (~10 km) — una comuna grande con una esquina cordillerana puede marcar
    ALTO por una fracción mínima de su área; no es "toda la comuna en riesgo".
  - Población = TODOS los habitantes de la comuna (censo 2024), no una
    estimación de expuestos reales — es una cota superior gruesa, rotulada
    como tal.
  - Sin modelo de terreno (columna terreno = "s/d") NO significa susceptibilidad
    cero — significa que esa comuna cae fuera de las 15 cuencas + transferencias
    del paper1. Nunca se lee como "seguro".
  - Nivel de severidad = el PEOR de los tres modelos (GFS, ECMWF, ICON) — bracketing,
    no promedio ni elección de un modelo "correcto".

Uso:
    python3 comuna_impact.py [--forecast ~/nowcast-forecast/latest]
        [--alert-level 0.5] [--out ~/nowcast-forecast/latest/comuna_impact]
Salidas: comuna_impact.geojson (todas las comunas del barrido, para GIS) +
comuna_impact.json (resumen: rankings, población expuesta, por región).
"""

import argparse
import json
import os

import duckdb
import geopandas as gpd
import numpy as np
import rasterio
from rasterstats import zonal_stats

CENSUS_GPKG = os.path.expanduser(
    "~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")
CENSUS_PERSONAS = os.path.expanduser(
    "~/proyectos/censo-2024/viv_hog_per_censo2024/personas_censo2024.parquet")

# III (Atacama) a XIV (Los Ríos) inclusive, más Ñuble (16, enclavada en el
# tramo Biobío/Maule): 3,4,5,6,7,8,9,13,14,16.
TARGET_REGIONS = [3, 4, 5, 6, 7, 8, 9, 13, 14, 16]
REGION_ORDER = {3: 0, 4: 1, 5: 2, 13: 3, 6: 4, 7: 5, 16: 6, 8: 7, 9: 8, 14: 9}

# región → dominio de pronóstico que la cubre (ver docstring: atacama propio,
# el resto por el barrido macro para no mosaicar/duplicar solapes).
REGION_DOMAIN = {3: "atacama"}
DEFAULT_DOMAIN = "chile-centro-sur"


def log(msg):
    print(f"[comuna] {msg}", flush=True)


def load_population():
    con = duckdb.connect()
    df = con.execute(
        f"SELECT comuna AS cut, COUNT(*) AS poblacion "
        f"FROM read_parquet('{CENSUS_PERSONAS}') GROUP BY comuna"
    ).df()
    con.close()
    return dict(zip(df["cut"].astype(int), df["poblacion"].astype(int)))


def zonal_peak(raster_path, geoms):
    """[max por geometría] o None por geometría si el raster no existe."""
    if not os.path.exists(raster_path):
        return [None] * len(geoms)
    with rasterio.open(raster_path) as src:
        affine = src.transform
        arr = src.read(1)
        nodata = src.nodata
    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
        arr = np.where(arr == nodata, np.nan, arr)
    stats = zonal_stats(geoms, arr, affine=affine, stats=["max"], nodata=np.nan, all_touched=True)
    return [s["max"] for s in stats]


def severity(worst):
    if worst is None or worst < 0:
        return "s-none", "SIN DATO"
    if worst >= 0.8:
        return "s-high", "ALTO"
    if worst >= 0.4:
        return "s-mid", "MEDIO"
    return "s-low", "BAJO"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--alert-level", type=float, default=0.5)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    fdir = os.path.realpath(os.path.expanduser(a.forecast))
    out_dir = a.out or os.path.join(fdir, "comuna_impact")
    os.makedirs(out_dir, exist_ok=True)

    log("cargando límite comunal oficial (Censo 2024, Comunal_CPV24)...")
    gdf = gpd.read_file(CENSUS_GPKG, layer="Comunal_CPV24")
    gdf = gdf[gdf["COD_REGION"].isin(TARGET_REGIONS)].to_crs(4326).reset_index(drop=True)
    log(f"{len(gdf)} comunas en el barrido (regiones III–XIV + Ñuble)")

    log("agregando población 2024 por comuna (microdato, 18.5M filas)...")
    pop_by_cut = load_population()
    gdf["poblacion"] = gdf["CUT"].astype(int).map(pop_by_cut)
    n_missing_pop = int(gdf["poblacion"].isna().sum())
    if n_missing_pop:
        log(f"AVISO: {n_missing_pop} comuna(s) sin match de población en el censo")

    # GFS/ECMWF: bilineal (forzante diluida) -> regional-a5.5. ICON: vecino-mas-
    # cercano desde ~13 km nativo (no diluye picos) -> caine-global sin descuento
    # de dilución; mismo intercepto que regional-a5.5 saturaba el peak_icon a 1.0
    # en 18/20 comunas del watchlist (ver susc_overlay.py). Mismo criterio en
    # ambos módulos.
    HAZARD_VARIANT_BY_SOURCE = {
        "gfs": "hazard_max_regional-a5.5.tif",
        "ecmwf": "hazard_max_regional-a5.5.tif",
        "icon": "hazard_max.tif",
    }
    for label, sub in [("gfs", ""), ("ecmwf", "ecmwf"), ("icon", "icon")]:
        variant = HAZARD_VARIANT_BY_SOURCE[label]
        vals_by_domain = {}
        for dom in set(REGION_DOMAIN.get(r, DEFAULT_DOMAIN) for r in TARGET_REGIONS):
            hz_path = os.path.join(fdir, dom, sub, variant) if sub else os.path.join(fdir, dom, variant)
            vals_by_domain[dom] = zonal_peak(hz_path, gdf.geometry)
        col = np.full(len(gdf), np.nan)
        for i, (_, row) in enumerate(gdf.iterrows()):
            dom = REGION_DOMAIN.get(int(row["COD_REGION"]), DEFAULT_DOMAIN)
            v = vals_by_domain[dom][i]
            col[i] = v if v is not None else np.nan
        gdf[f"peak_{label}"] = col

    # Terreno: hazard_x_susc_max por dominio propio (no el macro, que ya
    # incluye las mismas capas mosaicadas — usar el dominio nativo evita
    # re-mosaicar; para Coquimbo/RM/Ñuble-Biobío/Araucanía existen productos
    # propios, para el resto (Valparaíso, O'Higgins, Maule, Los Ríos sin
    # cuenca) el macro es la única fuente, con sus mismos huecos NaN).
    susc_vals = np.full(len(gdf), np.nan)
    for dom in ["atacama", "coquimbo", "rm", "nuble-biobio", "araucania", "chile-centro-sur"]:
        p = os.path.join(fdir, dom, "hazard_x_susc_max.tif")
        vals = zonal_peak(p, gdf.geometry)
        for i, v in enumerate(vals):
            if v is not None and np.isfinite(v):
                susc_vals[i] = np.nanmax([susc_vals[i], v]) if np.isfinite(susc_vals[i]) else v
    gdf["peak_terreno"] = susc_vals

    model_cols = ["peak_gfs", "peak_ecmwf", "peak_icon"]
    gdf["peak_worst"] = gdf[model_cols].max(axis=1, skipna=True)
    gdf.loc[gdf[model_cols].isna().all(axis=1), "peak_worst"] = np.nan
    gdf["nivel"] = [severity(v)[1] for v in gdf["peak_worst"]]
    gdf["divergencia_abs"] = gdf[model_cols].max(axis=1, skipna=True) - gdf[model_cols].min(axis=1, skipna=True)

    alto = gdf[gdf["nivel"] == "ALTO"].sort_values("peak_worst", ascending=False)
    medio = gdf[gdf["nivel"] == "MEDIO"].sort_values("peak_worst", ascending=False)
    pob_alto = int(alto["poblacion"].fillna(0).sum())
    pob_medio = int(medio["poblacion"].fillna(0).sum())
    pob_total_barrido = int(gdf["poblacion"].fillna(0).sum())

    por_region = (
        gdf.groupby("REGION", as_index=False)
        .agg(n_comunas=("CUT", "count"),
             n_alto=("nivel", lambda s: int((s == "ALTO").sum())),
             n_medio=("nivel", lambda s: int((s == "MEDIO").sum())),
             poblacion=("poblacion", "sum"),
             peak_worst_region=("peak_worst", "max"))
        .to_dict("records")
    )

    summary = {
        "alert_level": a.alert_level,
        "n_comunas_barrido": len(gdf),
        "n_alto": len(alto), "n_medio": len(medio),
        "poblacion_total_barrido": pob_total_barrido,
        "poblacion_en_comunas_alto": pob_alto,
        "poblacion_en_comunas_medio": pob_medio,
        "por_region": por_region,
        "top_alto": alto[["CUT", "COMUNA", "REGION", "peak_gfs", "peak_ecmwf", "peak_icon", "peak_worst",
                          "peak_terreno", "poblacion"]].head(60).to_dict("records"),
        # R4-H2: top_alto es una MUESTRA, no la lista de ALTO — en un evento
        # generalizado hubo 209-220 comunas en ALTO y este JSON mostraba 60 sin
        # decirlo; Coelemu (deslizamientos reales el 31-jul-2026) quedó fuera de
        # la muestra en 3 de 7 ciclos y pareció "perdida" cuando estuvo en ALTO
        # los 7. La truncación ahora se declara donde vive el dato, no solo en
        # el HTML. La lista COMPLETA con niveles está en comuna_impact.geojson.
        "top_alto_es_muestra": len(alto) > 60,
        "top_alto_nota": (
            f"primeras {min(60, len(alto))} de {len(alto)} comunas en ALTO, "
            f"ordenadas por peak_worst — la lista completa está en "
            f"comuna_impact.geojson (campo nivel)"
        ),
        "label": "cruce administrativo grueso (max 0.1° dentro del polígono comunal) — "
                 "NO es un mapa de exposición fina ni un aviso oficial. Población = "
                 "total censal 2024 de la comuna, cota superior, no expuestos reales.",
    }

    for c in gdf.select_dtypes(include="string").columns:
        gdf[c] = gdf[c].astype(object)
    gdf.to_file(os.path.join(out_dir, "comuna_impact.geojson"), driver="GeoJSON")
    with open(os.path.join(out_dir, "comuna_impact.json"), "w") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False, default=lambda o: None if
                  (isinstance(o, float) and np.isnan(o)) else o)

    log(f"{len(gdf)} comunas evaluadas | ALTO {len(alto)} (pob. {pob_alto:,}) | "
        f"MEDIO {len(medio)} (pob. {pob_medio:,}) | total barrido pob. {pob_total_barrido:,}")
    if len(alto) > 60:
        log(f"AVISO: top_alto del JSON es una MUESTRA (60 de {len(alto)} en ALTO) — "
            f"lista completa en el geojson (R4-H2)")
    log("top 10 ALTO (peor de los 3 modelos):")
    for _, r in alto.head(10).iterrows():
        d = f"{r['divergencia_abs']:.2f}" if np.isfinite(r["divergencia_abs"]) else "n/a"
        log(f"  {r['COMUNA']:<22} {r['REGION']:<12} gfs={r['peak_gfs']:.2f} "
            f"ecmwf={r['peak_ecmwf']:.2f} icon={r['peak_icon']:.2f} (rango={d}) "
            f"terreno={r['peak_terreno']} pob={int(r['poblacion']) if np.isfinite(r['poblacion']) else '?'}")
    log(f"salidas → {out_dir}")


if __name__ == "__main__":
    main()
