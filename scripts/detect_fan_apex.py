#!/usr/bin/env python3
"""Detector automático de fan-apex (ápice de abanico aluvial) desde hidrología.

PROTOTIPO — paso 1 de docs/representacion-area-fanapex.md. Reemplaza la
abstracción "una comuna = un centroide urbano" (que engancha al río del valle y
sesga a 'crecida', ver HALLAZGO-2 en monitor/melton_localidad.py) por "una
comuna = el conjunto de fan-apexes": los puntos donde una quebrada empinada y
confinada desemboca sobre terreno de baja pendiente, que es de donde nace un
flujo detrítico y hacia donde hay que sembrar el ABM.

Un fan apex se define aquí como una celda de cauce que cumple tres cosas:
  1. Área aportante en la banda productiva de flujo detrítico (≈0.5–100 km²;
     cuencas de miles de km² son crecida, no aluvión).
  2. Razón de Melton de SU PROPIA cuenca > umbral (R>0.3 debris-flood, R>0.6
     flujo detrítico franco) — no la del río vecino. Esto es el fix del sesgo.
  3. Quiebre de pendiente del canal: el tramo aguas arriba es empinado
     (confinado, grad ≥ S_confined) y en el apex el canal se tiende
     (grad ≤ S_fan). Es la firma del paso montaña→abanico.

Entradas: hidrología georreferenciada en la convención de SurtGIS (D8: 0=sin
salida, 1-8 CCW desde el Este) — la misma que producen `surtgis hydrology all`
y los rásters de KINGSTON. Así el detector es drop-in tanto para cuencas
modeladas (hidrología precomputada) como para ventanas on-demand (DEM
Copernicus → surtgis).

VALIDACIÓN (--footprint obs.geojson): compara el apex auto-detectado más
cercano contra el apex OBSERVADO (celda más alta dentro del footprint mapeado,
misma definición que scripts/validate_footprint_vsl.py). Si el detector
recupera el apex a <1–2 celdas, la premisa se valida sin footprint nuevo.

Uso:
    # deriva hidrología del DEM con surtgis y detecta:
    python3 scripts/detect_fan_apex.py --dem dem.tif --out apex.geojson

    # hidrología ya calculada (KINGSTON o corrida previa):
    python3 scripts/detect_fan_apex.py --hydro <dir_con_flow_*_stream_network> \
        --dem dem.tif --out apex.geojson

    # con validación contra footprint observado:
    python3 scripts/detect_fan_apex.py --dem dem.tif \
        --footprint data/vsl_mudflow_obs.geojson --out apex.geojson
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.warp import transform_geom

# Reusa la maquinaria de Melton del monitor (misma convención D8).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor"))
from melton_cuencas import cuenca_aguas_arriba, clase_melton, D8_OFFSETS  # noqa: E402

# --- parámetros por defecto (documentados, ajustables por CLI) ----------------
AREA_MIN_KM2 = 0.5      # bajo esto no hay agua para un flujo significativo
AREA_MAX_KM2 = 100.0    # sobre esto la cuenca produce crecida, no aluvión
MELTON_MIN = 0.3        # R>0.3 debris-flood; R>0.6 flujo detrítico franco
S_CONFINED_DEG = 12.0   # tramo aguas arriba empinado (canal confinado)
S_FAN_DEG = 8.0         # en el apex el canal se tiende (cabeza de abanico)
MIN_CONTRAST_DEG = 6.0  # (grad_arriba − grad_apex): fuerza el quiebre, no un bache
REACH_M = 300.0         # longitud de tramo para promediar pendiente de canal
DEDUP_M = 500.0         # dos apexes a menos de esto = el mismo abanico


def run_hydrology(dem_path, threshold=500):
    """Corre `surtgis hydrology all` sobre el DEM → dir temporal con
    flow_direction_d8 / flow_accumulation / stream_network."""
    work = tempfile.mkdtemp(prefix="fanapex_hydro_")
    cmd = ["surtgis", "hydrology", "all", "--outdir", work,
           "--threshold", str(threshold), "--compress", dem_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"surtgis hydrology falló:\n{r.stderr[-800:]}")
    return work


def load_layers(dem_path, hydro_dir):
    with rasterio.open(dem_path) as s:
        dem = s.read(1).astype(np.float64)
        transform, crs, res = s.transform, s.crs, s.res[0]
    dem[dem <= -1e30] = np.nan
    def _rd(name):
        with rasterio.open(os.path.join(hydro_dir, name)) as s:
            return s.read(1)
    fdir = _rd("flow_direction_d8.tif").astype(np.int16)
    facc = _rd("flow_accumulation.tif").astype(np.float64)
    stream = _rd("stream_network.tif").astype(np.uint8)
    return dem, fdir, facc, stream, transform, crs, res


def slope_deg(dem, res):
    """Pendiente por gradiente central (grados). NaN-safe en bordes."""
    fill = np.nanmean(dem) if np.isfinite(dem).any() else 0.0
    gy, gx = np.gradient(np.nan_to_num(dem, nan=fill), res, res)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def downstream_cell(fdir, r, c):
    """Celda a la que drena (r,c) según D8, o None si no hay salida/borde."""
    code = int(fdir[r, c])
    if code < 1 or code > 8:
        return None
    dr, dc = D8_OFFSETS[code - 1]
    rr, cc = r + dr, c + dc
    if 0 <= rr < fdir.shape[0] and 0 <= cc < fdir.shape[1]:
        return rr, cc
    return None


def channel_grad_deg(dem, res, r, c, nxt):
    """Gradiente del canal en el paso (r,c)->nxt (grados)."""
    if nxt is None:
        return np.nan
    rr, cc = nxt
    dz = dem[r, c] - dem[rr, cc]
    dist = res * (math.sqrt(2) if (rr != r and cc != c) else 1.0)
    return math.degrees(math.atan(dz / dist)) if dist > 0 else np.nan


def reach_grad_downstream(dem, fdir, res, r, c, reach_m):
    """Gradiente medio del canal aguas ABAJO por ~reach_m."""
    steps = max(1, int(reach_m / res))
    grads, cur = [], (r, c)
    for _ in range(steps):
        nxt = downstream_cell(fdir, cur[0], cur[1])
        g = channel_grad_deg(dem, res, cur[0], cur[1], nxt)
        if not math.isnan(g):
            grads.append(g)
        if nxt is None:
            break
        cur = nxt
    return float(np.mean(grads)) if grads else np.nan


def upstream_main_grad(dem, fdir, facc, stream, res, r, c, reach_m):
    """Gradiente medio del canal principal aguas ARRIBA por ~reach_m.
    Sube por el vecino-cauce de mayor acumulación que drena hacia la celda."""
    steps = max(1, int(reach_m / res))
    grads, cur = [], (r, c)
    for _ in range(steps):
        best, best_facc = None, -1.0
        for k, (dr, dc) in enumerate(D8_OFFSETS, start=1):
            rr, cc = cur[0] - dr, cur[1] - dc   # vecino que APUNTA a cur
            if not (0 <= rr < fdir.shape[0] and 0 <= cc < fdir.shape[1]):
                continue
            if stream[rr, cc] != 1 or int(fdir[rr, cc]) != k:
                continue
            if facc[rr, cc] > best_facc:
                best, best_facc = (rr, cc), facc[rr, cc]
        if best is None:
            break
        g = channel_grad_deg(dem, res, best[0], best[1], cur)
        if not math.isnan(g):
            grads.append(g)
        cur = best
    return float(np.mean(grads)) if grads else np.nan


def catchment_melton(dem, fdir, r, c, area_km2):
    """Razón de Melton de la cuenca que drena a (r,c)."""
    mask = cuenca_aguas_arriba(fdir, r, c)
    z = dem[mask]
    z = z[np.isfinite(z)]
    if z.size < 2:
        return None, None
    relief = float(z.max() - z.min())
    R = relief / math.sqrt(area_km2 * 1e6)
    return R, relief


def terminal_apexes(cand_cells, fdir, facc, res, cell_km2, area_max):
    """Deja solo los apexes TERMINALES: si al bajar por el canal desde un apex
    se pasa por otro apex antes de que la cuenca supere area_max (= el canal se
    une al tronco / sale al valle), el de arriba es un knickpoint INTERIOR de la
    misma tributaria y se descarta. Recupera 'un apex por quebrada que sale al
    frente montañoso', no cada bache de pendiente."""
    apex_set = {(r, c) for (r, c, *_) in cand_cells}
    interior = set()
    for r, c, *_ in cand_cells:
        cur = downstream_cell(fdir, r, c)
        steps = 0
        while cur is not None and steps < 2000:
            if cur in apex_set:
                interior.add((r, c))   # tiene un apex aguas abajo → interior
                break
            if facc[cur[0], cur[1]] * cell_km2 > area_max:
                break                   # salió al tronco sin otro apex → terminal
            cur = downstream_cell(fdir, cur[0], cur[1])
            steps += 1
    return [t for t in cand_cells if (t[0], t[1]) not in interior]


