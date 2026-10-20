#!/usr/bin/env python3
"""Caída del buscador CMR de Earthdata el 20-oct-2026: qué ciclos de monitor@rm y
monitor@ohiggins no corrieron y desde cuándo corren por el respaldo de GES DISC
(commit d9ffb75). Fuentes: evidencia/monitor_<dom>/cmr_lineas.txt (líneas de la
bitácora con el 500 de CMR, el aviso del respaldo o FETCH FALLÓ; el 500 no lleva
hora propia, se lee del extremo de la consulta temporal) y el extracto del día."""
import os, re
from datetime import datetime, timedelta

E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia")
for dom in ("rm", "ohiggins"):
    lin = open(os.path.join(E, f"monitor_{dom}", "cmr_lineas.txt")).read().splitlines()
    t500 = sorted({re.search(r"temporal%5B%5D=[^,]+,(\d{4}-\d\d-\d\dT\d\d:\d\d)", l).group(1)
                   for l in lin if "500 Server Error" in l and "temporal" in l})
    falla = [l[1:17] for l in lin if "FETCH FALL" in l and l.startswith("[")]
    log = open(os.path.join(E, f"monitor_{dom}", "monitor_log_20261020.txt")).read().splitlines()
    resp = [l for l in lin if "AVISO: búsqueda CMR" in l]
    print(f"\n== {dom}: consultas CMR con 500 (hora UTC de la consulta): {len(t500)}")
    print("  " + ", ".join(t[5:] for t in t500))
    print(f"  ciclos con FETCH FALLÓ (aviso o suprimido por histéresis): {len(falla)}: " + ", ".join(f[5:] for f in falla))
    print(f"  ciclos que entraron al respaldo GES DISC (CMR falló, siguió por listado): {len(resp)}")
    ciclos = [l[1:17] for l in log if "fusión (" in l]
    hueco = []
    for a, b in zip(ciclos, ciclos[1:]):
        ta, tb = (datetime.strptime(x, "%Y-%m-%dT%H:%M") for x in (a, b))
        if tb - ta > timedelta(minutes=45):
            hueco.append(f"{a[11:]}→{b[11:]}Z ({(tb - ta).total_seconds() / 3600:.1f} h)")
    print(f"  ciclos completos el 20-oct: {len(ciclos)}; huecos > 45 min entre ciclos: {hueco or '—'}")
