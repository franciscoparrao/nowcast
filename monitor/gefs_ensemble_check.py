#!/usr/bin/env python3
"""Tercera fuente de consenso: ensemble GEFS (GFS Ensemble Forecast System,
NOAA), para completar el chequeo de outlier iniciado con el ensemble de
ECMWF (`ecmwf_ensemble_check.py`) — misma pregunta del usuario ("¿es un
capricho de una sola corrida?"), ahora también contra la familia GFS.

Fuente: NOMADS, filtro `filter_gefs_atmos_0p25s.pl` (mismo sistema g2subset
que ya usa forecast_gfs.py para el GFS determinístico). 31 miembros: control
`gec00` + 30 perturbados `gep01`..`gep30`.

VERIFICADO EMPÍRICAMENTE (no asumido — dos veces nos equivocamos antes
asumiendo convención de acumulación sin chequear: GFS 6h-bucket vs ECMWF
acumulado-desde-t0): el APCP de GEFS resetea cada 6 h en bloques LIMPIOS
(stepRange "0-6", "6-12", "12-18", ...) — cada archivo en un múltiplo de 6 h
YA es el total de esas 6 h, sin necesitar diferenciar contra el paso
anterior (a diferencia del GFS determinístico hourly, que sí lo requiere).

LIMITACIÓN EXPLÍCITA: el producto de superficie `pgrb2s` de GEFS NO trae la
isoterma 0°C (`lev_0C_isotherm` → error del propio NOMADS, verificado). No
hay partición líquida/sólida por miembro como en GFS/ECMWF — este chequeo
usa **precipitación TOTAL** (agua+nieve equivalente), no comparable cifra a
cifra con los "mm líquidos" de los otros dos chequeos, pero sí válido como
chequeo de orden de magnitud y de consenso entre miembros.

Uso:
    python3 gefs_ensemble_check.py [--workers 8]
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25s.pl"
REGION = {"leftlon": -75.0, "rightlon": -68.7, "toplat": -25.5, "bottomlat": -40.5}
DOMAINS = {
    "atacama": (-71.4, -29.8, -68.9, -25.8),
    "coquimbo": (-71.8, -32.25, -69.75, -29.25),
    "rm": (-70.75, -34.25, -69.75, -33.25),
}
MEMBERS = ["gec00"] + [f"gep{n:02d}" for n in range(1, 31)]
STEPS = list(range(6, 121, 6))  # buckets de 6h limpios, 006..120
CACHE = os.path.expanduser("~/nowcast-forecast/.gefs_cache")


def log(msg):
    print(f"[gefs] {msg}", flush=True)


def filter_url(member, step, cycle_date, cycle_hz):
    q = {"file": f"{member}.t{cycle_hz}z.pgrb2s.0p25.f{step:03d}",
         "lev_surface": "on", "var_APCP": "on", "subregion": "",
         "dir": f"/gefs.{cycle_date}/{cycle_hz}/atmos/pgrb2sp25",
         **{k: str(v) for k, v in REGION.items()}}
    return f"{FILTER}?{urllib.parse.urlencode(q)}"


def fetch_one(member, step, cycle_date, cycle_hz):
    cache_f = os.path.join(CACHE, f"{cycle_date}{cycle_hz}_{member}_f{step:03d}.grib2")
    if not os.path.exists(cache_f):
        with urllib.request.urlopen(filter_url(member, step, cycle_date, cycle_hz), timeout=60) as r, \
                open(cache_f, "wb") as f:
            f.write(r.read())
    import xarray as xr
    ds = xr.open_dataset(cache_f, engine="cfgrib", backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    data = np.asarray(ds[var].values, "float64")
    lats, lons = ds.latitude.values, ds.longitude.values
    ds.close()
    return data, lats, lons


def domain_mask(lats, lons, bbox):
    w, s, e, n = bbox
    lon180 = np.where(lons > 180, lons - 360, lons)
    return np.ix_((lats >= s) & (lats <= n), (lon180 >= w) & (lon180 <= e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle-date", default="20260714")
    ap.add_argument("--cycle-hz", default="12")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)

    per_member_total = {d: {} for d in DOMAINS}
    masks_cache = {}

    def job(member, step):
        try:
            data, lats, lons = fetch_one(member, step, a.cycle_date, a.cycle_hz)
            return member, step, data, lats, lons, None
        except Exception as exc:
            return member, step, None, None, None, str(exc)

    tasks = [(m, s) for m in MEMBERS for s in STEPS]
    log(f"{len(tasks)} archivos a pedir ({len(MEMBERS)} miembros × {len(STEPS)} pasos de 6h)")
    done, errors = 0, 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(job, m, s) for m, s in tasks]
        for fut in as_completed(futs):
            member, step, data, lats, lons, err = fut.result()
            done += 1
            if err:
                errors += 1
                if errors <= 5:
                    log(f"ERROR {member} f{step:03d}: {err}")
                continue
            key = (lats.shape, lons.shape)
            if key not in masks_cache:
                masks_cache[key] = {d: domain_mask(lats, lons, bbox) for d, bbox in DOMAINS.items()}
            for d in DOMAINS:
                # unidades GRIB verificadas: "kg m**-2" = mm directamente (NO metros
                # como el tp de ECMWF) -- sin conversión.
                mx = float(data[masks_cache[key][d]].max())
                per_member_total[d].setdefault(member, 0.0)
                per_member_total[d][member] += mx
            if done % 100 == 0:
                log(f"{done}/{len(tasks)} archivos procesados ({errors} errores)")
    log(f"listo: {done}/{len(tasks)} procesados, {errors} errores")

    out = {}
    for d, by_member in per_member_total.items():
        arr = np.array(list(by_member.values()))
        if arr.size == 0:
            log(f"{d}: SIN DATOS (todos los miembros fallaron)")
            continue
        out[d] = {
            "n_members": int(arr.size),
            "min_mm": round(float(arr.min()), 1), "p25_mm": round(float(np.percentile(arr, 25)), 1),
            "median_mm": round(float(np.median(arr)), 1), "p75_mm": round(float(np.percentile(arr, 75)), 1),
            "max_mm": round(float(arr.max()), 1),
            "frac_over_100mm": round(float((arr >= 100).mean()), 2),
            "frac_over_150mm": round(float((arr >= 150).mean()), 2),
            "note": "precipitación TOTAL (agua+nieve equiv.), pgrb2s no trae isoterma — no partido líquido/sólido",
        }
        log(f"{d}: n={out[d]['n_members']} min={out[d]['min_mm']} p25={out[d]['p25_mm']} "
            f"mediana={out[d]['median_mm']} p75={out[d]['p75_mm']} max={out[d]['max_mm']} "
            f"| ≥100mm: {out[d]['frac_over_100mm']*100:.0f}% | ≥150mm: {out[d]['frac_over_150mm']*100:.0f}%")

    with open(os.path.expanduser("~/nowcast-forecast/latest/gefs_ensemble_check.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
