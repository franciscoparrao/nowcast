#!/usr/bin/env python3
"""Clasificación aluvión / crecida por LOCALIDAD (razón de Melton, estática).

El sistema corre un ABM de flujo detrítico (`runout_localidad.py`) en TODA
localidad de la watchlist y presenta su footprint como si el fenómeno fuera
siempre un aluvión. Pero en la literatura la geometría de la cuenca decide el
fenómeno: cuencas empinadas y compactas producen flujos detríticos; grandes y
tendidas, crecidas fluviales. El discriminador estándar es la razón de Melton
R = (Hmax − Hmin)/√A (Melton 1965), ya usada en `melton_cuencas.py`:

    R > 0.6          flujo detrítico (aluvión)
    0.3 < R ≤ 0.6    debris flood (hiperconcentrado)
    R ≤ 0.3          crecida fluvial

Este módulo lleva esa clasificación al PUNTO de cada localidad: engancha el
punto al cauce mapeado más cercano (`stream_network.tif`), toma el área
aportante en esa celda (de `flow_accumulation.tif`) y el relieve de la cuenca,
y devuelve la clase. Es MORFOMETRÍA ESTÁTICA (no cambia por ciclo), así que se
cachea entre ciclos.

POR QUÉ importa operativamente: donde la clase es `crecida fluvial`, el
footprint del ABM de flujo detrítico es probablemente el MODELO EQUIVOCADO —
igual que `footprint_vacio` avisa que no hay producto, `fenomeno=crecida`
avisa que el producto que hay es de otro fenómeno. Complementa (no reemplaza)
al gatillo climático: filtra qué PUEDE ser aluvión; el gatillo dice cuándo.

Costo acotado: el área sale directo de `facc` (sin backtrace); solo se retrocede
la cuenca (para el relieve) si el área ≤ MAX_BACKTRACE_KM2 — un cauce principal
(miles de km²) es crecida con cualquier relieve andino realista, así que no hace
falta recorrer sus millones de celdas.

Limitación honesta: sin una red de cauces por nombre, el enganche va al cauce
MÁS CERCANO. Un pueblo en el abanico de una quebrada lateral pero a <1 km del
río principal puede engancharse al río y clasificarse como crecida. Se reporta
`snap_dist_m` y `catchment_area_km2` para que ese caso sea visible.

HALLAZGO (2026-07-21, barrido sobre el universo real de localidades). Ese sesgo
NO es marginal: con enganche al cauce más cercano, sitios de aluviones MORTALES
salen clasificados como crecida —Alto del Carmen (aluviones 2015) R=0.21;
La Serena / Pueblo Islón (7 desaparecidos por aluvión, jul-2026) R=0.17; Monte
Patria R=0.15— porque el cauce mapeado más cercano (umbral ~1 km²) es el del
VALLE, no la quebrada lateral empinada que produjo el aluvión. El sesgo es
sistemático hacia "crecida". La alternativa (Melton MÁXIMO entre los cauces
cercanos) sesga al revés: cualquier cárcava chica empinada da R>0.6 y casi todo
pasa a "flujo detrítico". Por eso este módulo es DIAGNÓSTICO, NO se cablea como
rótulo operacional del reporte: un clasificador que llama "crecida" a Alto del
Carmen sería peor que no rotular. La clasificación por localidad CONFIABLE exige
la red de cauces por nombre (`cauces.json` de OSM/Overpass) que usa
`melton_cuencas.py` —elegir la quebrada correcta, no la más cercana— y validarla
contra los eventos documentados. Lo único robusto que sí sale de aquí es el
"indeterminado / sin cauce mapeado cercano" de las localidades costeras
(Valparaíso, Viña, Concón, Quilpué…), consistente con el hueco "costa sin
modelo".

Uso:
    python3 melton_localidad.py --susc <ruta_susc.tif> --lon -70.7 --lat -30.6
    python3 melton_localidad.py --watchlist ~/nowcast-forecast/latest/watchlist.json
"""

import argparse
import json
import os
import sys

import numpy as np
import rasterio
from pyproj import Transformer

