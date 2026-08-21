#!/usr/bin/env python3
"""Extract CR2MET daily precipitation at each event's IMERG storm core.

Generalises extract_atacama_cr2met.py to the three dated events, so the
single-factor experiment (examples/single_factor_resolution.rs) can separate
what is due to the *product* (CR2MET vs IMERG at daily resolution) from what is
due to the *resolution* (IMERG daily vs IMERG half-hourly), instead of
confounding the two as a CR2MET-daily vs IMERG-half-hourly comparison does.

The core is located exactly as extract_event_imerg.py locates it — the cell with
the highest IMERG event total inside the bbox — and every CR2MET cell inside that
IMERG pixel is sampled, so both products describe the same place (see the note on
HALF_PIXEL_DEG for why the nearest single cell is not used).

Produces ../data/event_<key>_cr2met.csv: date, p_mm_max, p_mm_mean (mm/day).

Requires numpy + netCDF4 + xarray, the cached IMERG granules under
../data/imerg_hhr/<key>/, and the CR2MET v2.5 daily archive.

Usage: python3 scripts/extract_event_cr2met.py [key ...]     # default: all
"""

import csv
import glob
import os
import sys

import numpy as np
import netCDF4 as nc
import xarray as xr

# The archive moved to the external drive; keep the old location as a fallback
# so the script works on whichever machine has it mounted.
CR2_CANDIDATES = [
    "/mnt/kingston/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr",
    os.path.expanduser("~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"),
]

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")

# Same registry as extract_event_imerg.py (bbox + the month CR2MET must cover).
EVENTS = {
    "atacama_2015": dict(bbox=(-71.5, -28.0, -69.0, -26.0), year=2015, month=3,
                         label="Atacama / Copiapo"),
    "maipo_2017": dict(bbox=(-70.7, -34.0, -69.6, -33.2), year=2017, month=2,
                       label="Cajon del Maipo"),
    "santalucia_2017": dict(bbox=(-72.7, -43.6, -71.7, -43.0), year=2017, month=12,
                            label="Villa Santa Lucia"),
}


def cr2_dir():
    for d in CR2_CANDIDATES:
        if os.path.isdir(d):
            return d
    raise SystemExit(
        "CR2MET archive not found. Looked in:\n  " + "\n  ".join(CR2_CANDIDATES)
    )


def imerg_core(key, ev):
    """Locate the storm core (lon, lat) exactly as extract_event_imerg.py does."""
    files = sorted(glob.glob(os.path.join(ROOT, "imerg_hhr", key, "*.HDF5")))
    if not files:
        raise SystemExit(f"[{key}] no cached IMERG granules — run extract_event_imerg.py first")
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
    return float(lons[ci[0]]), float(lats[ci[1]]), float(total[ci])


# IMERG pixels are ~0.1 deg and CR2MET cells ~0.05 deg, so an IMERG core covers
# about 2x2 CR2MET cells. Taking the *nearest* cell is numerically unstable — at
# every one of these cores the IMERG centre falls exactly halfway between two
# CR2MET centres, and the tie-break silently swings the wettest day by ~25 %
# (Atacama 24-Mar: 15.93 vs 12.73 mm in adjacent cells). We therefore take all
# CR2MET cells inside the IMERG footprint and report both their mean and their
# MAX. The max is the honest choice for the comparison: it gives the daily
# product its best shot at the storm, which biases the test *against* our own
# resolution argument.
HALF_PIXEL_DEG = 0.05


def extract(key, ev):
    lon0, lat0, imerg_total = imerg_core(key, ev)
    fp = os.path.join(cr2_dir(), f"CR2MET_pr_v2.5_day_{ev['year']}_{ev['month']:02d}_005deg.nc")
    if not os.path.exists(fp):
        raise SystemExit(f"[{key}] missing {fp}")
    ds = nc.Dataset(fp)
    lat, lon = ds.variables["lat"][:], ds.variables["lon"][:]
    ys = np.nonzero(np.abs(lat - lat0) <= HALF_PIXEL_DEG + 1e-9)[0]
    xs = np.nonzero(np.abs(lon - lon0) <= HALF_PIXEL_DEG + 1e-9)[0]
    if ys.size == 0 or xs.size == 0:
        raise SystemExit(f"[{key}] IMERG core falls outside the CR2MET domain")
    tv = ds.variables["time"]
    dates = nc.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)
    block = np.asarray(ds.variables["pr"][:, ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1], "float64")
    block = np.clip(np.nan_to_num(block, nan=0.0), 0.0, None)

    out = os.path.join(ROOT, f"event_{key}_cr2met.csv")
    tot_max = tot_mean = 0.0
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "p_mm_max", "p_mm_mean"])
        for k in range(len(dates)):
            vmax = float(block[k].max())
            vmean = float(block[k].mean())
            tot_max += vmax
            tot_mean += vmean
            w.writerow([dates[k].strftime("%Y-%m-%d"), f"{vmax:.2f}", f"{vmean:.2f}"])
    ds.close()
    print(f"[{key}] IMERG core lon {lon0:.2f} lat {lat0:.2f} (event total {imerg_total:.1f} mm) "
          f"-> {ys.size}x{xs.size} CR2MET cells in footprint "
          f"(lat {lat[ys[0]]:.3f}..{lat[ys[-1]]:.3f}, lon {lon[xs[0]]:.3f}..{lon[xs[-1]]:.3f}); "
          f"month total max {tot_max:.1f} mm / mean {tot_mean:.1f} mm -> {out}")


def main():
    keys = sys.argv[1:] or list(EVENTS)
    for key in keys:
        if key not in EVENTS:
            raise SystemExit(f"unknown event {key}; known: {', '.join(EVENTS)}")
        extract(key, EVENTS[key])


if __name__ == "__main__":
    main()