def detect(dem, fdir, facc, stream, res, *, area_min, area_max, melton_min,
           s_confined, s_fan, min_contrast, reach_m, dedup_m, near=None):
    cell_km2 = (res * res) / 1e6
    slp = slope_deg(dem, res)

    # filtro opcional: solo candidatos a <near[2] m del punto (near[0]=row, near[1]=col)
    near_rc = None
    if near is not None:
        near_rc = (near[0], near[1], near[2] / res)  # radio en celdas

    # 1+3: candidatos por banda de área y quiebre de pendiente de canal (barato).
    srows, scols = np.where(stream == 1)
    cand = []
    for r, c in zip(srows.tolist(), scols.tolist()):
        if near_rc is not None and math.hypot(r - near_rc[0], c - near_rc[1]) > near_rc[2]:
            continue
        area = facc[r, c] * cell_km2
        if not (area_min <= area <= area_max):
            continue
        g_down = reach_grad_downstream(dem, fdir, res, r, c, reach_m)
        g_up = upstream_main_grad(dem, fdir, facc, stream, res, r, c, reach_m)
        if math.isnan(g_down) or math.isnan(g_up):
            continue
        # quiebre: empinado arriba, tendido en el apex, con contraste real
        if g_up >= s_confined and g_down <= s_fan and (g_up - g_down) >= min_contrast:
            cand.append((r, c, area, g_up, g_down))

    # solo apexes terminales (un abanico por tributaria, el del frente montañoso)
    cand = terminal_apexes(cand, fdir, facc, res, cell_km2, area_max)
    cand.sort(key=lambda t: -t[2])  # área desc: el abanico principal manda en el dedup

    # 2: filtro de Melton (caro: cuenca aguas arriba) solo sobre candidatos.
    apex = []
    for r, c, area, g_up, g_down in cand:
        R, relief = catchment_melton(dem, fdir, r, c, area)
        if R is None or R < melton_min:
            continue
        apex.append(dict(row=r, col=c, area_km2=round(area, 2), melton=round(R, 3),
                         clase=clase_melton(R), relief_m=round(relief, 0),
                         slope_up_deg=round(g_up, 1), slope_down_deg=round(g_down, 1)))

    # 4: dedup espacial (un apex por abanico).
    kept = []
    for a in apex:  # ya ordenados por área desc
        rr, cc = a["row"], a["col"]
        if all(math.hypot((rr - k["row"]) * res, (cc - k["col"]) * res) > dedup_m for k in kept):
            kept.append(a)
    return kept, slp


