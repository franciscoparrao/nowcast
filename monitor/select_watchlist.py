#!/usr/bin/env python3
"""Reemplaza la lista arbitraria de 13 localidades por una selección objetiva:
las comunas con mayor gatillo×susceptibilidad real (comuna_impact.py, sección
02 del registro — "dónde el terreno amplifica la señal climática"), con su
localidad urbana principal (censo 2024, capa `Limite_Urbano_CPV24` — no un
punto inventado: es el polígono de límite urbano real, con población real).

Por qué `peak_terreno` y no `peak_worst` (el ranking que usa la sección 03
del registro): la vigilancia fina que corre esto es el ABM de **flujo
detrítico** (`runout_localidad.py`) — necesita terreno susceptible a
remociones en masa, no solo lluvia. Una comuna con gatillo climático alto
sobre terreno plano (`peak_terreno` bajo o sin dato) no es candidata útil
para esta simulación puntual (sí lo sería para inundación, que está fuera de
producción — ver docs/limitaciones).

Criterio de desempate y exclusión:
  - Requiere `peak_terreno` no nulo (si no, la comuna no tiene cobertura de
    susceptibilidad — no hay semilla de terreno, la corrida no sería
    informativa; mismo principio que el guard de `pick_susceptibility`).
  - Requiere un poblado censado al que referir la distancia del footprint.
    Se busca primero en `Limite_Urbano_CPV24` y, si la comuna no tiene área
    urbana, se cae a `Aldeas_CPV24` (campo `fuente_poblado` en la salida).
    El fallback se agregó el 2026-07-18: exigir polígono urbano excluía POR
    CONSTRUCCIÓN a las comunas rurales, y entre ellas estaba Alto del Carmen
    — el peak_terreno MÁS ALTO de las 260 comunas modeladas — que nunca
    había podido entrar a la lista.
  - Si el punto urbano exacto no tiene dato de susceptibilidad (puede pasar:
    el máximo de la comuna cae en una esquina cordillerana lejos del pueblo),
    se excluye esa comuna y se completa con la siguiente en el ranking —
    nunca se fuerza ni se reubica el punto a mano.
  - QA de cobertura (2026-07-18): cada seleccionada lleva
    `cobertura_raster_sospechosa`/`cobertura_raster_nota` — aviso, NO
    exclusión, de que el punto podría caer en el relleno del rectángulo DEM
    fuera de la cuenca real delineada (ver `raster_coverage_warning` en
    susc_overlay.py; hallazgo 2026-07-17, Canela/Coquimbo/Capitán Pastene/
    Contulmo/Caldera entre otros). Con falsos positivos conocidos en terreno
    plano real — revisar visualmente antes de excluir una localidad por esto.

Uso:
    python3 select_watchlist.py --top-n 20 [--out watchlist.json]
"""
import argparse
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from pyproj import Transformer

from susc_overlay import DOMAIN_BASINS, KINGSTON, raster_coverage_warning

FORECAST_DIR = os.path.expanduser("~/nowcast-forecast/latest")
COMUNA_GEOJSON = os.path.join(FORECAST_DIR, "comuna_impact", "comuna_impact.geojson")
CENSUS_GPKG = os.path.expanduser(
    "~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")


def log(msg):
    print(f"[watchlist] {msg}", flush=True)


