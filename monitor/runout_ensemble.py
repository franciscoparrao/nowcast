#!/usr/bin/env python3
"""Ensemble multi-fuente de runouts: rango GFS/ECMWF/ICON por localidad.

Un solo `runout_localidad.py` (fuente única, casi siempre GFS) presenta un
footprint determinístico como si fuera LA respuesta. Pero ya sabemos (recheck
del 2026-07-16/17) que GFS/ECMWF/ICON discrepan bastante en magnitud de
lluvia — el mismo desacuerdo debería propagarse al footprint. Este script
corre `runout_localidad.py --source ecmwf` y `--source icon` para cada
localidad de la watchlist vigente (GFS ya está corrido por separado), y
agrega los 3 miembros en un rango min/mediana/max de área afectada, más
cuántos de los 3 pasan el guard de forma — reemplaza "un número" por "un
rango con acuerdo/desacuerdo entre modelos", que es lo que hay de verdad.

Las corridas ECMWF/ICON quedan en `<runouts>/<name>__ecmwf/` y
`<name>__icon/` — sufijo `__fuente` para no colisionar con la corrida GFS
base ni con el filtro de watchlist vigente de `build_registro.py` (que solo
muestra tarjetas para nombres EXACTOS de la selección; las variantes de
ensemble quedan como datos de apoyo, no localidades adicionales en el mapa).

Uso:
    python3 runout_ensemble.py [--forecast ~/nowcast-forecast/latest]
        [--runouts ~/nowcast-forecast/runouts] [--skip-run]
Salida: <forecast>/runout_ensemble.json

--skip-run asume que las corridas __ecmwf/__icon ya existen (solo agrega).
"""

import argparse
import json
import os
import statistics
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_member(name, row, source, repo):
    out_name = f"{name}__{source}"
    cmd = ["python3", "monitor/runout_localidad.py",
           "--name", out_name, "--lon", str(row["lon"]), "--lat", str(row["lat"]),
           "--domain", row["domain"], "--source", source]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[{out_name}] FALLO rc={proc.returncode}: {proc.stderr[-600:]}", file=sys.stderr)
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--skip-run", action="store_true",
                     help="no correr ECMWF/ICON, asumir que ya existen")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    watchlist = json.load(open(os.path.join(forecast_dir, "watchlist.json")))

    if not a.skip_run:
        for row in watchlist["selected"]:
            for source in ("ecmwf", "icon"):
                print(f"[{row['name']}__{source}] corriendo...", file=sys.stderr)
                run_member(row["name"], row, source, REPO)

    ensemble = {}
    for row in watchlist["selected"]:
        name = row["name"]
        members = {}
        for src, dirname in [("gfs", name), ("ecmwf", f"{name}__ecmwf"), ("icon", f"{name}__icon")]:
            stats_p = os.path.join(a.runouts, dirname, "stats.json")
            resumen_p = os.path.join(a.runouts, dirname, "resumen.json")
            if not (os.path.exists(stats_p) and os.path.exists(resumen_p)):
                continue
            s = json.load(open(stats_p))
            r = json.load(open(resumen_p))
            members[src] = {
                "affected_km2": s["runout"]["affected_km2"],
                "window_frac": s["runout"]["window_frac"],
                "physically_plausible": r["physically_plausible"],
                "hazard_max_regional": r["signal"].get("hazard_max_regional"),
            }
        if not members:
            print(f"[{name}] sin miembros de ensemble disponibles, se omite", file=sys.stderr)
            continue
        km2_vals = [m["affected_km2"] for m in members.values()]
        ensemble[name] = {
            "members": members,
            "affected_km2_min": min(km2_vals),
            "affected_km2_max": max(km2_vals),
            "affected_km2_median": statistics.median(km2_vals),
            "n_members": len(members),
            "n_plausible": sum(1 for m in members.values() if m["physically_plausible"]),
        }

    out_p = os.path.join(forecast_dir, "runout_ensemble.json")
    with open(out_p, "w") as f:
        json.dump(ensemble, f, indent=1, ensure_ascii=False)
    print(f"ensemble de {len(ensemble)} localidades -> {out_p}")


if __name__ == "__main__":
    main()
