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


def _grid(da, w, s, e, n):
    """Subset a (lon, lat) DataArray to the bbox; return values + coords."""
    da = da.sel(lon=slice(w, e), lat=slice(s, n)).squeeze("time")
    return da


def extract():
    files = sorted(glob.glob(os.path.join(RAW, "*.HDF5")), key=file_time)
    if not files:
        raise SystemExit("no HDF5 granules found — run download() first")
    w, s, e, n = BBOX

    # Pass 1: per-cell storm total over the bbox → locate the storm core, so the
    # series is a fixed point (no wandering maximum that over-counts the total).
    total = None
    lons = lats = None
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        da = _grid(ds[var], w, s, e, n)
        a = np.nan_to_num(np.asarray(da.values, dtype="float64"), nan=0.0)
        a = np.clip(a, 0.0, None) * 0.5  # mm in this half-hour
        total = a if total is None else total + a
        if lons is None:
            lons, lats = da["lon"].values, da["lat"].values
        ds.close()
    ci = np.unravel_index(np.argmax(total), total.shape)  # storm-core (lon_i, lat_i)
    core_lon, core_lat = float(lons[ci[0]]), float(lats[ci[1]])
    print(f"storm core at lon {core_lon:.2f}, lat {core_lat:.2f}; "
          f"event total there {total[ci]:.1f} mm")

    # Pass 2: fixed storm-core cell rate + a tight 3×3-cell box mean around it.
    rows = []
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        a = np.nan_to_num(np.asarray(_grid(ds[var], w, s, e, n).values, "float64"), nan=0.0)
        a = np.clip(a, 0.0, None)
        core = float(a[ci])
        i0, i1 = max(0, ci[0] - 1), ci[0] + 2
        j0, j1 = max(0, ci[1] - 1), ci[1] + 2
        boxmean = float(a[i0:i1, j0:j1].mean())
        rows.append((file_time(fp).strftime("%Y-%m-%dT%H:%M:%S"), core, boxmean))
        ds.close()

    with open(OUT, "w", newline="") as f:
        wri = csv.writer(f)
        wri.writerow(["datetime", "core_mm_hr", "boxmean_mm_hr"])
        wri.writerows(rows)
    peak = max(rows, key=lambda r: r[1])
    tot = sum(r[1] for r in rows) * 0.5
    print(f"wrote {len(rows)} half-hours to {OUT}")
    print(f"storm-core total {tot:.1f} mm; peak rate {peak[1]:.1f} mm/hr at {peak[0]}")


if __name__ == "__main__":
    download()
    extract()