def pick_susceptibility(domain, lon, lat):
    """El raster de cuenca con dato válido en el punto, MÁS CERCANO al
    centro de su propio rectángulo — no el primero en la lista.

    Los rectángulos de cuencas vecinas se solapan (ej. Copiapó y Huasco) y
    un punto puede caer dentro del rect de la cuenca equivocada CON dato
    válido (no NaN) sin pertenecer a ella en absoluto — devolver "el primero
    que tenga dato" elige la cuenca por orden de lista, no por pertenencia
    real (hallazgo 2026-07-17: Vallenar, en Huasco, elegía Copiapó/Ridge
    porque aparece antes en DOMAIN_BASINS). Sin polígono de cuenca real, la
    distancia al centro del rectángulo es la señal barata más razonable de
    pertenencia: la cuenca dueña casi siempre tiene el punto más cerca de su
    propio centro que una vecina cuyo rectángulo apenas lo alcanza en el
    borde.
    """
    candidates = []
    for rel in DOMAIN_BASINS.get(domain, []):
        path = os.path.join(KINGSTON, rel)
        if not os.path.exists(path):
            continue
        with rasterio.open(path) as src:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = t.transform(lon, lat)
            b = src.bounds
            if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                continue
            v = list(src.sample([(x, y)]))[0][0]
            if not np.isfinite(v):
                continue
            cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
            candidates.append((((x - cx) ** 2 + (y - cy) ** 2) ** 0.5, path))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def domain_for_region(cod_region):
    return "atacama" if cod_region == 3 else "chile-centro-sur"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(FORECAST_DIR, "watchlist.json"))
    a = ap.parse_args()

    log("cargando cruce comunal (comuna_impact.geojson)...")
    comunas = gpd.read_file(COMUNA_GEOJSON)
    comunas = comunas[comunas["peak_terreno"].notna()].copy()
    log(f"{len(comunas)} comunas con cobertura de terreno (de {len(gpd.read_file(COMUNA_GEOJSON))} en el barrido)")

    log("cargando localidades urbanas reales (Censo 2024, Limite_Urbano_CPV24)...")
    urb = pyogrio.read_dataframe(
        CENSUS_GPKG, layer="Limite_Urbano_CPV24",
        columns=["CUT", "ENTIDAD", "LOCALIDAD", "CATEGORIA", "n_per"])
    urb["CUT"] = urb["CUT"].astype(int)
    # una fila por comuna: la entidad urbana MÁS POBLADA (la ciudad/pueblo
    # cabecera; algunas comunas grandes tienen 2+ áreas urbanas conurbadas).
    urb_main = urb.sort_values("n_per", ascending=False).groupby("CUT", as_index=False).first()
    urb_main["centroid"] = urb_main.geometry.representative_point()
    urb_main["fuente_poblado"] = "urbano"
    log(f"{len(urb_main)} comunas con área urbana censada de {urb['CUT'].nunique()} con alguna entidad")

    # FALLBACK A ALDEAS (2026-07-18). Exigir polígono urbano dejaba fuera, POR
    # CONSTRUCCIÓN, a las comunas rurales cuyos poblados el censo cataloga como
    # aldeas — y no son casos marginales: Alto del Carmen tiene el peak_terreno
    # MÁS ALTO de las 260 comunas modeladas (0.999, 4.788 hab, valle andino del
    # Huasco) y jamás pudo entrar a la watchlist. Río Hurtado, misma situación
    # (0.974). Son 6 comunas y ~35 mil personas, pero concentradas justo en la
    # geografía donde los aluviones matan. `Aldeas_CPV24` trae el mismo esquema
    # (ENTIDAD, n_per, geometría), así que el reemplazo es directo.
    ald = pyogrio.read_dataframe(
        CENSUS_GPKG, layer="Aldeas_CPV24", columns=["CUT", "ENTIDAD", "CATEGORIA", "n_per"])
    ald["CUT"] = ald["CUT"].astype(int)
    ald_main = ald.sort_values("n_per", ascending=False).groupby("CUT", as_index=False).first()
    ald_main["centroid"] = ald_main.geometry.representative_point()
    ald_main["fuente_poblado"] = "aldea"
    ald_main["LOCALIDAD"] = ald_main["ENTIDAD"]
    faltantes = ald_main[~ald_main["CUT"].isin(set(urb_main["CUT"]))]
    log(f"{len(faltantes)} comunas SIN área urbana pero CON aldea censada — se suman por fallback")

    cols = ["CUT", "ENTIDAD", "n_per", "centroid", "fuente_poblado"]
    poblados = gpd.GeoDataFrame(
        pd.concat([urb_main[cols], faltantes[cols]], ignore_index=True))

    comunas["CUT"] = comunas["CUT"].astype(int)
    merged = comunas.merge(poblados, on="CUT", how="inner")
    n_ald = int((merged["fuente_poblado"] == "aldea").sum())
    log(f"{len(merged)} comunas cruzan terreno modelado + poblado censado "
        f"({n_ald} vía aldea)")

    merged = merged.sort_values("peak_terreno", ascending=False).reset_index(drop=True)

    selected, excluded = [], []
    for _, row in merged.iterrows():
        if len(selected) >= a.top_n:
            break
        lon, lat = row["centroid"].x, row["centroid"].y
        domain = domain_for_region(int(row["COD_REGION"]))
        susc = pick_susceptibility(domain, lon, lat)
        if susc is None:
            excluded.append({"comuna": row["COMUNA"], "razon":
                             "sin dato de susceptibilidad en el punto urbano exacto "
                             "(el máximo de la comuna cae lejos del pueblo)"})
            continue
        name = (row["ENTIDAD"] or row["COMUNA"]).lower().replace(" ", "_").replace("í", "i") \
            .replace("á", "a").replace("é", "e").replace("ó", "o").replace("ú", "u").replace("ñ", "n")
        cobertura = raster_coverage_warning(susc, lon, lat)
        selected.append({
            "name": name, "label": row["ENTIDAD"] or row["COMUNA"], "comuna": row["COMUNA"],
            "region": row["REGION"], "lon": round(lon, 5), "lat": round(lat, 5),
            "domain": domain, "peak_terreno": round(float(row["peak_terreno"]), 3),
            "peak_gfs": round(float(row["peak_gfs"]), 3) if row["peak_gfs"] is not None else None,
            "peak_ecmwf": round(float(row["peak_ecmwf"]), 3) if row["peak_ecmwf"] is not None else None,
            "peak_icon": round(float(row["peak_icon"]), 3) if "peak_icon" in row and row["peak_icon"] is not None else None,
            "poblacion_comuna": int(row["poblacion"]) if row["poblacion"] is not None else None,
            "poblacion_urbana": int(row["n_per"]) if row["n_per"] is not None else None,
            "fuente_poblado": row["fuente_poblado"],
            "cobertura_raster_sospechosa": cobertura["sospechoso"],
            "cobertura_raster_nota": cobertura["nota"],
        })

    n_sospechosas = sum(1 for s in selected if s["cobertura_raster_sospechosa"])
    log(f"seleccionadas {len(selected)} localidades (top-{a.top_n} por gatillo×susceptibilidad "
        f"real, con poblado censado — urbano o aldea) | {len(excluded)} excluidas por falta de dato en el punto exacto "
        f"| {n_sospechosas} con AVISO QA de cobertura de raster (relleno posible, revisar antes de confiar)")
    for s in selected:
        aviso = " ⚠ COBERTURA" if s["cobertura_raster_sospechosa"] else ""
        log(f"  {s['label']:<20} ({s['comuna']}, {s['region']}) terreno={s['peak_terreno']} "
            f"gfs={s['peak_gfs']} ecmwf={s['peak_ecmwf']} icon={s.get('peak_icon')} "
            f"pob_urbana={s['poblacion_urbana']}{aviso}")
    if excluded:
        log(f"excluidas: {[e['comuna'] for e in excluded]}")

    with open(a.out, "w") as f:
        json.dump({"selected": selected, "excluded": excluded,
                   "criterio": "top-N por peak_terreno (hazard climático × susceptibilidad real de "
                              "terreno), restringido a comunas con poblado censado — Limite_Urbano_CPV24 "
                              "y, si la comuna no tiene área urbana, Aldeas_CPV24 (ver fuente_poblado) — "
                              "y con dato de susceptibilidad en el punto exacto del poblado"},
                  f, indent=1, ensure_ascii=False)
    log(f"lista guardada en {a.out}")


if __name__ == "__main__":
    main()
