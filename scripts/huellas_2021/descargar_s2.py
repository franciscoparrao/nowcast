#!/usr/bin/env python3
"""Recorte Sentinel-2 L2A (Planetary Computer) del AOI de los flujos del temporal
del 29–31 de enero de 2021 en la RM, para mapear sus huellas.

El AOI cubre los tres grupos de puntos del catálogo nacional de SERNAGEOMIN
(Cajón del Maipo rutas G-25/G-455, Las Vertientes, El Arrayán). Las teselas
19HCC y 19HCD están en el mismo UTM 19S, así que se leen sobre UNA grilla común
de 10 m (las bandas de 20 m se remuestrean por vecino más cercano) y se
mosaican: en el solape gana la tesela con dato válido según su SCL.

Salida por fecha: data/huellas_2021/s2/<fecha>.tif, uint16, 7 bandas
(B02 B03 B04 B08 B11 B12 SCL), nodata 0. Reflectancia = DN/10000 (las escenas
de 2021 son de baseline < 04.00, sin offset de −1000).

Uso:  python3 descargar_s2.py 2021-01-18 2021-01-23 2021-02-02 2021-02-17
"""
import os
import sys

import numpy as np
import planetary_computer as pc
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

AOI = (-70.50, -33.88, -69.96, -33.31)  # lon/lat
ORBITA = "R096"
BANDAS = ["B02", "B03", "B04", "B08", "B11", "B12", "SCL"]
CRS = "EPSG:32719"
RES = 10.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "huellas_2021", "s2")


def grilla():
    x0, y0, x1, y1 = transform_bounds("EPSG:4326", CRS, *AOI, densify_pts=21)
    x0, y1 = np.floor(x0 / RES) * RES, np.ceil(y1 / RES) * RES
    w, h = int(np.ceil((x1 - x0) / RES)), int(np.ceil((y1 - y0) / RES))
    return from_origin(x0, y1, RES, RES), w, h


def main(fechas):
    os.makedirs(OUT, exist_ok=True)
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1",
                                    modifier=pc.sign_inplace)
    T, W, H = grilla()
    for f in fechas:
        items = [i for i in cat.search(collections=["sentinel-2-l2a"], bbox=list(AOI), datetime=f).items()
                 if ORBITA in i.id]
        assert items, f"sin escenas {ORBITA} el {f}"
        stack = np.zeros((len(BANDAS), H, W), np.uint16)
        for it in sorted(items, key=lambda i: i.properties["eo:cloud_cover"]):  # la más limpia primero
            bl = it.properties.get("s2:processing_baseline", "?")
            assert bl < "04.00", f"{it.id}: baseline {bl} trae offset −1000; ajustar"
            cur = np.zeros_like(stack)
            for b, nombre in enumerate(BANDAS):
                with rasterio.open(it.assets[nombre].href) as src, WarpedVRT(
                        src, crs=CRS, transform=T, width=W, height=H,
                        resampling=Resampling.nearest, nodata=0) as vrt:
                    cur[b] = vrt.read(1)
            # Llena donde aún no hay píxel VÁLIDO (nube/sombra/nieve de la primera
            # tesela no debe tapar un píxel despejado de la otra en el solape).
            valido = lambda scl: np.isin(scl, (4, 5, 6, 7, 11))
            libre = (~valido(stack[6]) & valido(cur[6])) | ((stack[6] == 0) & (cur[6] > 0))
            stack[:, libre] = cur[:, libre]
            print(f"  {it.id}: nubes {it.properties['eo:cloud_cover']:.1f} %, llena {libre.mean():.1%} del AOI")
        out = os.path.join(OUT, f"{f}.tif")
        with rasterio.open(out + ".part", "w", driver="GTiff", width=W, height=H, count=len(BANDAS),
                           dtype="uint16", crs=CRS, transform=T, nodata=0, compress="deflate",
                           tiled=True, predictor=2) as dst:
            dst.write(stack)
            dst.descriptions = tuple(BANDAS)
        os.replace(out + ".part", out)
        print(f"{f}: {out} ({W}×{H}, cobertura {(stack[6] > 0).mean():.1%})")


if __name__ == "__main__":
    main(sys.argv[1:])
