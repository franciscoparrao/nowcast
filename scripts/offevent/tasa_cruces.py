#!/usr/bin/env python3
"""Tasa de cruces fuera de evento en los tres núcleos (ver PROTOCOLO.md).

Baja IMERG Final V07 semihorario por año desde Giovanni (caché en
~/nowcast-forecast/offevent/), calcula la excedencia I–D por paso con las
mismas ventanas del experimento de un solo factor y cuenta los días UTC con
cruce fuera de la ventana del evento. Resultado en
data/offevent/resultados.json.

Uso: python tasa_cruces.py   (venv del monitor: requests + earthaccess + numpy)
"""
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np

from giovanni import serie

NUCLEOS = {
    "atacama_2015": dict(lat=-27.15, lon=-70.45, dia=date(2015, 3, 25)),
    "maipo_2017": dict(lat=-33.45, lon=-70.55, dia=date(2017, 2, 25)),
    "santalucia_2017": dict(lat=-43.45, lon=-72.15, dia=date(2017, 12, 16)),
}
ANIOS = range(1998, 2025)
CURVAS = {"a*=5.5": (5.5, 0.39), "Caine": (14.82, 0.39)}
B = 0.39
CACHE = os.path.expanduser("~/nowcast-forecast/offevent")
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "offevent")
VENTANA_EV = 2  # días a cada lado del día documentado
MAX_FALTA = 0.10


def anio(key, y):
    f = os.path.join(CACHE, f"{key}_{y}.csv")
    if not os.path.exists(f):
        n = NUCLEOS[key]
        s = serie(n["lat"], n["lon"], f"{y}-01-01T00:00:00", f"{y}-12-31T23:30:00")
        with open(f + ".part", "w") as fh:
            fh.writelines(f"{t},{v}\n" for t, v in s)
        os.replace(f + ".part", f)
    return [(r[0], float(r[1])) for r in csv.reader(open(f))]


def e_max_semihoraria(depth, a, b):
    """E máximo por paso sobre ventanas de 0.5 a 24 h (48 pasos de 30 min)."""
    c = np.concatenate([[0.0], np.cumsum(depth)])
    out = np.zeros(len(depth))
    for m in range(1, 49):
        s = np.full(len(depth), 0.0)
        s[m - 1:] = c[m:] - c[:-m]
        d = 0.5 * m
        out = np.maximum(out, (s / d) / (a * d ** -b))
    return out


def e_max_diaria(tot, a, b):
    """E máximo por día sobre ventanas de 1 a 3 días."""
    c = np.concatenate([[0.0], np.cumsum(tot)])
    out = np.zeros(len(tot))
    for m in range(1, 4):
        s = np.zeros(len(tot))
        s[m - 1:] = c[m:] - c[:-m]
        d = 24.0 * m
        out = np.maximum(out, (s / d) / (a * d ** -b))
    return out


def analizar(key, filas, a, b, desde=None):
    n = NUCLEOS[key]
    t = [x[0] for x in filas]
    rate = np.array([x[1] for x in filas])
    falta = ~np.isfinite(rate)
    depth = np.where(falta, 0.0, rate) * 0.5
    dias = np.array([date.fromisoformat(x[:10]) for x in t])
    udias, inv = np.unique(dias, return_inverse=True)
    falta_dia = np.bincount(inv, weights=falta.astype(float)) / np.bincount(inv)
    ok = falta_dia <= MAX_FALTA
    if desde:
        ok &= udias >= desde
    e_sub_paso = e_max_semihoraria(depth, a, b)
    e_sub = np.zeros(len(udias))
    np.maximum.at(e_sub, inv, e_sub_paso)
    tot = np.bincount(inv, weights=depth)
    e_dia = e_max_diaria(tot, a, b)
    ev = np.array([abs((d - n["dia"]).days) <= VENTANA_EV for d in udias])
    res = {}
    for nombre, e in (("semihorario", e_sub), ("diario", e_dia)):
        cruza = (e >= 1) & ok
        fuera = cruza & ~ev
        anios = len(set(d.year for d in udias[ok]))
        # episodios: días con cruce consecutivos (en el calendario) cuentan como uno
        idx = np.where(fuera)[0]
        epis = int(len(idx) and 1 + np.sum(np.diff([udias[i].toordinal() for i in idx]) > 1))
        e_ev = float(e[ev].max())
        res[nombre] = dict(
            dias_con_cruce_fuera=int(fuera.sum()),
            dias_por_anio=round(float(fuera.sum()) / anios, 2),
            episodios_por_anio=round(epis / anios, 2),
            cruza_en_evento=bool(e_ev >= 1),
            e_max_evento=round(e_ev, 2),
            percentil_evento=round(100 * float((e[ok & ~ev] < e_ev).mean()), 3),
            dias_fuera_con_e_mayor_igual_al_evento=int(((e >= e_ev) & ok & ~ev).sum()),
        )
    res["dias_validos"] = int(ok.sum())
    res["dias_excluidos_por_falta"] = int((~(falta_dia <= MAX_FALTA)).sum())
    return res


def main():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    tareas = [(k, y) for k in NUCLEOS for y in ANIOS]
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda kt: anio(*kt), tareas))
    out = {}
    for key in NUCLEOS:
        filas = [r for y in ANIOS for r in anio(key, y)]
        out[key] = {}
        for cname, (a, b) in CURVAS.items():
            out[key][cname] = analizar(key, filas, a, b)
            out[key][cname + " (2014-2024)"] = analizar(key, filas, a, b, desde=date(2014, 1, 1))
        out[key]["pasos"] = len(filas)
    json.dump(out, open(os.path.join(OUT, "resultados.json"), "w"), indent=1, ensure_ascii=False)
    for key, r in out.items():
        for cname in CURVAS:
            for res in ("semihorario", "diario"):
                x = r[cname][res]
                print(f"{key:16s} {cname:7s} {res:11s} fuera: {x['dias_por_anio']:6.2f} días/año, "
                      f"{x['episodios_por_anio']:5.2f} episodios/año | evento cruza: {x['cruza_en_evento']!s:5s} "
                      f"E {x['e_max_evento']:6.2f}, percentil {x['percentil_evento']:7.3f}, "
                      f"días fuera con E ≥ evento {x['dias_fuera_con_e_mayor_igual_al_evento']}")
    import sys; sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
