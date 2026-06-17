#!/usr/bin/env python3
"""Extract CR2MET daily precipitation at the Atacama 2015 storm-core point, to
compare against the GPM IMERG half-hourly series at the same location.

Produces ../data/atacama_cr2met_daily.csv: date, p_mm (mm/day) for 2015-03.

The storm core (lon −70.45, lat −27.15) is where IMERG's event total peaks; we
sample the nearest CR2MET cell so both products describe the same place.

Requires numpy + netCDF4.
"""

import csv
import os

import numpy as np
import netCDF4 as nc

CR2 = os.path.expanduser("~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "atacama_cr2met_daily.csv")
CORE_LON, CORE_LAT = -70.45, -27.15


def main():
    fp = f"{CR2}/CR2MET_pr_v2.5_day_2015_03_005deg.nc"
    ds = nc.Dataset(fp)
    lat, lon = ds.variables["lat"][:], ds.variables["lon"][:]
    iy = int(np.abs(lat - CORE_LAT).argmin())
    ix = int(np.abs(lon - CORE_LON).argmin())
    tv = ds.variables["time"]
    dates = nc.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)
    pr = ds.variables["pr"][:, iy, ix]
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "p_mm"])
        for k in range(len(dates)):
            v = float(pr[k])
            v = 0.0 if np.isnan(v) else max(v, 0.0)
            w.writerow([dates[k].strftime("%Y-%m-%d"), f"{v:.2f}"])
    ds.close()
    print(f"CR2MET cell lat {lat[iy]:.3f} lon {lon[ix]:.3f} → {OUT}")


if __name__ == "__main__":
    main()
