#!/usr/bin/env python3
"""Download GPM IMERG half-hourly precipitation for the March 2015 Atacama
debris-flow event and extract a sub-daily rainfall-intensity series.

Produces ../data/atacama_imerg_hhr.csv: datetime, mean_mm_hr, max_mm_hr
(basin-mean and peak-cell rainfall rate over the affected region).

The raw HDF5 granules (~1 GB) go to ../data/imerg_hhr/ (git-ignored); only the
small derived CSV is kept. Requires earthaccess (Earthdata creds in ~/.netrc),
xarray, numpy.
"""

import os
import glob
import csv
from datetime import datetime

import numpy as np
import xarray as xr
import earthaccess

# Affected region: Copiapó / El Salado / Chañaral debris-flow zone.
BBOX = (-71.5, -28.0, -69.0, -26.0)  # (W, S, E, N)
START, END = "2015-03-24", "2015-03-26"
RAW = os.path.join(os.path.dirname(__file__), "..", "data", "imerg_hhr")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "atacama_imerg_hhr.csv")


def download():
    os.makedirs(RAW, exist_ok=True)
    earthaccess.login(strategy="netrc")
    granules = earthaccess.search_data(
        short_name="GPM_3IMERGHH", version="07",
        temporal=(START, END), bounding_box=BBOX,
    )
    print(f"{len(granules)} granules; downloading to {RAW} …")
    earthaccess.download(granules, local_path=RAW)


def file_time(path):
    # 3B-HHR.MS.MRG.3IMERG.20150324-S000000-E002959.0000.V07B.HDF5
    base = os.path.basename(path)
    tok = base.split(".3IMERG.")[1]
    day, s = tok.split("-")[0], tok.split("-S")[1][:6]
    return datetime.strptime(day + s, "%Y%m%d%H%M%S")


def extract():
    files = sorted(glob.glob(os.path.join(RAW, "*.HDF5")), key=file_time)
    if not files:
        raise SystemExit("no HDF5 granules found — run download() first")
    w, s, e, n = BBOX
    rows = []
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        da = ds[var]  # dims (time, lon, lat), mm/hr
        da = da.sel(lon=slice(w, e), lat=slice(s, n)).squeeze("time")
        vals = np.asarray(da.values, dtype="float64")
        vals = vals[np.isfinite(vals)]
        vals = vals[vals >= 0]
        if vals.size == 0:
            mean_r = max_r = 0.0
        else:
            mean_r, max_r = float(vals.mean()), float(vals.max())
        rows.append((file_time(fp).strftime("%Y-%m-%dT%H:%M:%S"), mean_r, max_r))
        ds.close()
    with open(OUT, "w", newline="") as f:
        wri = csv.writer(f)
        wri.writerow(["datetime", "mean_mm_hr", "max_mm_hr"])
        wri.writerows(rows)
    peak = max(rows, key=lambda r: r[2])
    print(f"wrote {len(rows)} half-hours to {OUT}")
    print(f"peak cell rate {peak[2]:.1f} mm/hr at {peak[0]}; "
          f"max basin-mean {max(r[1] for r in rows):.1f} mm/hr")


if __name__ == "__main__":
    download()
    extract()
