#!/usr/bin/env python3
"""EXPLORATORIO (no estaba en PROTOCOLO.md): ¿un umbral por tamaño del
episodio separa mejor los episodios con evento que el N por celda?

Sobre los episodios de la regla primaria (N=25), dos variantes:
  final   — usa las descargas totales del episodio (solo se conocen al final:
            cota optimista, no utilizable en vivo);
  causal  — el aviso sale en el primer bloque en que el episodio acumula X
            descargas; el adelanto se mide desde ese bloque.
Para no ajustar el umbral a los datos, se elige en 1-15 oct (máxima fracción
de episodios con evento que conserve al menos el 80 % de los eventos que se
detectan sin umbral) y se evalúa en 16-29 oct. Ese criterio reemplazó a uno
primero («detectar la mitad de todos los eventos») que resultó imposible por
construcción: ni sin umbral se llega, porque la mayoría de los eventos son
frontales. El cambio se hizo después de ver ese resultado.

Uso: python exploratorio_tamano.py [--glm DIR] [--catalogo CSV]
"""
import argparse
import os
from datetime import datetime, timezone

import numpy as np

import cruce as C

UMBRALES = [0, 25, 50, 100, 200, 400, 800]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glm", default=os.path.expanduser("~/nowcast-forecast/glm_retro/oct01_29"))
    ap.add_argument("--catalogo", default=os.path.join(os.path.dirname(__file__), "..", "..", "data",
                                                       "catalogo_eventos_2026-10.csv"))
    ap.add_argument("--fijo", type=int, action="append", default=[],
                    help="muestra las dos mitades con este X (mirada posterior, no validación)")
    a = ap.parse_args()
    tierra, _ = C.mascaras()
    t0, A, _ = C.cargar_glm(a.glm)
    lab, _ = C.episodios(A, tierra, 25)
    yy, xx = np.mgrid[0:C.NY, 0:C.NX]
    clat, clon = C.LAT0 + (yy + .5) * C.RES, C.LON0 + (xx + .5) * C.RES
    eps = []
    for k, sl in enumerate(C.ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        ti, yi, xi = np.nonzero(lab[sl] == k)
        ti, yi, xi = ti + sl[0].start, yi + sl[1].start, xi + sl[2].start
        orden = np.argsort(ti, kind="stable")
        ti, yi, xi = ti[orden], yi[orden], xi[orden]
        acum = np.cumsum(A[ti, yi, xi])
        eps.append(dict(id=k, ti=ti, yi=yi, xi=xi, celdas=set(zip(yi.tolist(), xi.tolist())), acum=acum,
                        t_ini=t0 + ti.min() * C.BIN, t_fin=t0 + (ti.max() + 1) * C.BIN,
                        total=int(acum[-1])))
    ev = C.leer_catalogo(a.catalogo)
    # emparejamiento idéntico a cruce.py (regla 3 del protocolo)
    for e in ev:
        e["ep"] = []
        d_ev = datetime.strptime(e["fecha"], "%Y-%m-%d").date()
        for ep in eps:
            if not any(C.hav_km(e["lat"], e["lon"], clat[y, x], clon[y, x]) <= C.RADIO_KM for y, x in ep["celdas"]):
                continue
            if e["t_utc"] is not None:
                ok = ep["t_ini"] - C.PRE <= e["t_utc"] <= ep["t_fin"] + C.POST
            else:
                ok = (ep["t_ini"] + C.CHILE).date() <= d_ev <= (ep["t_fin"] + C.CHILE).date()
            if ok:
                e["ep"].append(ep)
    corte = datetime(2026, 10, 16, tzinfo=timezone.utc)

    def evalua(X, causal, mitad):
        sel = [ep for ep in eps if ep["total"] >= X and (mitad is None or (ep["t_ini"] < corte) == (mitad == 1))]
        ids = {ep["id"] for ep in sel}
        evs = [e for e in ev if mitad is None or (datetime.strptime(e["fecha"], "%Y-%m-%d").replace(tzinfo=timezone.utc) < corte) == (mitad == 1)]
        con = {ep["id"] for e in evs for ep in e["ep"] if ep["id"] in ids}
        det = [e for e in evs if any(ep["id"] in ids for ep in e["ep"])]
        leads = []
        for e in det:
            if e["t_utc"] is None:
                continue
            best = None
            for ep in e["ep"]:
                if ep["id"] not in ids:
                    continue
                # primer bloque en aviso a <= 15 km del evento (como cruce.py) ...
                cerca = np.array([C.hav_km(e["lat"], e["lon"], clat[y, x], clon[y, x]) <= C.RADIO_KM
                                  for y, x in zip(ep["yi"], ep["xi"])])
                t_cerca = ep["ti"][np.argmax(cerca)]
                # ... y, en la variante causal, no antes de que el episodio junte X descargas
                t_x = ep["ti"][int(np.argmax(ep["acum"] >= X))] if causal else t_cerca
                t_av = t0 + max(t_cerca, t_x) * C.BIN
                lead = (e["t_utc"] - t_av).total_seconds() / 60
                best = lead if best is None else max(best, lead)
            leads.append(best)
        return len(sel), len(con), len(det), len(evs), leads

    print("EXPLORATORIO — umbral por descargas acumuladas del episodio (base: N=25)\n")
    print(f"{'X':>5} | {'episodios':>9} {'con ev':>6} {'precisión':>9} | {'eventos det':>11} | adelanto causal: mediana (n) · final: mediana")
    for X in UMBRALES:
        n, c, d, ne, lc = evalua(X, True, None)
        _, _, _, _, lf = evalua(X, False, None)
        med = lambda v: f"{np.median(v):5.0f}" if v else "    -"
        print(f"{X:5d} | {n:9d} {c:6d} {c / n if n else 0:9.0%} | {d:4d} de {ne:3d} | {med(lc)} ({len(lc)}) · {med(lf)}")

    for X in a.fijo:
        print(f"\nPOSTERIOR a ver los datos, X={X} fijo:")
        for mitad, nom in ((1, "1-15 oct"), (2, "16-29 oct")):
            n, c, d, ne, _ = evalua(X, True, mitad)
            n0, c0, d0, _, _ = evalua(0, True, mitad)
            print(f"  {nom}: {c}/{n} episodios con evento ({c / n if n else 0:.0%}), {d} de {ne} eventos"
                  f"  |  sin umbral: {c0}/{n0} ({c0 / n0 if n0 else 0:.0%}), {d0} de {ne}")

    print("\nValidación por mitades: el umbral se elige en 1-15 oct y se aplica a 16-29 oct")
    cand = []
    _, _, d_base, _, _ = evalua(0, True, 1)
    for X in UMBRALES:
        n, c, d, ne, _ = evalua(X, True, 1)
        if n and d >= 0.8 * d_base:
            cand.append((c / n, -X, X))
    if not cand:
        print("  ningún umbral conserva el 80 % de los eventos detectados sin umbral en 1-15 oct")
        return
    _, _, Xs = max(cand)
    for mitad, nom in ((1, "1-15 oct (elección)"), (2, "16-29 oct (prueba)")):
        n, c, d, ne, lc = evalua(Xs, True, mitad)
        n0, c0, d0, _, _ = evalua(0, True, mitad)
        print(f"  {nom}: X={Xs} → {c}/{n} episodios con evento ({c / n if n else 0:.0%}), {d} de {ne} eventos"
              f"  |  sin umbral: {c0}/{n0} ({c0 / n0 if n0 else 0:.0%}), {d0} de {ne}")


if __name__ == "__main__":
    main()
