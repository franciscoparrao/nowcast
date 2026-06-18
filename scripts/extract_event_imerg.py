#!/usr/bin/env python3
"""Download GPM IMERG half-hourly precipitation for a dated Chilean
debris-flow / aluvión event and extract its storm-core intensity series.

Usage:
    python3 scripts/extract_event_imerg.py <event_key> [<event_key> ...]
    python3 scripts/extract_event_imerg.py all

For each event it writes ../data/event_<key>.csv (datetime, core_mm_hr,
boxmean_mm_hr). Raw HDF5 (~1 GB/event) goes to ../data/imerg_hhr/<key>/
(git-ignored). Needs earthaccess (Earthdata creds in ~/.netrc; the "NASA GESDISC
DATA ARCHIVE" app must be authorized), xarray, numpy.

The storm core is the cell with the highest event-total rainfall inside the
bbox — a fixed point, so the series is coherent (no wandering maximum).
"""

import csv
import glob
import os
import sys
from datetime import datetime

import numpy as np
import xarray as xr
import earthaccess

# key: bbox (W,S,E,N), date window (inclusive), documented onset day, label
EVENTS = {
    "atacama_2015": dict(bbox=(-71.5, -28.0, -69.0, -26.0),
                         start="2015-03-24", end="2015-03-26",
                         onset="2015-03-25", label="Atacama / Copiapó"),
    "maipo_2017": dict(bbox=(-70.7, -34.0, -69.6, -33.2),
                       start="2017-02-24", end="2017-02-26",
                       onset="2017-02-25", label="Cajón del Maipo"),
    "santalucia_2017": dict(bbox=(-72.7, -43.6, -71.7, -43.0),
                            start="2017-12-15", end="2017-12-17",
                            onset="2017-12-16", label="Villa Santa Lucía"),
}

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")


def raw_dir(key):
    d = os.path.join(ROOT, "imerg_hhr", key)
    os.makedirs(d, exist_ok=True)
    return d


def download(key, ev):
    rd = raw_dir(key)
    if glob.glob(os.path.join(rd, "*.HDF5")):
        print(f"[{key}] HDF5 already present, skipping download")
        return
    earthaccess.login(strategy="netrc")
    g = earthaccess.search_data(short_name="GPM_3IMERGHH", version="07",
                                temporal=(ev["start"], ev["end"]), bounding_box=ev["bbox"])
    print(f"[{key}] {len(g)} granules → {rd}")
    earthaccess.download(g, local_path=rd)


def file_time(path):
    tok = os.path.basename(path).split(".3IMERG.")[1]
    return datetime.strptime(tok.split("-")[0] + tok.split("-S")[1][:6], "%Y%m%d%H%M%S")


def extract(key, ev):
    files = sorted(glob.glob(os.path.join(raw_dir(key), "*.HDF5")), key=file_time)
    if not files:
        raise SystemExit(f"[{key}] no HDF5 — download first")
    w, s, e, n = ev["bbox"]

    total, lons, lats = None, None, None
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        da = ds[var].sel(lon=slice(w, e), lat=slice(s, n)).squeeze("time")
        a = np.clip(np.nan_to_num(np.asarray(da.values, "float64"), nan=0.0), 0.0, None) * 0.5
        total = a if total is None else total + a
        if lons is None:
            lons, lats = da["lon"].values, da["lat"].values
        ds.close()
    ci = np.unravel_index(np.argmax(total), total.shape)
    print(f"[{key}] core lon {float(lons[ci[0]]):.2f} lat {float(lats[ci[1]]):.2f}; total {total[ci]:.1f} mm")

    rows = []
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        a = np.clip(np.nan_to_num(np.asarray(
            ds[var].sel(lon=slice(w, e), lat=slice(s, n)).squeeze("time").values, "float64"), nan=0.0), 0.0, None)
        i0, i1 = max(0, ci[0] - 1), ci[0] + 2
        j0, j1 = max(0, ci[1] - 1), ci[1] + 2
        rows.append((file_time(fp).strftime("%Y-%m-%dT%H:%M:%S"), float(a[ci]), float(a[i0:i1, j0:j1].mean())))
        ds.close()

    out = os.path.join(ROOT, f"event_{key}.csv")
    with open(out, "w", newline="") as f:
        wri = csv.writer(f)
        wri.writerow(["datetime", "core_mm_hr", "boxmean_mm_hr"])
        wri.writerows(rows)
    peak = max(rows, key=lambda r: r[1])
    print(f"[{key}] wrote {len(rows)} half-hours; peak {peak[1]:.1f} mm/h at {peak[0]} → {out}")


def main():
    keys = sys.argv[1:] or ["all"]
    if keys == ["all"]:
        keys = list(EVENTS)
    for key in keys:
        ev = EVENTS[key]
        download(key, ev)
        extract(key, ev)


if __name__ == "__main__":
    main()
