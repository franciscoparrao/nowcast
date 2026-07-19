#!/usr/bin/env python3
"""Consolida las salidas del evento en UN GeoPackage abrible en QGIS.

Los productos quedan hoy repartidos en decenas de carpetas
(`risk_maps/<loc>/gis/*.tif`, `runouts/<loc>/runout_footprint.tif`,
`risk_maps/cuenca_*/`). Esto los junta en un solo `.gpkg` con capas
vectoriales, que es lo que se puede arrastrar a QGIS de una:

  localidades        puntos con TODOS los atributos de riesgo.json
                     (peligro, excedencia I-D, compuesto, exposición,
                     vulnerabilidad, avisos QA) — simbolizable directo
  footprints         footprint del ABM vectorizado por localidad, con los
                     mismos atributos

Los rásters NO se meten al gpkg (QGIS los lee mejor como GeoTIFF suelto);
se listan en `capas_raster.csv` con su ruta, para cargarlos de una vez.

Uso:
    python3 export_gis.py [--out ~/nowcast-forecast/gis/evento.gpkg]
"""

import argparse
import csv
import glob
import json
import os

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from shapely.geometry import Point, shape

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")
RISK_MAPS = os.path.expanduser("~/nowcast-forecast/risk_maps")


def plano(d, prefijo=""):
    """Aplana riesgo.json/resumen.json a columnas simples (QGIS no anida)."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(plano(v, f"{prefijo}{k}_"))
        elif isinstance(v, (list, tuple)):
            out[f"{prefijo}{k}"] = ", ".join(str(x) for x in v)
        else:
            out[f"{prefijo}{k}"] = v
    return out


def normaliza_tipos(gdf):
    """Fiona no sabe inferir esquema desde los dtypes de extensión de pandas
    (StringDtype, Int64...) y revienta con 'Cannot interpret StringDtype'.
    Se bajan a tipos planos: texto a str, nulos a None."""
    import pandas as pd
    for col in gdf.columns:
        if col == "geometry":
            continue
        s = gdf[col]
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            vals = [None if pd.isna(v) else float(v) for v in s]
        else:
            vals = [None if (not isinstance(v, (list, tuple)) and pd.isna(v)) else str(v)
                    for v in s]
        # se reconstruye como object PURO: asignar una Series deja que pandas
        # vuelva a inferir StringDtype y el error reaparece
        gdf[col] = pd.Series(vals, index=gdf.index, dtype=object)
    return gdf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-forecast/gis/evento.gpkg"))
    ap.add_argument("--runouts", default=RUNOUTS)
    ap.add_argument("--risk-maps", default=RISK_MAPS)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    filas, polis = [], []
    for rdir in sorted(glob.glob(os.path.join(a.runouts, "*"))):
        name = os.path.basename(rdir)
        res_p = os.path.join(rdir, "resumen.json")
        if not os.path.exists(res_p):
            continue
        res = json.load(open(res_p))
        lon, lat = res.get("lon"), res.get("lat")
        if lon is None:
            continue
        attrs = {"name": name}
        attrs.update(plano({k: v for k, v in res.items()
                            if k not in ("lon", "lat", "name")}))
        riesgo_p = os.path.join(a.risk_maps, name, "riesgo.json")
        if os.path.exists(riesgo_p):
            attrs.update(plano(json.load(open(riesgo_p)), "riesgo_"))
        filas.append({**attrs, "geometry": Point(lon, lat)})

        # footprint vectorizado
        fp_p = os.path.join(rdir, "runout_footprint.tif")
        if os.path.exists(fp_p):
            with rasterio.open(fp_p) as src:
                banda = src.read(1)
                mask = banda == 1
                if mask.any():
                    geoms = [shape(g) for g, v in
                             shapes(banda.astype("uint8"), mask=mask, transform=src.transform)
                             if v == 1]
                    if geoms:
                        gs = gpd.GeoSeries(geoms, crs=src.crs).to_crs("EPSG:4326")
                        area_km2 = float(gpd.GeoSeries(geoms, crs=src.crs).area.sum() / 1e6)
                        polis.append({"name": name,
                                      "area_km2": round(area_km2, 2),
                                      "peligro": attrs.get("riesgo_peligro_no_saturado"),
                                      "excedencia": attrs.get("riesgo_exceedancia_id_exceedancia"),
                                      "compuesto": attrs.get("riesgo_indice_compuesto"),
                                      "plausible": attrs.get("physically_plausible"),
                                      "ciclo": attrs.get("cycle"),
                                      # union_all() es de geopandas >=1.0; unary_union
                                      # es el nombre disponible en la versión de esta máquina
                                      "geometry": gs.unary_union})

    if not filas:
        raise SystemExit("no se encontró ninguna corrida en " + a.runouts)

    gdf = gpd.GeoDataFrame(filas, crs="EPSG:4326")
    gdf = normaliza_tipos(gdf)
    gdf.to_file(a.out, layer="localidades", driver="GPKG")
    print(f"localidades: {len(gdf)} puntos -> {a.out}")

    if polis:
        gfp = normaliza_tipos(gpd.GeoDataFrame(polis, crs="EPSG:4326"))
        gfp.to_file(a.out, layer="footprints", driver="GPKG")
        print(f"footprints:  {len(gfp)} polígonos")

    # índice de rásters para cargar en bloque
    csv_p = os.path.join(os.path.dirname(a.out), "capas_raster.csv")
    with open(csv_p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["capa", "localidad", "ruta"])
        for p in sorted(glob.glob(os.path.join(a.risk_maps, "*", "gis", "*.tif"))):
            w.writerow([os.path.basename(p), os.path.basename(os.path.dirname(os.path.dirname(p))), p])
        for p in sorted(glob.glob(os.path.join(a.risk_maps, "cuenca_*", "*.tif"))):
            w.writerow([os.path.basename(p), os.path.basename(os.path.dirname(p)), p])
        for p in sorted(glob.glob(os.path.join(a.runouts, "*", "runout_footprint.tif"))):
            w.writerow(["runout_footprint.tif", os.path.basename(os.path.dirname(p)), p])
    print(f"índice de rásters -> {csv_p}")


if __name__ == "__main__":
    main()
