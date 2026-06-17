#!/usr/bin/env python3
"""Extract the Río Maipo backtest inputs from local source datasets.

Produces, under ../data/:
  - maipo_cr2met_pr_1979_2016.csv  daily precip (CR2MET v2.5, mm) at the
    centroid of the dated rainfall-triggered events.
  - maipo_events_dated.csv         rainfall-triggered SERNAGEOMIN events, dated
    (year, month) from the record id (the inventory `year` column is unreliable;
    the id encodes RM-YYYY-MM-NNN).

Source paths are absolute and NOT copied into this repo (shared research data,
CC-BY for CR2MET). Re-run this script to regenerate the small derived CSVs.

Requires: numpy, netCDF4.
"""

import csv
import glob
import os

import numpy as np
import netCDF4 as nc

INV = os.path.expanduser(
    "~/proyectos/postdoc/papers/paper1_susceptibilidad/basin_inventory/09_rio_maipo.csv"
)
CR2 = os.path.expanduser(
    "~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"
)
OUT = os.path.join(os.path.dirname(__file__), "..", "data")
START_YEAR, END_YEAR = 1979, 2016


def dated_rain_events():
    """Yield ((year, month), lat, lon, id) for rainfall-triggered events with a
    parseable month in the record id."""
    for r in csv.DictReader(open(INV, encoding="utf-8", errors="replace")):
        if "lluv" not in (r.get("trigger") or "").lower():
            continue
        parts = r["id"].split("-")
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            y, m = int(parts[1]), int(parts[2])
            if 1 <= m <= 12 and 1900 < y < 2100:
                yield (y, m), float(r["lat"]), float(r["lon"]), r["id"]


def main():
    os.makedirs(OUT, exist_ok=True)
    events = list(dated_rain_events())
    lat0 = float(np.mean([la for _, la, _, _ in events]))
    lon0 = float(np.mean([lo for _, _, lo, _ in events]))
    print(f"{len(events)} dated rain events; centroid lat={lat0:.4f} lon={lon0:.4f}")

    with open(os.path.join(OUT, "maipo_events_dated.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "year", "month", "lat", "lon"])
        for (y, m), la, lo, i in sorted(events):
            w.writerow([i, y, m, f"{la:.6f}", f"{lo:.6f}"])

    # Nearest CR2MET grid cell (grid is identical across monthly files).
    f0 = sorted(glob.glob(f"{CR2}/CR2MET_pr_v2.5_day_{START_YEAR}_01_005deg.nc"))[0]
    d0 = nc.Dataset(f0)
    lats, lons = d0.variables["lat"][:], d0.variables["lon"][:]
    lon_q = lon0 if lons.min() < 0 else lon0 % 360
    iy = int(np.abs(lats - lat0).argmin())
    ix = int(np.abs(lons - lon_q).argmin())
    print(f"CR2MET cell: lat={lats[iy]:.3f} lon={lons[ix]:.3f} (iy={iy}, ix={ix})")
    d0.close()

    n = 0
    out = os.path.join(OUT, "maipo_cr2met_pr_1979_2016.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "p_mm"])
        for y in range(START_YEAR, END_YEAR + 1):
            for m in range(1, 13):
                fp = f"{CR2}/CR2MET_pr_v2.5_day_{y}_{m:02d}_005deg.nc"
                if not os.path.exists(fp):
                    continue
                ds = nc.Dataset(fp)
                tv = ds.variables["time"]
                # Each monthly file references its own epoch in `units`.
                dates = nc.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)
                pr = ds.variables["pr"][:, iy, ix]
                for k in range(len(dates)):
                    v = float(pr[k])
                    v = 0.0 if np.isnan(v) else max(v, 0.0)
                    w.writerow([dates[k].strftime("%Y-%m-%d"), f"{v:.2f}"])
                    n += 1
                ds.close()
    print(f"wrote {n} days to {out}")


if __name__ == "__main__":
    main()