from susc_overlay import KINGSTON
from melton_cuencas import cuenca_aguas_arriba, clase_melton

SNAP_RADIUS_M = 1000.0
# Sobre este umbral la cuenca es un cauce principal: con área ≥ 500 km², R > 0.3
# exigiría > 6700 m de relieve, imposible en un solo drenaje andino → crecida
# garantizada. Evita retroceder millones de celdas del main stem.
MAX_BACKTRACE_KM2 = 500.0
CACHE_PATH = os.path.expanduser("~/nowcast-forecast/melton_cache.json")


def _hydro_dir(susc_path):
    """Directorio hydrology/ de la cuenca del raster de susceptibilidad elegido
    (mismo árbol KINGSTON). Devuelve (basin, hydro_dir) o (basin, None) si la
    cuenca no tiene hidrología propia (cuencas transfer: choapa, costa)."""
    rel = os.path.relpath(os.path.realpath(susc_path), KINGSTON)
    basin = rel.split(os.sep)[0]
    hydro = os.path.join(KINGSTON, basin, "hydrology")
    ok = all(os.path.exists(os.path.join(hydro, f)) for f in
             ("flow_direction_d8.tif", "flow_accumulation.tif", "stream_network.tif"))
    dem = os.path.join(KINGSTON, basin, "dem_30m.tif")
    return basin, (hydro if ok and os.path.exists(dem) else None)


def _classify(susc_path, lon, lat, snap_radius_m=SNAP_RADIUS_M):
    basin, hydro = _hydro_dir(susc_path)
    base = {"basin": basin, "clase": "indeterminado", "melton": None,
            "catchment_area_km2": None, "relief_m": None, "snap_dist_m": None}
    if hydro is None:
        base["note"] = f"cuenca {basin} sin hidrología propia (transfer) — no clasificable"
        return base

    p_fdir = os.path.join(hydro, "flow_direction_d8.tif")
    p_facc = os.path.join(hydro, "flow_accumulation.tif")
    p_stream = os.path.join(hydro, "stream_network.tif")
    p_dem = os.path.join(KINGSTON, basin, "dem_30m.tif")

    with rasterio.open(p_facc) as s:
        crs, transform, res = s.crs, s.transform, s.res[0]
        t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = t.transform(lon, lat)
        r0, c0 = rasterio.transform.rowcol(transform, x, y)
        nrow, ncol = s.height, s.width
        if not (0 <= r0 < nrow and 0 <= c0 < ncol):
            base["note"] = "punto fuera de la grilla de hidrología de su cuenca"
            return base
        facc = s.read(1)
    with rasterio.open(p_stream) as s:
        stream = s.read(1)

    # Enganche al cauce mapeado más cercano dentro del radio.
    rad = int(round(snap_radius_m / res))
    r_lo, r_hi = max(0, r0 - rad), min(nrow, r0 + rad + 1)
    c_lo, c_hi = max(0, c0 - rad), min(ncol, c0 + rad + 1)
    sub = stream[r_lo:r_hi, c_lo:c_hi]
    srows, scols = np.where(sub == 1)
    if srows.size == 0:
        base["note"] = f"sin cauce mapeado a menos de {snap_radius_m:.0f} m del punto"
        return base
    grow, gcol = srows + r_lo, scols + c_lo
    d = np.hypot((grow - r0).astype(float), (gcol - c0).astype(float))
    j = int(np.argmin(d))
    rs, cs = int(grow[j]), int(gcol[j])
    snap_dist_m = float(d[j] * res)

    cell_km2 = (res * res) / 1.0e6
    area_km2 = float(facc[rs, cs]) * cell_km2
    base["snap_dist_m"] = round(snap_dist_m, 0)
    base["catchment_area_km2"] = round(area_km2, 1)

    if area_km2 <= 0:
        base["note"] = "área aportante nula en la celda de cauce enganchada"
        return base

    if area_km2 > MAX_BACKTRACE_KM2:
        # Cauce principal: crecida garantizada, no hace falta el relieve exacto.
        base["clase"] = "crecida fluvial"
        base["note"] = (f"cuenca grande ({area_km2:.0f} km² > {MAX_BACKTRACE_KM2:.0f}) "
                        f"— cauce principal, R≤0.3 garantizado (no se retrocede el DEM)")
        return base

    # Backtrace acotado para el relieve de la cuenca.
    with rasterio.open(p_fdir) as s:
        fdir = s.read(1)
    with rasterio.open(p_dem) as s:
        dem = s.read(1)
    mask = cuenca_aguas_arriba(fdir, rs, cs)
    z = dem[mask]
    z = z[np.isfinite(z)]
    if z.size < 2:
        base["note"] = "cuenca sin DEM válido para el relieve"
        return base
    relief_m = float(z.max() - z.min())
    R = relief_m / np.sqrt(area_km2 * 1.0e6)
    base["clase"] = clase_melton(R)
    base["melton"] = round(R, 3)
    base["relief_m"] = round(relief_m, 0)
    base["note"] = None
    return base


