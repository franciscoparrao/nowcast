#!/usr/bin/env python3
"""Descargas GLM (GOES-19) por hora el 20-oct-2026 en los dominios rm y ohiggins,
desde el acumulado del feed (fetch_glm.py → feeds/glm/counts.json, copiado en
evidencia/monitor_<dom>/glm_counts.json). Clave = inicio del paso semihorario UTC;
'domain' = descargas en el bbox; 'scars' = dentro de cada cicatriz vigente +5 km.
Los dos bbox se solapan en la fila −33.85 (San José de Maipo sur), así que las
sumas de ambos dominios NO se pueden sumar entre sí."""
import json, os
from datetime import datetime, timedelta

E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia")
for dom in ("rm", "ohiggins"):
    c = json.load(open(os.path.join(E, f"monitor_{dom}", "glm_counts.json")))
    hor, cic, mx = {}, {}, {}
    for k, v in c.items():
        if not k.startswith("20261020"):
            continue
        h = (datetime.strptime(k, "%Y%m%dT%H%M") - timedelta(hours=3)).strftime("%d-oct %Hh")
        hor[h] = hor.get(h, 0) + v["domain"]
        if v["max_cell"] > mx.get(h, (0, None))[0]:
            mx[h] = (v["max_cell"], v["max_cell_lonlat"])
        for s, n in (v.get("scars") or {}).items():
            cic[h] = cic.get(h, 0) + n
    tot = sum(hor.values())
    print(f"\n== {dom}: {tot} descargas el 20-oct (UTC; horas Chile)")
    for h in sorted(hor):
        if hor[h]:
            extra = f" | sobre cicatriz {cic[h]}" if cic.get(h) else ""
            print(f"  {h} Chile: {hor[h]:4d}  (máx en una celda y paso: {mx[h][0]} en {mx[h][1]}){extra}")
