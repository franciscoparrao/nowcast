#!/usr/bin/env python3
"""Extract a DISTRIBUTED backtest dataset for the Río Maipo event region.

Unlike extract_maipo_cr2met.py (one gauge at the event centroid), this builds a
gridded dataset over a CR2MET subgrid covering the rainfall-triggered events:

  ../data/maipo_dist_grid.csv    cell, row, col, lat, lon, susceptibility
  ../data/maipo_dist_pr.csv      date, c0, c1, ... (per-cell daily precip, mm)
  ../data/maipo_dist_events.csv  id, year, month, cell

Grid order is row-major with rows north→south, cols west→east, matching
nowcast_core::GridDims. Susceptibility is the real RandomForest raster
(EPSG:32719, 30 m) reprojected to the CR2MET grid by area-average; cells outside
the mapped basin become 0.

Sources (absolute, not copied into the repo):
  - CR2MET v2.5 daily precip  ~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr
  - SERNAGEOMIN inventory      ~/proyectos/postdoc/.../basin_inventory/09_rio_maipo.csv
  - Susceptibility raster      ~/proyectos/postdoc/.../factors/09_rio_maipo/susceptibility_RandomForest.tif

Requires: numpy, netCDF4, rasterio.
"""

import csv
import os

import numpy as np
import netCDF4 as nc
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.transform import from_origin

INV = os.path.expanduser(
    "~/proyectos/postdoc/papers/paper1_susceptibilidad/basin_inventory/09_rio_maipo.csv"
)
CR2 = os.path.expanduser("~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr")
SUSC = os.path.expanduser(
    "~/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif"
)
OUT = os.path.join(os.path.dirname(__file__), "..", "data")
START_YEAR, END_YEAR = 1979, 2016
PAD = 0.10  # degrees of padding around the event bounding box
RES = 0.05  # CR2MET grid resolution (deg)


def dated_rain_events():
    for r in csv.DictReader(open(INV, encoding="utf-8", errors="replace")):
        if "lluv" not in (r.get("trigger") or "").lower():
            continue
        p = r["id"].split("-")
        if len(p) >= 3 and p[1].isdigit() and p[2].isdigit():
            y, m = int(p[1]), int(p[2])
            if 1 <= m <= 12 and 1900 < y < 2100:
                yield r["id"], y, m, float(r["lat"]), float(r["lon"])


def main():
    os.makedirs(OUT, exist_ok=True)
    events = list(dated_rain_events())
    lats = [e[3] for e in events]
    lons = [e[4] for e in events]
    lat_min, lat_max = min(lats) - PAD, max(lats) + PAD
    lon_min, lon_max = min(lons) - PAD, max(lons) + PAD

    # CR2MET grid → subgrid index ranges.
    d0 = nc.Dataset(f"{CR2}/CR2MET_pr_v2.5_day_{START_YEAR}_01_005deg.nc")
    glat = d0.variables["lat"][:]
    glon = d0.variables["lon"][:]
    d0.close()
    iy = np.where((glat >= lat_min) & (glat <= lat_max))[0]  # ascending lat
    ix = np.where((glon >= lon_min) & (glon <= lon_max))[0]
    iy0, iy1 = int(iy.min()), int(iy.max())
    ix0, ix1 = int(ix.min()), int(ix.max())
    sub_lat = glat[iy0 : iy1 + 1]  # ascending
    sub_lon = glon[ix0 : ix1 + 1]
    nrows, ncols = len(sub_lat), len(sub_lon)
    lat_desc = sub_lat[::-1]  # rows north→south
    print(f"subgrid {nrows}x{ncols} = {nrows * ncols} cells; "
          f"lat [{sub_lat.min():.3f},{sub_lat.max():.3f}] lon [{sub_lon.min():.3f},{sub_lon.max():.3f}]")

    # --- susceptibility reprojected to the CR2MET subgrid (area average) ------
    north = float(lat_desc[0]) + RES / 2
    west = float(sub_lon[0]) - RES / 2
    dst_transform = from_origin(west, north, RES, RES)
    dst = np.full((nrows, ncols), np.nan, dtype="float32")
    with rasterio.open(SUSC) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.average,
        )
    susc = np.nan_to_num(dst, nan=0.0)
    susc = np.clip(susc, 0.0, 1.0)
    print(f"susceptibility per cell: min {susc.min():.3f} max {susc.max():.3f} "
          f"mean {susc.mean():.3f}; cells>0: {(susc > 0).sum()}")

    # --- grid metadata -------------------------------------------------------
    with open(os.path.join(OUT, "maipo_dist_grid.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "row", "col", "lat", "lon", "susceptibility"])
        for r in range(nrows):
            for c in range(ncols):
                w.writerow([r * ncols + c, r, c,
                            f"{lat_desc[r]:.4f}", f"{sub_lon[c]:.4f}",
                            f"{susc[r, c]:.4f}"])

    # --- events → cell -------------------------------------------------------
    with open(os.path.join(OUT, "maipo_dist_events.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "year", "month", "cell"])
        kept = 0
        for eid, y, m, la, lo in sorted(events, key=lambda e: (e[1], e[2])):
            if not (lat_min <= la <= lat_max and lon_min <= lo <= lon_max):
                continue
            row = int(np.abs(lat_desc - la).argmin())
            col = int(np.abs(sub_lon - lo).argmin())
            w.writerow([eid, y, m, row * ncols + col])
            kept += 1
    print(f"events mapped to cells: {kept}/{len(events)}")

    # --- distributed daily precip (row-major, rows north→south) --------------
    out = os.path.join(OUT, "maipo_dist_pr.csv")
    n = 0
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date"] + [f"c{i}" for i in range(nrows * ncols)])
        for y in range(START_YEAR, END_YEAR + 1):
            for mo in range(1, 13):
                fp = f"{CR2}/CR2MET_pr_v2.5_day_{y}_{mo:02d}_005deg.nc"
                if not os.path.exists(fp):
                    continue
                ds = nc.Dataset(fp)
                tv = ds.variables["time"]
                dates = nc.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)
                block = ds.variables["pr"][:, iy0 : iy1 + 1, ix0 : ix1 + 1]  # (t, lat_asc, lon)
                block = np.flip(block, axis=1)  # lat → descending (north→south)
                block = np.nan_to_num(np.asarray(block), nan=0.0)
                block = np.clip(block, 0.0, None)
                for k in range(block.shape[0]):
                    flat = block[k].reshape(-1)
                    w.writerow([dates[k].strftime("%Y-%m-%d")] + [f"{v:.2f}" for v in flat])
                    n += 1
                ds.close()
    print(f"wrote {n} days × {nrows * ncols} cells to {out}")


if __name__ == "__main__":
    main()
