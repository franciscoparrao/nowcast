#!/usr/bin/env python3
"""Extrae lluvia diaria CR2MET real de marzo 2015 en Vallenar/Freirina/Huasco,
para comparar contra la forzante del ciclo vigente en el reporte de cuenca.

Mismo patrón que scripts/extract_atacama_cr2met.py (punto de tormenta de
Copiapó), aplicado a los 3 puntos de la cuenca del Huasco.
"""

import csv
import os

import netCDF4 as nc
import numpy as np

CR2 = "/mnt/kingston/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "huasco_cr2met_2015_03.csv")

TOWNS = {
    "Vallenar": (-70.76517, -28.5774),
    "Freirina": (-71.07987, -28.50991),
    "Huasco": (-71.219017, -28.467542),
}


def main():
    fp = f"{CR2}/CR2MET_pr_v2.5_day_2015_03_005deg.nc"
    ds = nc.Dataset(fp)
    lat, lon = ds.variables["lat"][:], ds.variables["lon"][:]
    tv = ds.variables["time"]
    dates = nc.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)

    idx = {}
    for name, (lo, la) in TOWNS.items():
        iy = int(np.abs(lat - la).argmin())
        ix = int(np.abs(lon - lo).argmin())
        idx[name] = (iy, ix)
        print(f"{name}: celda CR2MET más cercana lat={lat[iy]:.3f} lon={lon[ix]:.3f}")

    series = {name: ds.variables["pr"][:, iy, ix] for name, (iy, ix) in idx.items()}
    ds.close()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date"] + list(TOWNS))
        for k in range(len(dates)):
            row = [dates[k].strftime("%Y-%m-%d")]
            for name in TOWNS:
                v = float(series[name][k])
                v = 0.0 if np.isnan(v) else max(v, 0.0)
                row.append(f"{v:.2f}")
            w.writerow(row)
    print(f"escrito -> {OUT}")

    print("\n=== resumen marzo 2015 ===")
    for name in TOWNS:
        vals = np.nan_to_num(series[name], nan=0.0)
        vals = np.clip(vals, 0, None)
        total = float(vals.sum())
        peak_day = int(np.argmax(vals))
        print(f"{name}: total mes {total:.1f} mm | pico diario {vals[peak_day]:.1f} mm "
              f"el {dates[peak_day].strftime('%Y-%m-%d')} | "
              f"25-27 mar: {vals[24]:.1f}/{vals[25]:.1f}/{vals[26]:.1f} mm")


if __name__ == "__main__":
    main()