def pixel_to_xy(transform, r, c):
    return transform.c + (c + 0.5) * transform.a, transform.f + (r + 0.5) * transform.e


def observed_apex(dem, mask):
    """Apex observado = celda más alta del DEM dentro de la máscara del footprint."""
    z = np.where(mask, dem, np.nan)
    if not np.isfinite(z).any():
        return None
    r, c = np.unravel_index(np.nanargmax(z), z.shape)
    return int(r), int(c), float(z[r, c])


def footprint_mask(dem, transform, crs, *, geojson=None, raster=None):
    """Máscara booleana del footprint observado, en la grilla del DEM.
    Acepta polígono (geojson, se rasteriza) o ráster (>0.5)."""
    if raster:
        with rasterio.open(raster) as s:
            m = s.read(1)
        if m.shape != dem.shape:
            sys.exit(f"footprint ráster {m.shape} no calza con el DEM {dem.shape}")
        return m > 0.5
    import geopandas as gpd
    obs = gpd.read_file(geojson).to_crs(crs)
    geoms = [g.__geo_interface__ for g in obs.geometry]
    return rasterize([(g, 1) for g in geoms], out_shape=dem.shape,
                     transform=transform, fill=0, dtype=np.uint8) == 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dem", required=True, help="DEM GeoTIFF (m)")
    ap.add_argument("--hydro", help="dir con flow_direction_d8/flow_accumulation/"
                                    "stream_network .tif; si falta, se corre surtgis")
    ap.add_argument("--threshold", type=int, default=500,
                    help="umbral de red de cauces en celdas (si se corre surtgis)")
    ap.add_argument("--footprint", help="footprint observado (geojson) para validar")
    ap.add_argument("--footprint-raster", help="footprint observado (ráster >0.5) para validar")
    ap.add_argument("--out", help="geojson de salida con los apexes")
    ap.add_argument("--area-min-km2", type=float, default=AREA_MIN_KM2)
    ap.add_argument("--area-max-km2", type=float, default=AREA_MAX_KM2)
    ap.add_argument("--melton-min", type=float, default=MELTON_MIN)
    ap.add_argument("--s-confined", type=float, default=S_CONFINED_DEG)
    ap.add_argument("--s-fan", type=float, default=S_FAN_DEG)
    ap.add_argument("--min-contrast", type=float, default=MIN_CONTRAST_DEG)
    ap.add_argument("--reach-m", type=float, default=REACH_M)
    ap.add_argument("--dedup-m", type=float, default=DEDUP_M)
    ap.add_argument("--near-lon", type=float, help="restringe candidatos a <near-km de este punto")
    ap.add_argument("--near-lat", type=float)
    ap.add_argument("--near-km", type=float, default=6.0)
    a = ap.parse_args()

    hydro = a.hydro or run_hydrology(a.dem, a.threshold)
    dem, fdir, facc, stream, transform, crs, res = load_layers(a.dem, hydro)
    print(f"DEM {dem.shape[0]}x{dem.shape[1]} @ {res:.0f} m · CRS {crs} · "
          f"cauce {int((stream==1).sum())} celdas")

    near = None
    if a.near_lon is not None and a.near_lat is not None:
        from pyproj import Transformer
        t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        nx, ny = t.transform(a.near_lon, a.near_lat)
        nr, nc = rasterio.transform.rowcol(transform, nx, ny)
        near = (nr, nc, a.near_km * 1000.0)
        print(f"filtro near: pueblo en celda ({nr},{nc}), radio {a.near_km} km")

    apex, _ = detect(dem, fdir, facc, stream, res,
                     area_min=a.area_min_km2, area_max=a.area_max_km2,
                     melton_min=a.melton_min, s_confined=a.s_confined, s_fan=a.s_fan,
                     min_contrast=a.min_contrast, reach_m=a.reach_m, dedup_m=a.dedup_m,
                     near=near)
    for x in apex:
        lon_x, lat_y = pixel_to_xy(transform, x["row"], x["col"])
        x["x"], x["y"] = lon_x, lat_y
    print(f"\n{len(apex)} fan-apex detectados (área {a.area_min_km2}-{a.area_max_km2} km², "
          f"Melton>{a.melton_min}, quiebre {a.s_confined}->{a.s_fan}°):")
    print(f"  {'#':>2} {'area_km2':>8} {'Melton':>7} {'clase':16} {'relieve':>7} "
          f"{'slp_up':>6} {'slp_dn':>6}")
    for i, x in enumerate(sorted(apex, key=lambda t: -t["area_km2"]), 1):
        print(f"  {i:>2} {x['area_km2']:>8.2f} {x['melton']:>7.3f} {x['clase']:16} "
              f"{x['relief_m']:>7.0f} {x['slope_up_deg']:>6.1f} {x['slope_down_deg']:>6.1f}")

    if a.footprint or a.footprint_raster:
        mask = footprint_mask(dem, transform, crs, geojson=a.footprint, raster=a.footprint_raster)
        obs = observed_apex(dem, mask)
        if obs is None:
            print("\n[validación] footprint sin celda válida en la ventana DEM")
        else:
            orow, ocol, oz = obs
            ox, oy = pixel_to_xy(transform, orow, ocol)
            if apex:
                d = min(math.hypot((x["row"] - orow) * res, (x["col"] - ocol) * res) for x in apex)
                nearest = min(apex, key=lambda x: math.hypot((x["row"] - orow) * res, (x["col"] - ocol) * res))
                print(f"\n[validación] apex observado: celda ({orow},{ocol}) elev {oz:.0f} m")
                print(f"  distancia al apex detectado más cercano: {d:.0f} m ({d/res:.1f} celdas)")
                print(f"  ese apex: área {nearest['area_km2']} km², Melton {nearest['melton']} "
                      f"({nearest['clase']})")
            else:
                print("\n[validación] no se detectó ningún apex — no comparable")

    if a.out:
        feats = [{"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [x["x"], x["y"]]},
                  "properties": {k: v for k, v in x.items() if k not in ("x", "y")}}
                 for x in apex]
        fc = {"type": "FeatureCollection",
              "crs": {"type": "name", "properties": {"name": str(crs)}},
              "features": feats}
        with open(a.out, "w") as f:
            json.dump(fc, f, indent=1, ensure_ascii=False)
        print(f"\n→ {a.out} ({len(feats)} apexes)")


if __name__ == "__main__":
    main()
