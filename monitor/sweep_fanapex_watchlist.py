#!/usr/bin/env python3
"""Barrido: clase por CENTROIDE (melton_localidad, lo de hoy) vs FAN-APEX
(fan_apex_localidad, la propuesta) sobre toda la watchlist vigente.

Mide cuántas comunas cambian de clase y con qué runout-a-alcanzar. Las comunas
en cuencas transfer (17_costa_transfer, 16_araucania_transfer, etc.) no tienen
hidrología propia → ambos métodos quedan 'sin hidrología' (hueco costa sin
modelo ya documentado); se cuentan aparte, no se fuerzan.

Uso:
    python3 sweep_fanapex_watchlist.py [--watchlist <ruta>] [--out <json>]
    python3 sweep_fanapex_watchlist.py --hydro-source kingston   # más rápido
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from melton_localidad import _hydro_dir, classify_point   # noqa: E402
import fan_apex_localidad as FA                            # noqa: E402

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")


def basin_of(name):
    """Cuenca KINGSTON de la comuna (de su runout) y si tiene hidrología."""
    rp = os.path.join(RUNOUTS, name, "resumen.json")
    if not os.path.exists(rp):
        return None, None, None
    susc = json.load(open(rp)).get("susceptibility")
    if not susc:
        return None, None, None
    basin, hydro = _hydro_dir(susc)
    return basin, hydro, susc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist",
                    default=os.path.expanduser("~/nowcast-forecast/latest/watchlist.json"))
    ap.add_argument("--hydro-source", choices=["surtgis", "kingston"], default="surtgis")
    ap.add_argument("--out")
    a = ap.parse_args()

    wl = json.load(open(os.path.realpath(a.watchlist)))["selected"]
    rows, changed, no_hydro = [], 0, 0
    print(f"{'comuna':22s} {'cuenca':16s} {'centroide':16s} {'fan-apex':16s} {'runout':>8s} {'banda'}")
    for s in sorted(wl, key=lambda x: x.get("comuna", "")):
        name, comuna = s["name"], s.get("comuna", "")
        lon, lat = s["lon"], s["lat"]
        basin, hydro, susc = basin_of(name)
        if basin is None or hydro is None:
            no_hydro += 1
            print(f"{comuna[:21]:22s} {(basin or '?')[:15]:16s} {'sin hidrología':16s} "
                  f"{'sin hidrología':16s} {'—':>8s}")
            rows.append({"comuna": comuna, "basin": basin, "centroide": None,
                         "fan_apex": None, "nota": "sin hidrología (transfer)"})
            continue
        # centroide (hoy)
        cen = classify_point(susc, lon, lat)
        cen_clase = cen.get("clase", "?")
        # fan-apex (propuesta)
        try:
            fa = FA.classify(comuna, basin, lon, lat, hydro_source=a.hydro_source)
        except (Exception, SystemExit) as e:
            # SystemExit explícito por lo mismo que en fanapex_localidades.py y
            # melton_localidad.py: un sys.exit() de librería río abajo abortaría
            # el barrido completo en vez de degradar una comuna.
            fa = {"error": str(e)[:60]}
        if "error" in fa:
            fa_clase, ro, banda = f"ERR", None, ""
        else:
            fa_clase = fa["clase_comuna"]
            ro = fa.get("runout_a_alcanzar_km")
            banda = fa.get("banda", "")
        # ¿cambió? (centroide 'crecida' → fan-apex reconoce aluvión con runout creíble)
        cambio = (cen_clase != fa_clase.split(" /")[0] and ro is not None and ro <= 8.0)
        if cambio:
            changed += 1
        print(f"{comuna[:21]:22s} {basin[:15]:16s} {cen_clase[:15]:16s} "
              f"{fa_clase[:15]:16s} {(f'{ro:.1f}km' if ro is not None else '—'):>8s} {banda}"
              + ("  ← CAMBIA" if cambio else ""))
        rows.append({"comuna": comuna, "basin": basin, "lon": lon, "lat": lat,
                     "centroide": cen_clase, "centroide_melton": cen.get("melton"),
                     "fan_apex": fa_clase, "runout_a_alcanzar_km": ro, "banda": banda,
                     "cambia": cambio})

    con_hydro = len(wl) - no_hydro
    print(f"\nResumen: {len(wl)} comunas · {con_hydro} con hidrología · {no_hydro} transfer "
          f"(sin modelo) · {changed} cambian de clase (centroide→fan-apex, runout ≤8 km)")
    if a.out:
        json.dump({"watchlist": os.path.realpath(a.watchlist), "hydro_source": a.hydro_source,
                   "n_total": len(wl), "n_con_hidrologia": con_hydro, "n_transfer": no_hydro,
                   "n_cambian": changed, "rows": rows},
                  open(a.out, "w"), indent=1, ensure_ascii=False)
        print(f"→ {a.out}")


if __name__ == "__main__":
    main()
