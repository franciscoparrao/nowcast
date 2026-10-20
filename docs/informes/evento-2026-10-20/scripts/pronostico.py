#!/usr/bin/env python3
"""Pronóstico comunal (comuna_impact, peor de GFS/ECMWF/ICON × terreno) de los
ciclos 00z y 06z del 20-oct-2026 para la RM y O'Higgins, ventanas de cruce de GFS
con a*=5.5 (00z, 06z y 12z) y contraste con lo medido por las EMAs en su comuna.
Fuentes: evidencia/comunas_<ciclo>.json (atributos del GeoJSON comunal, sin
geometría) y evidencia/crossings_<dominio>_<ciclo>.json."""
import json, os

E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia")
REG = {13: "RM", 6: "O'Higgins"}
for cyc in ("20261020T00z", "20261020T06z"):
    rows = json.load(open(os.path.join(E, f"comunas_{cyc}.json")))
    print(f"\n== ciclo {cyc}")
    for cod, nom in REG.items():
        r = sorted([x for x in rows if int(x["COD_REGION"]) == cod], key=lambda x: -x["peak_worst"])
        n = {k: sum(x["nivel"] == k for x in r) for k in ("ALTO", "MEDIO", "BAJO")}
        alto = ", ".join(f"{x['COMUNA'].title()} {x['peak_worst']:.2f}" for x in r if x["nivel"] == "ALTO")
        print(f"  {nom:10s} {n} | ALTO: {alto or '—'}")
        # qué modelo empuja: cuántas comunas ALTO lo son por un solo modelo
        uno = sum(1 for x in r if x["nivel"] == "ALTO" and sum((x[f"peak_{m}"] or 0) >= 0.5 for m in ("gfs", "ecmwf", "icon")) == 1)
        print(f"  {'':10s} comunas ALTO sostenidas por un solo modelo ≥0.5: {uno} de {n['ALTO']}")

print("\n== ventanas de cruce GFS (a*=5.5), hora UTC")
for cyc in ("20261020T00z", "20261020T06z", "20261020T12z"):
    for dom in ("rm", "chile-centro-sur"):
        p = os.path.join(E, f"crossings_{dom}_{cyc}.json")
        if os.path.exists(p):
            cr = [c for c in json.load(open(p)) if c["start"] < "2026-10-21T06"]
            print(f"  {cyc} {dom:17s}: " + ("; ".join(f"{c['start'][5:16]}→{c['end'][5:16]} pico {c['peak_prob']:.2f} ({c['peak_cells']} celdas)" for c in cr) or "sin cruces hasta el 21-oct 06Z"))

print("\n== comunas con EMA: nivel pronosticado (06z) vs lo medido (scripts/estaciones.py)")
EMA = {"LA REINA": "Tobalaba", "LA FLORIDA": "La Florida", "SAN JOSÉ DE MAIPO": "S. J. de Guayacán",
       "LO BARNECHEA": "El Colorado", "RANCAGUA": "Rancagua", "COLTAUCO": "Quimávida",
       "MACHALÍ": "Portezuelo-Codelco", "PUMANQUE": "Nilahue", "PICHILEMU": "Pichilemu"}
rows = {x["COMUNA"]: x for x in json.load(open(os.path.join(E, "comunas_20261020T06z.json")))}
for com, st in EMA.items():
    x = rows[com]
    print(f"  {com.title():18s} ({st:18s}): {x['nivel']:5s} peor {x['peak_worst']:.2f} | gfs {x['peak_gfs'] or 0:.2f} ecmwf {x['peak_ecmwf'] or 0:.2f} icon {x['peak_icon'] or 0:.2f}")
