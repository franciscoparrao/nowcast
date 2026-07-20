#!/usr/bin/env python3
"""Alertas oficiales de SENAPRED (verde/amarilla/roja) al registro de verificación.

Fuente: el FeatureServer que alimenta el dashboard público de SENAPRED
(services3.arcgis.com/CNzkI2T3GmfwkaAR). Es la única fuente ESTRUCTURADA y
autoritativa que encontré: entrega comuna, tipo de alerta, causalidad y fecha
de inicio, consultable por región.

LÍMITE IMPORTANTE, medido y no supuesto (2026-07-20): el campo `FECHA_INI`
tiene resolución de DÍA ("18-07-2026"), no de hora. Y las horas de los avisos
SAE de evacuación —que son las que servirían para puntuar anticipación— no
están en este servicio ni en las compilaciones de prensa que revisé
(Meganoticias, T13, La Tercera solo dan fecha). Aparecen sueltas en notas
individuales, cuando el redactor las incluye.

Consecuencia para la verificación: con esta fuente el registro puede anotar
QUÉ comuna entró en alerta y QUÉ día, pero no puede medir anticipación con
precisión horaria. De todo el evento del Huasco quedó UN solo punto con hora
(Alto del Carmen, evacuación 05:50, de una nota que sí la traía). Conseguir
horas sistemáticas requiere otra vía —solicitud a SENAPRED, o capturar los
mensajes SAE en vivo— y hasta entonces esa es la cota de lo que se puede
puntuar. Se documenta acá para que nadie asuma una precisión que el dato no
tiene.

Uso:
    python3 fetch_senapred_alertas.py --region ATACAMA --anotar
    python3 fetch_senapred_alertas.py --region COQUIMBO
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = ("https://services3.arcgis.com/CNzkI2T3GmfwkaAR/arcgis/rest/services/"
        "METEOROLOGICAS_{nivel}/FeatureServer/0/query")
NIVELES = ("ROJA", "AMARILLA", "VERDE")
UA = {"User-Agent": "nowcast-verificacion/0.1 (investigacion geohazards)"}


def consultar(nivel, region=None, timeout=60):
    donde = "1=1" if not region else f"REGION LIKE '%{region.upper()}%'"
    q = urllib.parse.urlencode({
        "where": donde,
        "outFields": "COMUNA,PROVINCIA,REGION,TIPO_ALERT,CAUSALIDAD,FECHA_INI,CUT_COM",
        "returnGeometry": "false", "f": "json"})
    url = BASE.format(nivel=nivel) + "?" + q
    req = urllib.request.Request(url, headers=UA)
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception as exc:
        print(f"[senapred] {nivel}: fallo la consulta ({type(exc).__name__}) — se omite",
              file=sys.stderr)
        return []
    if "error" in d:
        print(f"[senapred] {nivel}: {d['error']}", file=sys.stderr)
        return []
    return [f["attributes"] for f in d.get("features", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="filtro por región (subcadena, ej. ATACAMA)")
    ap.add_argument("--anotar", action="store_true",
                    help="escribe cada alerta como evento en el registro de verificación")
    a = ap.parse_args()

    todas = []
    for nivel in NIVELES:
        filas = consultar(nivel, a.region)
        for r in filas:
            r["_nivel"] = nivel
        todas.extend(filas)

    if not todas:
        sys.exit("sin alertas vigentes para ese filtro (o el servicio no respondió)")

    orden = {"ROJA": 0, "AMARILLA": 1, "VERDE": 2}
    todas.sort(key=lambda r: (orden.get(r["_nivel"], 9), str(r.get("REGION")), str(r.get("COMUNA"))))
    print(f"{'nivel':10s} {'comuna':24s} {'region':24s} {'desde':12s} causalidad")
    for r in todas:
        print(f"{r['_nivel']:10s} {str(r.get('COMUNA'))[:23]:24s} {str(r.get('REGION'))[:23]:24s} "
              f"{str(r.get('FECHA_INI'))[:11]:12s} {str(r.get('CAUSALIDAD'))[:44]}")

    if a.anotar:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from verif_log import anotar
        n = 0
        for r in todas:
            f = str(r.get("FECHA_INI") or "")
            cuando = None
            if len(f) >= 10 and f[2] == "-" and f[5] == "-":
                # "18-07-2026" → ISO. Se fija 00:00Z y se DECLARA en la nota que
                # la hora es desconocida: inventar una hora plausible sería peor
                # que declarar la imprecisión.
                cuando = f"{f[6:10]}-{f[3:5]}-{f[0:2]}T00:00:00Z"
            anotar({"tipo": "evento",
                    "lugar": str(r.get("COMUNA", "")).lower().replace(" ", "_"),
                    "que": f"Alerta {r['_nivel'].capitalize()} SENAPRED — {r.get('CAUSALIDAD')}",
                    "cuando": cuando,
                    "fuente": "SENAPRED (FeatureServer oficial)",
                    "url": BASE.format(nivel=r["_nivel"]).replace("/query", ""),
                    "precision": "dia",
                    "nota": ("RESOLUCIÓN DE DÍA: FECHA_INI no trae hora, se fija 00:00Z como "
                             "marcador. NO usar para medir anticipación horaria."),
                    "region": r.get("REGION"), "cut_com": r.get("CUT_COM")})
            n += 1
        print(f"\n{n} alertas anotadas en el registro (con su límite de resolución declarado)")


if __name__ == "__main__":
    main()
