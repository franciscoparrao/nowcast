#!/usr/bin/env python3
"""Contrafactual: ¿cuándo tuvo el sistema la señal, y cuánto antes del hecho?

Recorre los ciclos ARCHIVADOS en el workdir y, para cada localidad, reconstruye
qué decía cada uno: lluvia pronosticada en el punto y hora del cruce I-D. Luego
lo contrasta con los eventos reales anotados en el registro de verificación
(`verif_log.py`), y reporta la anticipación de cada ciclo respecto del hecho.

POR QUÉ (2026-07-20). Alto del Carmen mostró que la pregunta "¿cuánto
anticipamos?" no se responde mirando el ciclo vigente: el sistema tenía la
señal 29.7 h antes —el ciclo 20260716T18z ponía el cruce el 18-jul 06:00
local y la evacuación fue a las 05:50— pero estaba invisible por el punto
ciego de la watchlist. Sin recorrer el archivo, esa anticipación no se ve.

QUÉ MIDE Y QUÉ NO. El "cruce" es cuando el modelo dice que se supera el
umbral I-D, y el "evento" suele ser la hora en que la AUTORIDAD actuó, que
va después del hecho físico. Así que la anticipación calculada acá es contra
la decisión, no contra el fenómeno: es una cota INFERIOR de lo tarde que
llegó el modelo y no una medida de cuánto se adelantó al aluvión. Se reporta
así a propósito, en vez de un número que suene mejor de lo que es.

Uso:
    python3 contrafactual.py --puntos puntos.json [--lugar alto_del_carmen]
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKDIR = os.path.expanduser("~/nowcast-forecast")


def cycles_disponibles(workdir):
    out = []
    for d in sorted(glob.glob(os.path.join(workdir, "20*"))):
        b = os.path.basename(d)
        if b.endswith("_icon") or b.endswith("_ecmwf") or not b.endswith("z"):
            continue
        if os.path.exists(os.path.join(d, "atacama")) or os.path.exists(os.path.join(d, "coquimbo")):
            out.append(b)
    return out


# El registro guarda, bajo el mismo tipo `evento`, dos cosas distintas: hechos
# de TERRENO (una evacuación, una quebrada que activó) y anotaciones ANALÍTICAS
# nuestras (un test que salió negativo, un contrafactual). Para medir
# anticipación solo sirven las primeras: contra una nota propia el resultado es
# circular. Se distinguen por la procedencia, que por eso es obligatoria.
FUENTES_ANALISIS = ("análisis propio", "analisis propio", "verificación propia",
                    "verificacion propia", "control propio")


def es_hecho_de_terreno(r):
    f = str(r.get("fuente") or "").lower()
    return not any(m in f for m in FUENTES_ANALISIS)


def eventos_del_registro(lugar=None, incluir_analisis=False):
    from verif_log import leer
    evs = []
    for r in leer():
        if r.get("tipo") != "evento" or not r.get("cuando"):
            continue
        if lugar and r.get("lugar") != lugar:
            continue
        if not incluir_analisis and not es_hecho_de_terreno(r):
            continue
        try:
            t = datetime.strptime(r["cuando"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            try:
                t = datetime.strptime(r["cuando"], "%Y-%m-%dT%H:%MZ")
            except ValueError:
                continue
        evs.append((t, r))
    return sorted(evs, key=lambda x: x[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--puntos", required=True)
    ap.add_argument("--lugar", help="solo esta localidad")
    ap.add_argument("--workdir", default=WORKDIR)
    ap.add_argument("--variante", default="regional-a5.5")
    ap.add_argument("--incluir-analisis", action="store_true",
                    help="usar también anotaciones propias como referencia (circular, solo para inspección)")
    a = ap.parse_args()

    from runout_localidad import point_value

    puntos = json.load(open(a.puntos))
    if a.lugar:
        puntos = {k: v for k, v in puntos.items() if k == a.lugar}
    ciclos = cycles_disponibles(a.workdir)
    print(f"ciclos archivados: {len(ciclos)} ({ciclos[0]} … {ciclos[-1]})\n")

    for nombre, info in puntos.items():
        lon, lat, dom = info["lon"], info["lat"], info["domain"]
        evs = eventos_del_registro(nombre, incluir_analisis=a.incluir_analisis)
        ev_t, ev_r = (evs[0] if evs else (None, None))

        print(f"══ {nombre}")
        if ev_r:
            print(f"   evento de referencia: {ev_r['que'][:70]}")
            print(f"   hora anotada: {ev_t:%d-%b %H:%M}Z  (fuente: {ev_r.get('fuente','?')})")
        else:
            print("   sin evento anotado en el registro — solo se lista lo pronosticado")

        print(f"   {'ciclo':16s} {'generado':>13s} {'mm':>8s} {'cruce previsto':>20s} {'antic.':>9s}")
        primera = None
        for c in ciclos:
            d = os.path.join(a.workdir, c, dom)
            fj = os.path.join(d, "forecast.json")
            rt = os.path.join(d, "rain_liquid_total.tif")
            ft = os.path.join(d, f"first_crossing_fhour_{a.variante}.tif")
            if not (os.path.exists(fj) and os.path.exists(rt)):
                continue
            gen = json.load(open(fj)).get("generated_utc", "")
            mm = point_value(rt, lon, lat)
            v = point_value(ft, lon, lat) if os.path.exists(ft) else float("nan")
            try:
                t0 = datetime.strptime(c[:-1], "%Y%m%dT%H")
                gendt = datetime.strptime(gen, "%Y-%m-%dT%H:%MZ")
            except ValueError:
                continue
            if v == v and v > 0:
                cruce = t0 + timedelta(hours=float(v))
                cruce_s = f"{cruce - timedelta(hours=4):%d-%b %H:%M}"
                if primera is None:
                    primera = (c, gendt, cruce)
            else:
                cruce_s = "sin cruce"
            antic = f"{(ev_t - gendt).total_seconds()/3600:+8.1f}h" if ev_t else "—"
            print(f"   {c:16s} {gen[-6:]:>13s} {mm:7.1f} {cruce_s:>20s} {antic:>9s}")

        if primera and ev_t:
            c, gendt, cruce = primera
            print(f"   → señal disponible desde {c} (generado {gendt:%d-%b %H:%M}Z): "
                  f"{(ev_t-gendt).total_seconds()/3600:.1f} h antes de la hora anotada")
            print(f"   → cruce previsto {cruce-timedelta(hours=4):%d-%b %H:%M} local vs evento "
                  f"{ev_t-timedelta(hours=4):%d-%b %H:%M} local "
                  f"({(cruce-ev_t).total_seconds()/3600:+.1f} h)")
        print()

    print("CÓMO LEER: la anticipación es contra la hora en que la AUTORIDAD actuó,")
    print("que va DESPUÉS del hecho físico. Es una cota inferior de lo tarde que")
    print("llegó el modelo, no una medida de cuánto se adelantó al aluvión.")


if __name__ == "__main__":
    main()
