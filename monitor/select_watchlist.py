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

Robustez del umbral de exposición (2026-07-20): `peak_terreno` = gatillo×
susceptibilidad ESCALA con la forzante, así que el corte absoluto 0.85 (elegido
mirando La Serena, no derivado) es frágil —entre ciclos el nº de candidatas
oscila y dentro de un ciclo la lista es un filo (mover el umbral ±0.02 cambia
~4-5 de 20 comunas). No se cambia el valor a ciegas; se MIDE y EXPONE la
fragilidad: cada comuna de la lista lleva `margen_umbral` y `borderline`, y el
JSON trae `sensibilidad_umbral` con el barrido de estabilidad (Jaccard a ±0.02/
±0.05). Reproducible cada ciclo. Ajústese con `--umbral-exposicion` /
`--borderline-delta`.

Uso:
    python3 select_watchlist.py --top-n 20 [--umbral-exposicion 0.85]
        [--borderline-delta 0.05] [--out watchlist.json]
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


def exposure_list(pool, umbral, top_n):
    """Lista de exposición: entre las comunas con peak_terreno≥umbral (peligro
    realmente alto), las top_n por población. Aislada como función para poder
    reevaluarla a umbrales perturbados sin duplicar la lógica (barrido de
    sensibilidad)."""
    cand = [s for s in pool if s["peak_terreno"] >= umbral and s["poblacion_comuna"]]
    return sorted(cand, key=lambda s: -s["poblacion_comuna"])[:top_n]


