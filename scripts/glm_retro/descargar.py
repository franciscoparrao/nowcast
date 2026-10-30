#!/usr/bin/env python3
"""Rayos GLM (GOES-19, GLM-L2-LCFA) retrospectivos, binados a 0.1° × 10 min.

Para medir la tasa de falsas alarmas del aviso por rayos: cuántas celdas
eléctricas del centro-norte-sur de Chile terminaron en un evento y cuántas no.
Un archivo por día en OUT/glm_YYYYMMDD.csv.gz con (t10 UTC, lon, lat, n): t10 es
el inicio del bloque de 10 min, lon/lat el centro de la celda 0.1°. Los .nc se
leen en memoria y no tocan el disco. Reanudable: un día ya escrito se salta.

Uso: python3 descargar.py 2026-10-01 2026-10-31 [--out DIR] [--workers 16]
"""
import argparse
import csv
import gzip
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "monitor"))
from fetch_glm import PRODUCT, fetch_one, file_start, read_flashes  # noqa: E402
from fetch_goes_qpe import list_bucket  # noqa: E402

BUCKET = "noaa-goes19"
# Atacama (−25.9) a Araucanía (−39.55), costa a cordillera: cubre los cinco dominios.
LON0, LON1, LAT0, LAT1 = -74.0, -68.0, -40.0, -25.0
RES = 0.1


def procesar(key, raw):
    lat, lon = read_flashes(raw)
    m = (lon >= LON0) & (lon < LON1) & (lat >= LAT0) & (lat < LAT1)
    t = file_start(key)
    t10 = t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)
    ix = np.floor((lon[m] - LON0) / RES).astype(int)
    iy = np.floor((lat[m] - LAT0) / RES).astype(int)
    return t10, Counter(zip(ix.tolist(), iy.tolist()))


def dia(d, out, workers):
    path = os.path.join(out, f"glm_{d:%Y%m%d}.csv.gz")
    if os.path.exists(path):
        return f"{d} ya estaba"
    keys = []
    for h in range(24):
        keys += [k for k in list_bucket(BUCKET, f"{PRODUCT}/{d:%Y}/{d.timetuple().tm_yday:03d}/{h:02d}/")
                 if k.endswith(".nc")]
    acc, fallos = Counter(), 0
    with ThreadPoolExecutor(workers) as ex:
        for key, raw, err in ex.map(lambda k: fetch_one(BUCKET, k), keys):
            if raw is None:
                fallos += 1
                continue
            try:
                t10, c = procesar(key, raw)
            except Exception:  # noqa: BLE001  archivo ilegible: se cuenta, no tumba el día
                fallos += 1
                continue
            for (ix, iy), n in c.items():
                acc[(t10, ix, iy)] += n
    tmp = path + ".part"
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t10", "lon", "lat", "n"])
        for (t10, ix, iy), n in sorted(acc.items()):
            w.writerow([f"{t10:%Y-%m-%dT%H:%MZ}", f"{LON0 + (ix + .5) * RES:.2f}",
                        f"{LAT0 + (iy + .5) * RES:.2f}", n])
    os.replace(tmp, path)
    # Faltantes declarados aparte: un día con huecos no se confunde con calma.
    with open(os.path.join(out, "cobertura.csv"), "a") as f:
        f.write(f"{d},{len(keys)},{fallos},{sum(acc.values())}\n")
    return f"{d} archivos {len(keys)} fallos {fallos} descargas {sum(acc.values())}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("desde")
    ap.add_argument("hasta")
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-forecast/glm_retro"))
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    d, fin = date.fromisoformat(a.desde), date.fromisoformat(a.hasta)
    while d <= fin:
        t0 = datetime.now(timezone.utc)
        print(dia(d, a.out, a.workers), f"({(datetime.now(timezone.utc) - t0).seconds} s)", flush=True)
        d += timedelta(days=1)
    os._exit(0)  # mismo contrato que las hermanas HDF5: sin teardown de netCDF4


if __name__ == "__main__":
    main()
