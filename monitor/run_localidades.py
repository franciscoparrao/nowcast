#!/usr/bin/env python3
"""Corre runout + risk_map para cada localidad de la watchlist vigente.

Quinta etapa de la cadena de análisis (analysis_all.sh), separada como script
propio a propósito: la watchlist ya es la salida operacional del ciclo, así
que estos productos por localidad son ENRIQUECIMIENTO —el mapa de riesgo, el
footprint, el índice compuesto— y una localidad que falle (sin hazard en el
punto, footprint vacío, costa sin modelo) NO debe abortar el ciclo. Por eso
es blanda: registra el fallo y sigue.

Lee la watchlist.json DIRECTAMENTE (no vía campos en bash). El 2026-07-20 un
driver bash falló las 13 localidades porque el TSV intermedio tenía 6 campos
y el bucle leía 5 —a Coquimbo le llegó lon="COQUIMBO"—; pasar el JSON entero
a Python elimina esa clase de error de formato de raíz. Cada localidad trae
name/lon/lat/domain/comuna; se usan tal cual.

Idempotente por ciclo: si el runout de una localidad ya es del ciclo vigente
se salta (para que re-correr analysis_all no repita ~30 ABM). --forzar lo
ignora.

Uso (normalmente lo invoca analysis_all.sh):
    python3 run_localidades.py [--forecast ~/nowcast-forecast/latest]
        [--params chanaral_enhanced] [--source gfs] [--forzar]
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def ciclo_de(runout_dir, name):
    p = os.path.join(runout_dir, name, "resumen.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p)).get("cycle")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--params", default="chanaral_enhanced")
    ap.add_argument("--sombra-densidad", type=float,
                    default=float(os.environ.get("RUNOUT_SOMBRA_DENSIDAD") or 0) or None,
                    help="si se da, corre además el runout en modo gatillo con esta densidad de gotas "
                         "(sombra; no reemplaza al producto). Default: $RUNOUT_SOMBRA_DENSIDAD.")
    ap.add_argument("--runouts-sombra", default=os.path.expanduser("~/nowcast-forecast/runouts_sombra"))
    ap.add_argument("--source", default="gfs")
    ap.add_argument("--forzar", action="store_true",
                    help="re-correr aunque el runout ya sea del ciclo vigente")
    a = ap.parse_args()

    forecast = os.path.realpath(os.path.expanduser(a.forecast))
    ciclo = os.path.basename(forecast)
    wl_p = os.path.join(forecast, "watchlist.json")
    if not os.path.exists(wl_p):
        sys.exit(f"[localidades] falta {wl_p} — ¿corrió select_watchlist?")
    sel = json.load(open(wl_p)).get("selected", [])
    if not sel:
        sys.exit("[localidades] watchlist sin localidades seleccionadas")

    print(f"[localidades] ciclo {ciclo} · {len(sel)} localidades")
    hechas = saltadas = 0
    fallos_runout, fallos_risk, fallos_sombra = [], [], []

    for s in sel:
        name = s.get("name")
        lon, lat = s.get("lon"), s.get("lat")
        dom, comuna = s.get("domain"), s.get("comuna")
        if not (name and lon is not None and lat is not None and dom and comuna):
            print(f"[localidades] {name or '?'}: entrada incompleta, se salta", file=sys.stderr)
            continue

        if not a.forzar and ciclo_de(a.runouts, name) == ciclo:
            saltadas += 1
            continue

        r = subprocess.run(
            ["python3", os.path.join(HERE, "runout_localidad.py"),
             "--name", name, "--lon", str(lon), "--lat", str(lat),
             "--domain", dom, "--params", a.params, "--source", a.source],
            capture_output=True, text=True)
        if r.returncode != 0:
            fallos_runout.append(name)
            # sin runout no hay footprint; igual se intenta el mapa (da peligro/
            # excedencia aunque no exposición), pero se deja constancia.
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            print(f"[localidades] {name}: runout FALLÓ — {tail[0][:120]}", file=sys.stderr)

        # SOMBRA del gatillo externo (decisión 23-oct-2026): el mismo runout en
        # modo gatillo I-D con siembra FollowField, guardado aparte para
        # compararlo con el actual en eventos reales. Nunca reemplaza al
        # producto ni tumba el ciclo: si falla, se cuenta y se sigue.
        if a.sombra_densidad:
            rs = subprocess.run(
                ["python3", os.path.join(HERE, "runout_localidad.py"),
                 "--name", name, "--lon", str(lon), "--lat", str(lat),
                 "--domain", dom, "--params", a.params, "--source", a.source,
                 "--trigger", "--trigger-drops-per-km2", str(a.sombra_densidad),
                 "--out-root", a.runouts_sombra],
                capture_output=True, text=True)
            if rs.returncode != 0:
                fallos_sombra.append(name)
                tail = (rs.stderr or rs.stdout).strip().splitlines()[-1:] or [""]
                print(f"[localidades] {name}: sombra gatillo FALLÓ — {tail[0][:120]}", file=sys.stderr)

        k = subprocess.run(
            ["python3", os.path.join(HERE, "risk_map_localidad.py"),
             "--name", name, "--lon", str(lon), "--lat", str(lat),
             "--domain", dom, "--comuna", comuna],
            capture_output=True, text=True)
        if k.returncode != 0:
            fallos_risk.append(name)
            tail = (k.stderr or k.stdout).strip().splitlines()[-1:] or [""]
            print(f"[localidades] {name}: risk_map FALLÓ — {tail[0][:120]}", file=sys.stderr)

        hechas += 1

    print(f"[localidades] fin: {hechas} corridas · {saltadas} ya al día (saltadas) · "
          f"{len(fallos_runout)} runout fallidos {fallos_runout or ''} · "
          f"{len(fallos_risk)} risk_map fallidos {fallos_risk or ''}"
          + (f" · sombra gatillo (densidad {a.sombra_densidad}): {len(fallos_sombra)} fallidos {fallos_sombra or ''}"
             if a.sombra_densidad else ""))


if __name__ == "__main__":
    main()