def jaccard(a, b):
    """Similitud de conjunto entre dos listas de comunas (por `name`)."""
    A = {s["name"] for s in a}
    B = {s["name"] for s in b}
    return len(A & B) / len(A | B) if (A | B) else 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--umbral-exposicion", type=float, default=0.85,
                    help="peak_terreno mínimo para la lista de exposición; evita que "
                         "comunas planas grandes (Santiago, Maipú) entren por tamaño")
    ap.add_argument("--borderline-delta", type=float, default=0.05,
                    help="una comuna de la lista de exposición es 'borderline' si su "
                         "peak_terreno está a menos de este margen del umbral (cae de la "
                         "lista con un corte apenas más alto)")
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

    # POOL COMPLETO primero, rankings después. El loop anterior cortaba en
    # top_n MIENTRAS evaluaba peak_terreno, así que una comuna que solo
    # entraría por exposición ni siquiera se puntuaba. Ese corte prematuro es
    # lo que dejó La Serena (250 mil hab, peligro 0.865, con 7 desaparecidos el
    # 20-jul) fuera de todo: quedaba 44a por terreno y el loop paraba en la 20.
    # Ahora se evalúan TODAS las candidatas válidas una vez y de ese pool salen
    # las dos listas.
    merged = merged.sort_values("peak_terreno", ascending=False).reset_index(drop=True)
    pool, excluded = [], []
    for _, row in merged.iterrows():
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
        pool.append({
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

    # LISTA A — PELIGRO: dónde el terreno amplifica más la señal climática, sin
    # importar cuánta gente vive ahí. Es la que existía; sesga a valles rurales.
    lista_peligro = sorted(pool, key=lambda s: -s["peak_terreno"])[:a.top_n]

    # LISTA B — EXPOSICIÓN: entre las comunas con peligro REALMENTE alto
    # (umbral), la que tiene más población. El umbral es indispensable: ordenar
    # por peligro×población sin él pone primero Maipú/Puente Alto/Santiago,
    # comunas planas que puntúan solo por tamaño.
    #
    # SOBRE EL 0.85: `peak_terreno` = gatillo×susceptibilidad ESCALA con la
    # forzante, así que un corte absoluto es intrínsecamente frágil —entre
    # ciclos el nº de candidatas oscila (ciclo seco ~0, ciclo activo decenas) y
    # DENTRO de un ciclo la lista es un filo: mover el umbral ±0.02 intercambia
    # ~4-5 de las 20 comunas (barrido del 2026-07-20: Jaccard 0.85→0.83 ≈ 0.67,
    # 0.85→0.87 ≈ 0.60; ~9/20 comunas quedan a <0.05 del corte). El 0.85 se
    # eligió mirando La Serena, no derivado. En vez de fingir que es principiado
    # se MIDE y EXPONE la fragilidad (margen/borderline por comuna + bloque
    # `sensibilidad_umbral`), y por eso las dos listas van SEPARADAS en vez de
    # fundirse en un índice: la B es una segunda mirada explícita, con su
    # incertidumbre a la vista, no un reemplazo de la A.
    umbral = a.umbral_exposicion
    lista_exposicion = exposure_list(pool, umbral, a.top_n)

    # Robustez por comuna: margen al umbral y flag borderline (cae de la lista
    # con un corte apenas más alto). Mismo espíritu que cobertura_raster_
    # sospechosa: no excluye, avisa.
    delta = a.borderline_delta
    for s in lista_exposicion:
        s["margen_umbral"] = round(s["peak_terreno"] - umbral, 3)
        s["borderline"] = s["margen_umbral"] < delta

    # Barrido de sensibilidad del umbral: cuánto cambia la lista si el corte se
    # mueve ±0.02/±0.05. Se guarda en watchlist.json para que el consumidor vea
    # la estabilidad sin recalcular; reproducible cada ciclo (no un one-off).
    barrido = []
    for du in (-0.05, -0.02, 0.0, 0.02, 0.05):
        u2 = round(umbral + du, 3)
        L2 = exposure_list(pool, u2, a.top_n)
        barrido.append({
            "umbral": u2,
            "n_candidatas": sum(1 for s in pool if s["peak_terreno"] >= u2 and s["poblacion_comuna"]),
            "n_lista": len(L2),
            "jaccard_vs_umbral": round(jaccard(L2, lista_exposicion), 2),
        })
    n_borderline = sum(1 for s in lista_exposicion if s["borderline"])
    sensibilidad_umbral = {
        "umbral": umbral,
        "borderline_delta": delta,
        "n_candidatas": sum(1 for s in pool if s["peak_terreno"] >= umbral and s["poblacion_comuna"]),
        "n_borderline_en_lista": n_borderline,
        "nota": ("peak_terreno escala con la forzante, así que el corte absoluto es "
                 f"frágil: {n_borderline}/{len(lista_exposicion)} comunas de la lista "
                 f"están a <{delta} del umbral (borderline). `barrido.jaccard_vs_umbral` "
                 "<1 mide cuánto cambia la lista si el corte se mueve ±0.02/±0.05."),
        "barrido": barrido,
    }

    # Marca de procedencia: en qué lista(s) aparece cada comuna. Lo interesante
    # operativamente son las que están en UNA sola — la diferencia entre ambas
    # miradas es informativa, no ruido.
    en_peligro = {s["name"] for s in lista_peligro}
    en_exposicion = {s["name"] for s in lista_exposicion}
    for s in pool:
        s["en_lista"] = ([("peligro" if s["name"] in en_peligro else None),
                          ("exposicion" if s["name"] in en_exposicion else None)])
        s["en_lista"] = [x for x in s["en_lista"] if x]

    solo_exposicion = [s["label"] for s in lista_exposicion if s["name"] not in en_peligro]
    solo_peligro = [s["label"] for s in lista_peligro if s["name"] not in en_exposicion]

    log(f"pool de {len(pool)} comunas evaluadas | {len(excluded)} sin dato en el punto")
    log(f"── LISTA PELIGRO (top-{a.top_n} por peak_terreno):")
    for s in lista_peligro:
        aviso = " ⚠" if s["cobertura_raster_sospechosa"] else ""
        log(f"    {s['label']:<20} terreno={s['peak_terreno']} pob={s['poblacion_comuna']}{aviso}")
    log(f"── LISTA EXPOSICIÓN (top-{a.top_n} por población, con peak_terreno≥{umbral}):")
    for s in lista_exposicion:
        aviso = " ⚠" if s["cobertura_raster_sospechosa"] else ""
        bl = " ⚠borderline" if s["borderline"] else ""
        log(f"    {s['label']:<20} terreno={s['peak_terreno']} pob={s['poblacion_comuna']} "
            f"margen={s['margen_umbral']:+.3f}{bl}{aviso}")
    log(f"── SENSIBILIDAD del umbral {umbral}: {n_borderline}/{len(lista_exposicion)} "
        f"borderline (<{delta} del corte); estabilidad de la lista:")
    for b in barrido:
        marca = "  <<< umbral actual" if abs(b["umbral"] - umbral) < 1e-9 else ""
        log(f"    umbral={b['umbral']:.2f}  candidatas={b['n_candidatas']:>3}  "
            f"jaccard={b['jaccard_vs_umbral']:.2f}{marca}")
    log(f"── solo en EXPOSICIÓN (las que el criterio viejo perdía): {solo_exposicion}")
    log(f"── solo en PELIGRO (peligro alto, población baja): {solo_peligro}")

    # `selected` = unión de ambas, para no romper a los consumidores que leen
    # esa clave (runout/risk_map/reportes). Cada uno trae `en_lista`.
    union = {s["name"]: s for s in lista_peligro}
    for s in lista_exposicion:
        union.setdefault(s["name"], s)
    selected = list(union.values())

    with open(a.out, "w") as f:
        json.dump({"selected": selected,
                   "lista_peligro": lista_peligro,
                   "lista_exposicion": lista_exposicion,
                   "solo_exposicion": solo_exposicion,
                   "solo_peligro": solo_peligro,
                   "sensibilidad_umbral": sensibilidad_umbral,
                   "excluded": excluded,
                   "criterio": (
                       "DOS listas del mismo pool (comunas con poblado censado en "
                       "Limite_Urbano_CPV24 o, si es rural, Aldeas_CPV24, y con dato de "
                       "susceptibilidad en el punto). PELIGRO: top-N por peak_terreno "
                       "(gatillo×susceptibilidad), sesga a valles rurales. EXPOSICIÓN: "
                       f"top-N por población entre las de peak_terreno≥{umbral} (el umbral "
                       "evita que comunas planas grandes dominen por tamaño). `selected` es "
                       "la unión; `en_lista` en cada comuna dice de cuál viene. Motivación: "
                       "Alto del Carmen (peligro, rural, excluido por construcción) y La Serena "
                       "(250 mil hab, peligro alto, quedaba 44a) fueron los dos modos de falla "
                       "de selección del evento de julio 2026.")},
                  f, indent=1, ensure_ascii=False)
    log(f"lista guardada en {a.out} ({len(selected)} comunas en la unión)")


if __name__ == "__main__":
    main()
