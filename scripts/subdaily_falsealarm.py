#!/usr/bin/env python3
"""Spatial false-alarm context for the sub-daily I-D lead-time demonstration
(review C6). Uses the IMERG half-hourly granules already cached by
extract_event_imerg.py (../data/imerg_hhr/<key>/) — no new download.

For each event it runs the SAME rolling intensity-duration trigger the engine
uses, per cell, over the whole IMERG scene (the event bbox), and asks: does the
trigger cross the I-D curve only on the storm footprint, or everywhere? It reports
the fraction of scene cells that ever cross, the storm-core peak exceedance, and
the core's percentile rank. This bounds the *spatial* specificity of the sub-daily
trigger; a temporal off-event false-alarm rate (crossings at these cores on days
without a documented flow) needs a multi-year sub-daily record not assembled here.

Usage: python3 scripts/subdaily_falsealarm.py            # all events
"""
import glob
import os
from datetime import datetime

import numpy as np
import xarray as xr

A, B = 4.0, 0.39          # regional I-D intercept / exponent (as in the paper)
DT_H = 0.5                # half-hourly
MAX_WIN = 48              # rolling windows up to 24 h
CROSS = 1.0              # exceedance >= 1 means the I-D curve is crossed

EVENTS = {
    "atacama_2015": dict(bbox=(-71.5, -28.0, -69.0, -26.0), label="Atacama / Copiapo"),
    "maipo_2017": dict(bbox=(-70.7, -34.0, -69.6, -33.2), label="Cajon del Maipo"),
    "santalucia_2017": dict(bbox=(-72.7, -43.6, -71.7, -43.0), label="Villa Santa Lucia"),
}
ROOT = os.path.join(os.path.dirname(__file__), "..", "data")


def file_time(path):
    tok = os.path.basename(path).split(".3IMERG.")[1]
    return datetime.strptime(tok.split("-")[0] + tok.split("-S")[1][:6], "%Y%m%d%H%M%S")


def load_depths(key, ev):
    """Return (n_steps, n_lat, n_lon) array of per-step rainfall DEPTH (mm)."""
    files = sorted(glob.glob(os.path.join(ROOT, "imerg_hhr", key, "*.HDF5")), key=file_time)
    if not files:
        raise SystemExit(f"[{key}] no cached HDF5 granules")
    w, s, e, n = ev["bbox"]
    frames = []
    for fp in files:
        ds = xr.open_dataset(fp, group="Grid")
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        rate = ds[var].sel(lon=slice(w, e), lat=slice(s, n)).squeeze("time").values  # mm/h
        a = np.clip(np.nan_to_num(np.asarray(rate, "float64"), nan=0.0), 0.0, None)
        frames.append(a * DT_H)   # mm per half-hour step
        ds.close()
    return np.stack(frames, axis=0)  # (T, nlon, nlat) following the lon/lat slice order


def peak_exceedance(depths):
    """Max rolling-window I-D exceedance per cell over the whole series.

    depths: (T, ...) per-step depth (mm). Vectorised over the spatial cells.
    """
    T = depths.shape[0]
    csum = np.cumsum(depths, axis=0)
    csum = np.concatenate([np.zeros((1,) + depths.shape[1:]), csum], axis=0)  # prefix[0..T]
    best = np.zeros(depths.shape[1:], dtype="float64")
    for m in range(1, min(MAX_WIN, T) + 1):
        dur = m * DT_H
        icrit = A * dur ** (-B)
        # window sums ending at each t (t = m..T): prefix[t] - prefix[t-m]
        wsum = csum[m:] - csum[:-m]                 # (T-m+1, ...)
        inten = wsum / dur                          # mean intensity mm/h
        e = inten / icrit
        best = np.maximum(best, e.max(axis=0))
    return best


def main():
    print(f"Sub-daily I-D spatial false-alarm context (a={A}, b={B}, windows<=24h)\n"
          f"{'event':<20} {'cells':>6} {'cross%':>7} {'core E':>8} {'core pctile':>12}")
    print("-" * 60)
    for key, ev in EVENTS.items():
        depths = load_depths(key, ev)
        peakE = peak_exceedance(depths)             # (nlon, nlat)
        total = depths.sum(axis=0)
        ci = np.unravel_index(np.argmax(total), total.shape)   # storm core = max event total
        flat = peakE.ravel()
        cross_frac = float((flat >= CROSS).mean())
        core_e = float(peakE[ci])
        pct = float((flat <= core_e).mean()) * 100.0
        print(f"{ev['label']:<20} {flat.size:>6} {100*cross_frac:>6.1f}% {core_e:>8.1f} {pct:>11.1f}%")


if __name__ == "__main__":
    main()
