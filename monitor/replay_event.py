#!/usr/bin/env python3
"""Replay offline de un evento archivado por el nowcast-monitor.

Consume el `archive/` de un dominio (la forzante completa preservada por
ARCHIVE_STEPS=1: steps de cada feed + marcas de hueco) y re-corre el motor
`nowcast` sobre ella — con los parámetros operacionales o con CUALQUIER otro
juego (barrido de umbrales, otro alert-level, un calibrador nuevo). Es la
mitad computacional del informe post-evento: convierte el archivo crudo en
series listas para figuras y tablas.

Salidas (en --out):
  timeline.csv     una fila por paso: clave temporal, UTC ISO, max_prob,
                   alerta (bool), n_celdas, fracción del dominio
  crossings.json   intervalos de alerta (inicio, fin, duración_h, pico,
                   celdas máximas) — la materia prima de la tabla de lead
                   times contra los reportes oficiales
  replay.json      metadatos completos de la corrida (parámetros, conteos
                   por feed, span, huecos) para reproducibilidad
  hazard/          (opcional, --hazard-tifs) GeoTIFF de peligro por paso
                   para la animación

El eje temporal se reconstruye con la misma lógica de fusión del monitor
(prepare_fusion): unión de ventanas, cero-relleno contado donde un feed no
tiene dato. Los timestamps del timeline vienen de las claves de los steps —
por eso el replay puede afirmar "cruzó a las 03:30 UTC" con autoridad.

Uso típico (tras rsync del dominio desde el nodo):
    python3 replay_event.py --data ~/evento/rm --out ~/evento/rm/replay \
        --nowcast-bin ../target/release/nowcast
    # barrido: ¿a qué nivel habría alertado antes?
    python3 replay_event.py --data ~/evento/rm --out /tmp/r2 --alert-level 0.3
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import timezone

from prepare_fusion import feed_steps, key_time, make_zero

STEP_H = 0.5


def build_axis(feeds):
    """Unión de claves de todos los feeds → eje semihorario contiguo."""
    from datetime import timedelta

    all_keys = sorted({k for steps, _ in feeds.values() for k in steps})
    if not all_keys:
        sys.exit("replay: el archivo no tiene steps")
    axis, t, last = [], key_time(all_keys[0]), key_time(all_keys[-1])
    while t <= last:
        axis.append(t.strftime("%Y%m%dT%H%M"))
        t += timedelta(minutes=30)
    return axis


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True,
                    help="directorio del dominio (contiene archive/)")
    ap.add_argument("--out", required=True, help="directorio de salida")
    ap.add_argument("--nowcast-bin", default="nowcast")
    ap.add_argument("--id-a", type=float, default=14.82)
    ap.add_argument("--id-b", type=float, default=0.39)
    ap.add_argument("--k", type=float, default=4.0)
    ap.add_argument("--max-window", type=int, default=96)
    ap.add_argument("--alert-level", type=float, default=0.5)
    ap.add_argument("--combine", default="noisy-or",
                    choices=["noisy-or", "max", "product"])
    ap.add_argument("--susc", help="GeoTIFF de susceptibilidad (default uniforme 1.0)")
    ap.add_argument("--calibrator", help="JSON de `nowcast calibrate`")
    ap.add_argument("--hazard-tifs", action="store_true",
                    help="escribir GeoTIFFs de peligro por paso (animación)")
    ap.add_argument("--primary-only", action="store_true",
                    help="solo el feed primario, sin fusión (contrafactual)")
    a = ap.parse_args()

    arch = os.path.join(a.data, "archive")
    if not os.path.isdir(arch):
        sys.exit(f"replay: no existe {arch} (¿ARCHIVE_STEPS estaba activo?)")
    feeds = {}
    for name in sorted(os.listdir(arch)):
        steps, marks = feed_steps(os.path.join(arch, name))
        if steps:
            feeds[name] = (steps, marks)
    if "primary" not in feeds:
        sys.exit("replay: el archivo no tiene feed primario")
    if a.primary_only:
        feeds = {"primary": feeds["primary"]}

    axis = build_axis(feeds)
    os.makedirs(a.out, exist_ok=True)
    zero_tif = os.path.join(a.out, "_zero.tif")
    make_zero(next(iter(feeds["primary"][0].values())), zero_tif)

    lists, counts = {}, {}
    for name, (steps, marks) in feeds.items():
        files = [steps.get(k, zero_tif) for k in axis]
        lists[name] = ",".join(files)
        counts[name] = {
            "real": sum(1 for k in axis if k in steps and k not in marks),
            "gapmarks": sum(1 for k in axis if k in marks),
            "zerofill": sum(1 for k in axis if k not in steps),
        }

    cmd = [a.nowcast_bin, "run",
           "--rain-rasters", lists["primary"],
           "--dt-hours", str(STEP_H), "--max-window", str(a.max_window),
           "--id-a", str(a.id_a), "--id-b", str(a.id_b), "--k", str(a.k),
           "--alert-level", str(a.alert_level), "--format", "json"]
    cmd += ["--susc", a.susc] if a.susc else ["--uniform-susc", "1.0"]
    if a.calibrator:
        cmd += ["--calibrator", a.calibrator]
    fused = [n for n in feeds if n != "primary"]
    for name in fused:
        cmd += ["--fuse-rasters", lists[name]]
    if fused:
        cmd += ["--combine", a.combine]
    if a.hazard_tifs:
        hz = os.path.join(a.out, "hazard")
        os.makedirs(hz, exist_ok=True)
        cmd += ["--out-dir", hz]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 2):
        sys.exit(f"replay: el motor falló (rc={proc.returncode}):\n{proc.stderr}")
    run = json.loads(proc.stdout)

    # timeline.csv — el índice de paso del motor mapea 1:1 al eje.
    rows = []
    for s in run["steps"]:
        k = axis[s["step"]]
        al = s.get("alert")
        rows.append({
            "step_key": k,
            "utc": key_time(k).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "max_prob": s["max_probability"],
            "alert": bool(al),
            "n_cells": al["n_cells"] if al else 0,
            "fraction": al["fraction"] if al else 0.0,
        })
    with open(os.path.join(a.out, "timeline.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    # crossings.json — intervalos contiguos de alerta.
    crossings, cur = [], None
    for r in rows:
        if r["alert"] and cur is None:
            cur = {"start": r["utc"], "start_key": r["step_key"],
                   "peak_prob": r["max_prob"], "peak_cells": r["n_cells"]}
        elif r["alert"]:
            cur["peak_prob"] = max(cur["peak_prob"], r["max_prob"])
            cur["peak_cells"] = max(cur["peak_cells"], r["n_cells"])
        elif cur is not None:
            cur["end"] = r["utc"]
            cur["duration_h"] = round(
                (key_time(r["step_key"]) - key_time(cur["start_key"])).total_seconds() / 3600, 1)
            crossings.append(cur)
            cur = None
    if cur is not None:
        cur["end"] = rows[-1]["utc"]
        cur["duration_h"] = round(
            (key_time(rows[-1]["step_key"]) - key_time(cur["start_key"])).total_seconds() / 3600 + STEP_H, 1)
        crossings.append(cur)
    with open(os.path.join(a.out, "crossings.json"), "w") as f:
        json.dump(crossings, f, indent=1)

    meta = {
        "data": os.path.abspath(a.data),
        "span": [axis[0], axis[-1]], "n_steps": len(axis),
        "feeds": counts, "fused": fused, "combine": a.combine if fused else None,
        "params": {"id_a": a.id_a, "id_b": a.id_b, "k": a.k,
                   "max_window": a.max_window, "alert_level": a.alert_level,
                   "susc": a.susc or "uniform 1.0", "calibrator": a.calibrator},
        "n_alerts": run["n_alerts"], "n_crossings": len(crossings),
    }
    with open(os.path.join(a.out, "replay.json"), "w") as f:
        json.dump(meta, f, indent=1)
    os.remove(zero_tif)

    print(f"replay: {len(axis)} pasos ({axis[0]} → {axis[-1]}), "
          f"{run['n_alerts']} en alerta, {len(crossings)} cruce(s)")
    for c in crossings:
        print(f"  cruce {c['start']} → {c['end']} ({c['duration_h']} h, "
              f"pico {c['peak_prob']:.2f}, {c['peak_cells']} celdas)")
    print(f"salidas en {a.out}/: timeline.csv, crossings.json, replay.json"
          + (", hazard/" if a.hazard_tifs else ""))


if __name__ == "__main__":
    main()
