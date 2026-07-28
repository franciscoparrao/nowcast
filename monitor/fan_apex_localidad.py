#!/usr/bin/env python3
"""Clasificación aluvión/crecida por comuna vía FAN-APEX (destraba el centroide).

Paso 2 de docs/representacion-area-fanapex.md. Reemplaza la abstracción "una
comuna = un centroide urbano enganchado al cauce más cercano" (que sesga a
'crecida' porque el cauce cercano es el río del valle — HALLAZGO/HALLAZGO-2 en
melton_localidad.py) por "una comuna = el conjunto de fan-apexes cuya RUTA DE
FLUJO alcanza su área poblada".

Pipeline:
  1. Objetivo poblado de la comuna: polígono urbano (Limite_Urbano_CPV24) si
     existe; si la comuna es rural (p.ej. Alto del Carmen, sin polígono urbano),
     cae a las aldeas (Aldeas_CPV24) como puntos + buffer. Mismo orden que
     select_watchlist.py.
  2. Ventana de hidrología KINGSTON de la cuenca alrededor del objetivo.
  3. detect_fan_apex.detect(): fan-apexes (banda de área, Melton de la cuenca
     PROPIA, quiebre de pendiente, terminal por tributaria).
  4. Asociación por causalidad de drenaje: un apex pertenece a la comuna si su
     ruta de flujo D8 aguas abajo ENTRA al objetivo poblado ANTES de que la
     cuenca supere `assoc_area_cap_km2` (= antes de fundirse con el main-stem;
     si el flujo ya es el río grande, es crecida, no el abanico del apex).
  5. Clase de la comuna: se reportan TODOS los apexes asociados con su clase
     (R>0.3 rotulado: 'flujo detrítico' R>0.6 / 'debris flood' 0.3<R≤0.6). Si
     ninguno asocia, la comuna queda 'crecida fluvial / sin fan-apex' (el
     mecanismo local es fluvial, no de abanico) — que es la respuesta HONESTA,
     no el falso 'crecida' del centroide enganchado al río.

Decisiones (fijadas 2026-07-21 con el usuario): umbral Melton R>0.3 rotulado por
clase; asociación por ruta-de-flujo-alcanza-polígono (no radio).

Uso:
    python3 fan_apex_localidad.py --comuna "MONTE PATRIA" --basin 08_rio_limari \
        --lon -70.96184 --lat -30.69261
    python3 fan_apex_localidad.py --comuna "ALTO DEL CARMEN" --basin 06_rio_huasco \
        --lon -70.4859 --lat -28.75741   # rural → aldeas
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.features import rasterize
from pyproj import Transformer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from detect_fan_apex import detect, downstream_cell, run_hydrology  # noqa: E402

KINGSTON = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors"
CENSUS_GPKG = os.path.expanduser(
    "~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")
FAN_BUFFER_M = 400.0         # el abanico cubre a ~esta distancia del thalweg (D8 es 1 línea, el fan es 2D)
SEARCH_CAP_M = 20000.0       # límite de búsqueda del runout-a-alcanzar (no es un cap físico, solo acota la traza)
ALDEA_BUFFER_M = 400.0       # radio del objetivo para comunas rurales (punto de aldea)
WINDOW_KM = 26.0             # ventana de hidrología alrededor del objetivo

# Bandas de interpretación del runout-a-alcanzar (km) → qué tan creíble es que el
# abanico de ese apex llegue al pueblo. Un flujo detrítico corre típicamente 1-10 km.
def runout_band(km):
    if km is None:
        return "no alcanza"
    if km <= 2.0:
        return "sobre/al pie del abanico"       # inequívoco
    if km <= 8.0:
        return "alcanzable por flujo detrítico"  # plausible
    return "solo por flujo excepcional"          # lejano


_CENSUS_CACHE = {}   # layer → GeoDataFrame (leer la capa país entera es caro; se cachea)


def _census_layer(layer):
    if layer not in _CENSUS_CACHE:
        import geopandas as gpd
        cols = ["COMUNA", "geometry"] if layer == "Limite_Urbano_CPV24" else None
        _CENSUS_CACHE[layer] = gpd.read_file(CENSUS_GPKG, layer=layer, columns=cols)
    return _CENSUS_CACHE[layer]


def target_geoms(comuna, crs):
    """Geometrías-objetivo (población de la comuna) en `crs`. Devuelve
    (geoms, kind): polígonos urbanos, o puntos de aldea (rural), reproyectados."""
    urb = _census_layer("Limite_Urbano_CPV24")
    h = urb[urb["COMUNA"].str.upper() == comuna.upper()]
    if len(h):
        return list(h.to_crs(crs).geometry), "urbano"
    ald = _census_layer("Aldeas_CPV24")
    col = "COMUNA" if "COMUNA" in ald.columns else None
    if col is None:  # Aldeas puede venir sin COMUNA; se filtra por cercanía luego
        return list(ald.to_crs(crs).geometry), "aldea"
    h = ald[ald[col].str.upper() == comuna.upper()]
    return list(h.to_crs(crs).geometry), "aldea"


def _read_kingston_hydro(basin, x, y, half, dt_map):
    hydro = os.path.join(KINGSTON, basin, "hydrology")
    out = {}
    for name, dt in dt_map.items():
        with rasterio.open(os.path.join(hydro, name)) as s:
            win = from_bounds(x - half, y - half, x + half, y + half,
                              s.transform).round_offsets().round_lengths()
            out[name] = s.read(1, window=win).astype(dt)
    return out


def load_window(basin, lon, lat, window_km, hydro_source="surtgis", threshold=300):
    """Ventana de la cuenca centrada en el punto. `hydro_source`:
      - 'surtgis' (default): recomputa la hidrología con `surtgis hydrology all`
        sobre el DEM de la ventana — CONSISTENTE con la validación del paso 1.
      - 'kingston': lee la hidrología precomputada de KINGSTON (más rápido, pero
        su red de cauces usa otro umbral).
    Devuelve dem, fdir, facc, stream, transform, crs, res."""
    dem_p = os.path.join(KINGSTON, basin, "dem_30m.tif")
    with rasterio.open(dem_p) as s:
        crs, res = s.crs, s.res[0]
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        half = window_km * 1000 / 2
        win = from_bounds(x - half, y - half, x + half, y + half, s.transform)
        win = win.round_offsets().round_lengths()
        dem = s.read(1, window=win).astype(np.float64)
        transform = s.window_transform(win)
        dem_prof = dict(driver="GTiff", height=dem.shape[0], width=dem.shape[1], count=1,
                        dtype="float32", crs=crs, transform=transform, nodata=0.0,
                        compress="deflate")

    if hydro_source == "surtgis":
        import shutil
        import tempfile
        # Los temporales se BORRAN al salir: en un barrido de toda la watchlist
        # (~40 comunas) cada ventana deja un DEM + 3 rásters de hidrología, y
        # acumularlos llenaba /tmp sin que nadie los volviera a leer (las capas
        # ya quedan en memoria). El finally cubre también el fallo de surtgis.
        work = tempfile.mkdtemp(prefix="fanapex_win_")
        hydro_dir = None
        try:
            dem_path = os.path.join(work, "dem.tif")
            with rasterio.open(dem_path, "w", **dem_prof) as d:
                d.write(np.nan_to_num(dem, nan=0.0).astype(np.float32), 1)
            hydro_dir = run_hydrology(dem_path, threshold=threshold)
            def _rd(name, dt):
                with rasterio.open(os.path.join(hydro_dir, name)) as s:
                    return s.read(1).astype(dt)
            fdir = _rd("flow_direction_d8.tif", np.int16)
            facc = _rd("flow_accumulation.tif", np.float64)
            stream = _rd("stream_network.tif", np.uint8)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if hydro_dir:
                shutil.rmtree(hydro_dir, ignore_errors=True)
    else:
        h = _read_kingston_hydro(basin, x, y, half,
                                 {"flow_direction_d8.tif": np.int16,
                                  "flow_accumulation.tif": np.float64,
                                  "stream_network.tif": np.uint8})
        fdir, facc, stream = (h["flow_direction_d8.tif"], h["flow_accumulation.tif"],
                              h["stream_network.tif"])

    dem[dem <= 0] = np.nan
    # alinea formas (round_* pueden diferir en 1 fila/col entre capas)
    hh = min(dem.shape[0], fdir.shape[0], facc.shape[0], stream.shape[0])
    ww = min(dem.shape[1], fdir.shape[1], facc.shape[1], stream.shape[1])
    return (dem[:hh, :ww], fdir[:hh, :ww], facc[:hh, :ww], stream[:hh, :ww],
            transform, crs, res)


def target_mask(geoms, kind, dem_shape, transform, res):
    """Máscara booleana del objetivo poblado en la grilla de la ventana.
    Urbano Y aldea son POLÍGONOS reales (Limite_Urbano_CPV24 / Aldeas_CPV24 son
    ambos MultiPolygon del extent poblado) → se rasterizan directo. El disco de
    buffer solo es fallback defensivo si alguna geometría no fuese poligonal."""
    polys = [g for g in geoms if getattr(g, "geom_type", "").endswith("Polygon")]
    pts = [g for g in geoms if not getattr(g, "geom_type", "").endswith("Polygon")]
    m = np.zeros(dem_shape, dtype=bool)
    if polys:
        m |= rasterize([(g.__geo_interface__, 1) for g in polys], out_shape=dem_shape,
                       transform=transform, fill=0, dtype=np.uint8) == 1
    if pts:  # fallback: geometría de punto → disco de ALDEA_BUFFER_M
        rad = int(ALDEA_BUFFER_M / res)
        yy, xx = np.mgrid[0:dem_shape[0], 0:dem_shape[1]]
        for g in pts:
            c = int((g.centroid.x - transform.c) / transform.a)
            r = int((g.centroid.y - transform.f) / transform.e)
            if 0 <= r < dem_shape[0] and 0 <= c < dem_shape[1]:
                m |= (np.hypot(yy - r, xx - c) <= rad)
    return m


def runout_to_reach(apex, fdir, dist_to_target, res, fan_buffer_m, search_cap_m):
    """Runout-a-alcanzar: distancia recorrida por la ruta de flujo D8 desde el
    apex hasta que pasa por primera vez a ≤fan_buffer_m del objetivo. Como el D8
    es un thalweg de una línea y un flujo detrítico se abre en abanico 2D, se
    admite proximidad (fan_buffer), no entrada exacta. NO se aplica un cap físico
    arbitrario: se traza hasta search_cap_m (solo para acotar) y se REPORTA el
    runout — el consumidor juzga con `runout_band`.
    Devuelve (runout_m | None, min_dist_al_objetivo_m)."""
    r, c = apex["row"], apex["col"]
    max_steps = int(search_cap_m / res)
    best = float("inf")
    travelled = 0.0
    for _ in range(max_steps):
        d = dist_to_target[r, c]
        if d < best:
            best = d
        if d <= fan_buffer_m:
            return travelled, float(d)
        nxt = downstream_cell(fdir, r, c)
        if nxt is None:
            break
        travelled += res * (math.sqrt(2) if (nxt[0] != r and nxt[1] != c) else 1.0)
        r, c = nxt
    return None, float(best)


def classify(comuna, basin, lon, lat, *, window_km=WINDOW_KM, fan_buffer_m=FAN_BUFFER_M,
             search_cap_m=SEARCH_CAP_M, melton_min=0.3, hydro_source="surtgis"):
    from scipy.ndimage import distance_transform_edt
    dem, fdir, facc, stream, transform, crs, res = load_window(
        basin, lon, lat, window_km, hydro_source=hydro_source)
    geoms, kind = target_geoms(comuna, crs)
    mask = target_mask(geoms, kind, dem.shape, transform, res)
    if mask.sum() == 0:
        return {"comuna": comuna, "basin": basin, "error":
                "objetivo poblado sin celdas en la ventana (¿comuna/cuenca mal pareadas?)"}
    dist_to_target = distance_transform_edt(~mask) * res   # metros al objetivo

    apex, _ = detect(dem, fdir, facc, stream, res, area_min=0.5, area_max=100.0,
                     melton_min=melton_min, s_confined=12.0, s_fan=8.0, min_contrast=6.0,
                     reach_m=300.0, dedup_m=500.0)

    # lon/lat de cada apex (para sembrar el ABM en el paso 3)
    to4326 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    for a in apex:
        ax = transform.c + (a["col"] + 0.5) * transform.a
        ay = transform.f + (a["row"] + 0.5) * transform.e
        a["lon"], a["lat"] = [round(v, 6) for v in to4326.transform(ax, ay)]

    # runout-a-alcanzar por apex (los que alcanzan dentro del límite de búsqueda)
    reaching = []
    for a in apex:
        ro, mind = runout_to_reach(a, fdir, dist_to_target, res, fan_buffer_m, search_cap_m)
        if ro is not None:
            a = dict(a)
            a["runout_to_reach_m"] = round(ro, 0)
            a["runout_to_reach_km"] = round(ro / 1000.0, 2)
            a["banda"] = runout_band(ro / 1000.0)
            a["reach_dist_m"] = round(mind, 0)
            reaching.append(a)
    reaching.sort(key=lambda x: x["runout_to_reach_m"])  # el que alcanza más cerca, primero

    # clase de la comuna = la del apex que alcanza a MENOR runout (el más creíble),
    # con la banda de ese runout. Sin ninguno: crecida / sin fan-apex que alcance.
    if reaching:
        nearest = reaching[0]
        comuna_clase = nearest["clase"]
        min_runout_km = nearest["runout_to_reach_km"]
        banda = nearest["banda"]
    else:
        comuna_clase = "crecida fluvial / sin fan-apex que alcance"
        min_runout_km = None
        banda = "no alcanza"
    return {"comuna": comuna, "basin": basin, "target_kind": kind,
            "hydro_source": hydro_source, "n_apex_detectados": len(apex),
            "n_apex_alcanzan": len(reaching),
            "clase_comuna": comuna_clase,
            "runout_a_alcanzar_km": min_runout_km, "banda": banda,
            "apex_que_alcanzan": reaching}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--comuna", required=True)
    ap.add_argument("--basin", required=True, help="p.ej. 06_rio_huasco")
    ap.add_argument("--lon", type=float, required=True, help="centro de la ventana")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--window-km", type=float, default=WINDOW_KM)
    ap.add_argument("--fan-buffer-m", type=float, default=FAN_BUFFER_M)
    ap.add_argument("--search-cap-m", type=float, default=SEARCH_CAP_M,
                    help="límite de búsqueda del runout-a-alcanzar (no es cap físico)")
    ap.add_argument("--melton-min", type=float, default=0.3)
    ap.add_argument("--hydro-source", choices=["surtgis", "kingston"], default="surtgis",
                    help="surtgis: recomputa por ventana (consistente con la validación); "
                         "kingston: lee la hidrología precomputada (más rápido)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    res = classify(a.comuna, a.basin, a.lon, a.lat, window_km=a.window_km,
                   fan_buffer_m=a.fan_buffer_m, search_cap_m=a.search_cap_m,
                   melton_min=a.melton_min, hydro_source=a.hydro_source)
    if a.json:
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return
    if "error" in res:
        sys.exit(f"[{a.comuna}] {res['error']}")
    ro = res["runout_a_alcanzar_km"]
    print(f"[{res['comuna']}] cuenca {res['basin']} · objetivo {res['target_kind']} · "
          f"{res['n_apex_detectados']} apex detectados, {res['n_apex_alcanzan']} alcanzan")
    print(f"  CLASE (fan-apex): {res['clase_comuna']}"
          + (f" · fan más cercano alcanza a {ro:.1f} km ({res['banda']})" if ro is not None else ""))
    for i, x in enumerate(res["apex_que_alcanzan"][:8], 1):
        print(f"    apex {i}: Melton {x['melton']:.3f} ({x['clase']}) · área "
              f"{x['area_km2']} km² · alcanza a {x['runout_to_reach_km']:.1f} km — {x['banda']}")


if __name__ == "__main__":
    main()
