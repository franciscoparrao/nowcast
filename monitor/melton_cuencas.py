#!/usr/bin/env python3
"""Índice morfométrico por cuenca de quebrada: razón de Melton + área aportante.

POR QUÉ (hallazgo 2026-07-19/20). El producto de peligro del sistema es
PURAMENTE LOCAL: gatillo × susceptibilidad en el píxel, sin ninguna
información de la cuenca que alimenta el cauce. Verificado contra el evento
real del Huasco, eso lo lleva a rankear primero quebradas de 1-19 km² —
físicamente incapaces de producir un flujo detrítico significativo, no
tienen de dónde sacar el agua— por encima de la Quebrada Maitencillo
(533 km², la que efectivamente activó y forzó evacuaciones).

Pero "más área = más peligro" TAMPOCO es correcto, y por eso este script no
usa el área a secas: en la literatura las cuencas grandes y tendidas
producen CRECIDAS, no aluviones. El discriminador estándar es la razón de
Melton, R = (Hmax − Hmin) / √A (Melton 1965), con los cortes de uso común:

    R > 0.6          propensa a flujo detrítico
    0.3 < R < 0.6    flujo hiperconcentrado / debris flood
    R < 0.3          crecida fluvial

Es morfometría estática: NO reemplaza al gatillo climático, lo complementa.
La idea es filtrar qué cuencas pueden producir un aluvión, y dejar que el
gatillo diga cuándo.

Uso:
    python3 melton_cuencas.py --cauces cauces.json --basin 06_rio_huasco
donde `cauces.json` es {"nombre": [[lon,lat],...]} (p.ej. de OSM/Overpass).
"""

import argparse
import json
import os
import sys

import numpy as np
import rasterio
from pyproj import Transformer

KINGSTON = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors"

# D8 de SurtGIS: código 1..8 = E, NE, N, NW, W, SW, S, SE (antihorario desde
# el Este). Ver crates/algorithms/src/hydrology/d8.rs, que es la fuente de
# verdad — NO es la convención de potencias de 2 de ArcGIS.
D8_OFFSETS = [(0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1)]


def cuenca_aguas_arriba(fdir, r0, c0):
    """Celdas que drenan al punto (r0,c0), por retroceso D8.

    Iterativo con pila explícita: una cuenca de 500 km² a 30 m son ~600 mil
    celdas y la recursión revienta el stack de Python mucho antes.
    """
    nrow, ncol = fdir.shape
    vistas = np.zeros(fdir.shape, dtype=bool)
    pila = [(r0, c0)]
    vistas[r0, c0] = True
    while pila:
        r, c = pila.pop()
        for k, (dr, dc) in enumerate(D8_OFFSETS, start=1):
            rr, cc = r - dr, c - dc          # vecino que APUNTA hacia (r,c)
            if not (0 <= rr < nrow and 0 <= cc < ncol) or vistas[rr, cc]:
                continue
            if int(fdir[rr, cc]) == k:       # ese vecino drena hacia acá
                vistas[rr, cc] = True
                pila.append((rr, cc))
    return vistas


def clase_melton(R):
    if R is None:
        return "—"
    if R > 0.6:
        return "flujo detrítico"
    if R > 0.3:
        return "debris flood"
    return "crecida fluvial"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cauces", required=True, help='JSON {"nombre": [[lon,lat],...]}')
    ap.add_argument("--basin", default="06_rio_huasco")
    ap.add_argument("--susc", default=None, help="raster de susceptibilidad (opcional)")
    ap.add_argument("--min-vertices", type=int, default=10)
    a = ap.parse_args()

    base = os.path.join(KINGSTON, a.basin)
    p_fdir = os.path.join(base, "hydrology", "flow_direction_d8.tif")
    p_facc = os.path.join(base, "hydrology", "flow_accumulation.tif")
    p_dem = os.path.join(base, "dem_30m.tif")
    for p in (p_fdir, p_facc, p_dem):
        if not os.path.exists(p):
            sys.exit(f"falta {p}")

    with rasterio.open(p_fdir) as s:
        fdir = s.read(1)
        crs, transform, res = s.crs, s.transform, s.res[0]
    with rasterio.open(p_facc) as s:
        facc = s.read(1)
    with rasterio.open(p_dem) as s:
        dem = s.read(1)
    susc = None
    if a.susc and os.path.exists(a.susc):
        with rasterio.open(a.susc) as s:
            if s.shape == dem.shape:
                susc = s.read(1)
            else:
                print("aviso: susceptibilidad con otra grilla, se omite", file=sys.stderr)

    cell_km2 = (res * res) / 1.0e6
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    cauces = json.load(open(a.cauces))

    print(f"{'cauce':28s} {'area':>9s} {'relieve':>8s} {'Melton':>7s}  {'clase':16s} {'susc_med':>8s}")
    filas = []
    for nombre, pts in sorted(cauces.items(), key=lambda kv: -len(kv[1])):
        if len(pts) < a.min_vertices:
            continue
        # desembocadura = vértice con MAYOR área aportante
        mejor = None
        for lon, lat in pts:
            x, y = t.transform(lon, lat)
            r, c = rasterio.transform.rowcol(transform, x, y)
            if not (0 <= r < fdir.shape[0] and 0 <= c < fdir.shape[1]):
                continue
            v = facc[r, c]
            if np.isfinite(v) and (mejor is None or v > mejor[0]):
                mejor = (float(v), r, c)
        if mejor is None:
            continue
        _, r0, c0 = mejor
        m = cuenca_aguas_arriba(fdir, r0, c0)
        n = int(m.sum())
        area = n * cell_km2
        z = dem[m]
        z = z[np.isfinite(z)]
        if z.size < 2 or area <= 0:
            continue
        relieve = float(z.max() - z.min())
        R = relieve / np.sqrt(area * 1.0e6)   # relieve en m, área en m² → adimensional
        smed = float(np.nanmedian(susc[m])) if susc is not None else None
        filas.append({"cauce": nombre, "area_km2": round(area, 1),
                      "relieve_m": round(relieve, 0), "melton": round(R, 3),
                      "clase": clase_melton(R), "susc_mediana": round(smed, 3) if smed else None,
                      "n_celdas": n})
        print(f"{nombre[:27]:28s} {area:8.1f}km² {relieve:7.0f}m {R:7.3f}  {clase_melton(R):16s} "
              f"{smed if smed is not None else float('nan'):8.3f}")

    print()
    print("Melton (Melton 1965): R>0.6 flujo detrítico · 0.3-0.6 debris flood · <0.3 crecida")
    print("Morfometría ESTÁTICA: filtra qué cuencas PUEDEN producir un aluvión;")
    print("el gatillo climático sigue diciendo cuándo.")
    return filas


if __name__ == "__main__":
    main()
