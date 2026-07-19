#!/usr/bin/env python3
"""Genera los puntos de un barrido de cuenca para el ABM de runout.

`runout_localidad.py` corre una ventana de 12 km centrada en UN punto. Para
cubrir una cuenca entera hay que sembrar puntos a lo largo de sus cauces y
mosaicar los footprints (`basin_runout_mosaic.py`). Este script elige esos
puntos de forma reproducible en vez de a ojo.

Criterio: se toman las celdas cuya área contribuyente supera `--min-km2`
(el cauce principal y sus tributarios grandes — donde un flujo detrítico
tiene de dónde venir y por dónde canalizarse) y se las submuestrea con un
espaciamiento mínimo de `--spacing-km`. Con ventana de 12 km, un
espaciamiento de 6 km deja ~50% de traslape entre ventanas vecinas, que es
lo que evita las costuras en el mosaico.

Los puntos se ordenan de aguas abajo hacia aguas arriba (área contribuyente
decreciente), así que truncar la lista con `--max-points` conserva primero
los tramos de mayor caudal, que son los que importan.

Uso:
    python3 basin_sweep_points.py --facc <flow_accumulation.tif> \
        --name-prefix limari_valle --min-km2 50 --spacing-km 6
"""

import argparse
import json
import sys

import numpy as np
import rasterio
from pyproj import Transformer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facc", required=True, help="flow_accumulation.tif de la cuenca")
    ap.add_argument("--name-prefix", required=True)
    ap.add_argument("--min-km2", type=float, default=50.0,
                    help="área contribuyente mínima para considerar cauce")
    ap.add_argument("--spacing-km", type=float, default=6.0,
                    help="separación mínima entre puntos (ventana ABM = 12 km)")
    ap.add_argument("--max-points", type=int, default=40)
    ap.add_argument("--out", default=None, help="JSON de salida (default: stdout)")
    a = ap.parse_args()

    with rasterio.open(a.facc) as src:
        facc = src.read(1).astype("float64")
        transform = src.transform
        crs = src.crs
        bounds = src.bounds

    # El tamaño de celda puede venir en metros (CRS proyectado, ej. las
    # cuencas de paper1 en UTM 19S) o en GRADOS (CRS geográfico, ej. la
    # hidrología de Choapa, calculada directamente sobre el DEM Copernicus
    # sin reproyectar). Tratar grados como metros da un área de celda ~0 y
    # hace que NINGÚN cauce supere el umbral — falla silenciosa disfrazada de
    # "esta cuenca no tiene cauces". Se resuelve la métrica local explícitamente.
    if crs and crs.is_geographic:
        lat_media = (bounds.bottom + bounds.top) / 2.0
        m_por_grado_lat = 111_320.0
        m_por_grado_lon = 111_320.0 * np.cos(np.radians(lat_media))
        dx_m = abs(transform.a) * m_por_grado_lon
        dy_m = abs(transform.e) * m_por_grado_lat
        escala_x, escala_y = m_por_grado_lon, m_por_grado_lat
        print(f"CRS geográfico: celda ≈ {dx_m:.1f} × {dy_m:.1f} m "
              f"a lat {lat_media:.2f}", file=sys.stderr)
    else:
        dx_m, dy_m = abs(transform.a), abs(transform.e)
        escala_x = escala_y = 1.0

    cell_km2 = (dx_m * dy_m) / 1.0e6
    thr_cells = a.min_km2 / cell_km2
    rows, cols = np.where(np.isfinite(facc) & (facc >= thr_cells))
    if rows.size == 0:
        finito = facc[np.isfinite(facc)]
        sys.exit(f"ningún cauce supera {a.min_km2} km² en {a.facc} "
                 f"(máx observado {finito.max() * cell_km2:.1f} km²)")

    # de mayor a menor área contribuyente: aguas abajo primero
    order = np.argsort(-facc[rows, cols])
    rows, cols = rows[order], cols[order]

    xs = transform.c + (cols + 0.5) * transform.a
    ys = transform.f + (rows + 0.5) * transform.e
    # coordenadas métricas SOLO para el test de distancia (en CRS proyectado
    # las escalas son 1 y esto es la identidad)
    xm, ym = xs * escala_x, ys * escala_y

    # submuestreo codicioso por distancia mínima
    sep_m = a.spacing_km * 1000.0
    sel_x, sel_y, sel_xm, sel_ym, sel_area = [], [], [], [], []
    for x, y, x_m, y_m, r, c in zip(xs, ys, xm, ym, rows, cols):
        if sel_xm:
            d = np.hypot(np.array(sel_xm) - x_m, np.array(sel_ym) - y_m)
            if d.min() < sep_m:
                continue
        sel_x.append(x)
        sel_y.append(y)
        sel_xm.append(x_m)
        sel_ym.append(y_m)
        sel_area.append(facc[r, c] * cell_km2)
        if len(sel_x) >= a.max_points:
            break

    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pts = []
    for i, (x, y, km2) in enumerate(zip(sel_x, sel_y, sel_area), 1):
        lon, lat = t.transform(x, y)
        pts.append({"name": f"{a.name_prefix}_{i:02d}",
                    "lon": round(lon, 5), "lat": round(lat, 5),
                    "area_contribuyente_km2": round(float(km2), 1)})

    out = {"facc": a.facc, "min_km2": a.min_km2, "spacing_km": a.spacing_km,
           "n_puntos": len(pts), "puntos": pts}
    txt = json.dumps(out, indent=1, ensure_ascii=False)
    if a.out:
        with open(a.out, "w") as f:
            f.write(txt)
        print(f"{len(pts)} puntos -> {a.out}", file=sys.stderr)
    else:
        print(txt)


if __name__ == "__main__":
    main()
