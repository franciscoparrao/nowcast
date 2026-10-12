#!/usr/bin/env python3
"""Pico del dominio RM por modelo (umbral regional a*=5.5) en los últimos ciclos del pronóstico.
Uso: tendencia_rm.py <raíz de nowcast-forecast> <ciclo_desde> <ciclo_hasta>"""
import glob, json, os, sys
root, lo, hi = os.path.expanduser(sys.argv[1]), sys.argv[2], sys.argv[3]
for c in sorted(os.path.basename(p) for p in glob.glob(f"{root}/2026*T*z")):
    if not (lo <= c <= hi): continue
    row = c
    for m, p in (("GFS", f"{c}/rm/forecast.json"), ("ECMWF", f"{c}/rm/ecmwf/ecmwf.json"), ("ICON", f"{c}/rm/icon/icon.json")):
        p = os.path.join(root, p)
        if not os.path.exists(p): row += f" | {m}: —"; continue
        v = json.load(open(p))["variants"].get("regional-a5.5", {})
        row += f" | {m} pico {v.get('peak_prob', float('nan')):.2f} pasos {v.get('n_alert_steps', '?')}"
    print(row)
