#!/usr/bin/env python3
"""Chequeo rápido: ¿el HRES determinístico de ECMWF es un outlier de su propio
ensemble, o es el escenario central? Motivado por la pregunta del usuario
(2026-07-14): los totales de lluvia líquida de ECMWF le parecían "increíbles"
tras 6 años estudiando el tema — la duda correcta no es "¿probamos el CFS?"
(modelo estacional, resolución ~1°, no resuelve una tormenta de días — ver
respuesta en el chat), sino "¿qué tan de acuerdo está ECMWF consigo mismo?".

Fuente: ECMWF Open Data, stream `enfo` (IFS ENS, 50 miembros perturbados,
0.25°, mismo bucket S3 que el HRES `oper` ya en producción). No hay miembro
de control (`cf`) publicado para este ciclo — se usan los 50 perturbados.

Método (deliberadamente simple, un solo paso): tp y sf ACUMULADOS del paso
f120 (=total de todo el horizonte, por la misma razón de acumulación que
fetch_ecmwf.py documenta — no hace falta diferenciar pasos intermedios para
esta pregunta puntual). Lluvia líquida = tp−sf, máximo dentro de cada bbox de
dominio, por miembro → distribución de 50 valores a comparar contra el único
valor determinístico (HRES) ya calculado por fetch_ecmwf.py.

Uso:
    python3 ecmwf_ensemble_check.py [--step 120]
"""
import argparse
import json
import os

import numpy as np

# Espejos con respaldo (ver ecmwf_mirrors.py: el bucket de AWS solo respondía
# 1 de cada ~6 pedidos en sep-2026).
from ecmwf_mirrors import fetch as mirror_fetch  # noqa: E402
CACHE = os.path.expanduser("~/nowcast-forecast/.enfo_cache")
DOMAINS = {
    "atacama": (-71.4, -29.8, -68.9, -25.8),
    "coquimbo": (-71.8, -32.25, -69.75, -29.25),
    "rm": (-70.75, -34.25, -69.75, -33.25),
}
N_MEMBERS = 50


def log(msg):
    print(f"[enfo] {msg}", flush=True)


def fetch(cycle_date, cycle_hz, step, number, param, idx):
    rec = next((r for r in idx if r["param"] == param and r.get("number") == str(number)), None)
    if rec is None:
        return None
    cache_f = os.path.join(CACHE, f"{cycle_date}{cycle_hz}_{step}h_{number}_{param}.grib2")
    if not os.path.exists(cache_f):
        grib_rel = (f"{cycle_date}/{cycle_hz}z/ifs/0p25/enfo/"
                    f"{cycle_date}{cycle_hz}0000-{step}h-enfo-ef.grib2")
        off, length = rec["_offset"], rec["_length"]
        data = mirror_fetch(grib_rel, timeout=90,
                            headers={"Range": f"bytes={off}-{off + length - 1}"})
        # atómico: antes el archivo se abría ANTES de leer, y un corte dejaba en
        # caché un GRIB vacío que la corrida siguiente daba por bueno
        with open(cache_f + ".tmp", "wb") as f:
            f.write(data)
        os.replace(cache_f + ".tmp", cache_f)
    return cache_f


def read_grib_value(path):
    import xarray as xr
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    data = np.asarray(ds[var].values, "float64")
    lats = ds.latitude.values
    lons = ds.longitude.values
    ds.close()
    return data, lats, lons


def domain_mask(lats, lons, bbox):
    w, s, e, n = bbox
    lon180 = np.where(lons > 180, lons - 360, lons)
    lon_mask = (lon180 >= w) & (lon180 <= e)
    lat_mask = (lats >= s) & (lats <= n)
    return np.ix_(lat_mask, lon_mask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle-date", default="20260714")
    ap.add_argument("--cycle-hz", default="12")
    ap.add_argument("--step", type=int, default=120)
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)

    idx_rel = (f"{a.cycle_date}/{a.cycle_hz}z/ifs/0p25/enfo/"
               f"{a.cycle_date}{a.cycle_hz}0000-{a.step}h-enfo-ef.index")
    log(f"índice: {idx_rel}")
    idx = [json.loads(l) for l in
           mirror_fetch(idx_rel, timeout=60).decode().strip().split("\n")]
    n_tp = sum(1 for r in idx if r["param"] == "tp")
    log(f"{n_tp} miembros con campo tp en el índice (perturbados, sin control cf)")

    liquid_by_domain = {d: [] for d in DOMAINS}
    lats = lons = None
    for number in range(1, N_MEMBERS + 1):
        try:
            f_tp = fetch(a.cycle_date, a.cycle_hz, a.step, number, "tp", idx)
            f_sf = fetch(a.cycle_date, a.cycle_hz, a.step, number, "sf", idx)
            tp, lats, lons = read_grib_value(f_tp)
            sf, _, _ = read_grib_value(f_sf)
        except Exception as exc:
            log(f"miembro {number}: ERROR {exc}")
            continue
        liquid = np.clip(tp - sf, 0.0, None) * 1000.0  # m -> mm, acumulado 0-120h
        for d, bbox in DOMAINS.items():
            mask = domain_mask(lats, lons, bbox)
            liquid_by_domain[d].append(float(liquid[mask].max()))
        if number % 10 == 0:
            log(f"{number}/{N_MEMBERS} miembros leídos")

    out = {}
    for d, vals in liquid_by_domain.items():
        arr = np.array(vals)
        out[d] = {
            "n_members": len(arr),
            "min_mm": round(float(arr.min()), 1), "p25_mm": round(float(np.percentile(arr, 25)), 1),
            "median_mm": round(float(np.median(arr)), 1), "p75_mm": round(float(np.percentile(arr, 75)), 1),
            "max_mm": round(float(arr.max()), 1),
            "frac_over_100mm": round(float((arr >= 100).mean()), 2),
            "frac_over_150mm": round(float((arr >= 150).mean()), 2),
        }
        log(f"{d}: n={len(arr)} min={out[d]['min_mm']} p25={out[d]['p25_mm']} "
            f"mediana={out[d]['median_mm']} p75={out[d]['p75_mm']} max={out[d]['max_mm']} "
            f"| ≥100mm: {out[d]['frac_over_100mm']*100:.0f}% | ≥150mm: {out[d]['frac_over_150mm']*100:.0f}%")

    with open(os.path.expanduser("~/nowcast-forecast/latest/ecmwf_ensemble_check.json"), "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