def _load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(open(CACHE_PATH))
        except (OSError, ValueError):
            return {}
    return {}


def classify_point(susc_path, lon, lat, snap_radius_m=SNAP_RADIUS_M, cache=None):
    """Clasificación de Melton en el punto, con cache estática (persistente
    entre ciclos porque la morfometría no cambia). `cache` puede pasarse ya
    cargado para lotes; si es None se lee/escribe el archivo de una."""
    basin, _ = _hydro_dir(susc_path)
    key = f"{basin}|{round(lon, 4)}|{round(lat, 4)}|{int(snap_radius_m)}"
    own = cache is None
    if own:
        cache = _load_cache()
    if key in cache:
        return cache[key]
    res = _classify(susc_path, lon, lat, snap_radius_m)
    cache[key] = res
    if own:
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            json.dump(cache, open(CACHE_PATH, "w"), indent=1, ensure_ascii=False)
        except OSError:
            pass
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--susc", help="raster de susceptibilidad de la cuenca del punto")
    ap.add_argument("--lon", type=float)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--snap-radius-m", type=float, default=SNAP_RADIUS_M)
    ap.add_argument("--watchlist", help="clasifica cada localidad de una watchlist.json "
                                        "(usa su campo susceptibility de los runouts si existe)")
    a = ap.parse_args()

    if a.watchlist:
        wl = json.load(open(a.watchlist))
        cache = _load_cache()
        print(f"{'localidad':22s} {'clase':16s} {'Melton':>7s} {'area_km2':>9s} "
              f"{'relieve':>8s} {'snap_m':>7s}")
        for s in wl.get("selected", []):
            # el runout ya eligió la susceptibilidad de la cuenca; si no hay
            # runout todavía, se salta (no se re-elige aquí).
            rp = os.path.join(os.path.expanduser("~/nowcast-forecast/runouts"),
                              s["name"], "resumen.json")
            if not os.path.exists(rp):
                continue
            susc = json.load(open(rp)).get("susceptibility")
            if not susc:
                continue
            m = classify_point(susc, s["lon"], s["lat"], a.snap_radius_m, cache)
            print(f"{s['label'][:21]:22s} {m['clase']:16s} "
                  f"{('%.3f' % m['melton']) if m['melton'] is not None else '—':>7s} "
                  f"{('%.1f' % m['catchment_area_km2']) if m['catchment_area_km2'] is not None else '—':>9s} "
                  f"{('%.0f' % m['relief_m']) if m['relief_m'] is not None else '—':>8s} "
                  f"{('%.0f' % m['snap_dist_m']) if m['snap_dist_m'] is not None else '—':>7s}")
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            json.dump(cache, open(CACHE_PATH, "w"), indent=1, ensure_ascii=False)
        except OSError:
            pass
        return

    if a.susc is None or a.lon is None or a.lat is None:
        sys.exit("faltan --susc/--lon/--lat (o usar --watchlist)")
    m = classify_point(a.susc, a.lon, a.lat, a.snap_radius_m)
    print(json.dumps(m, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
