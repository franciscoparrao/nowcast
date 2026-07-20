#!/usr/bin/env python3
"""Registro de verificación: pronóstico vs. observación vs. lo que pasó.

POR QUÉ EXISTE (2026-07-19). Durante el evento del Huasco el sistema falló de
una manera que ninguna auditoría de código habría detectado: la columna de
acuerdo entre modelos apuntaba al revés —Freirina, descontada porque ECMWF
daba 25-32 mm, es donde se activaron las quebradas; Alto del Carmen,
descontado por lo mismo, es donde más llovió (66 mm en El Corral)— y nos
enteramos POR LA PRENSA, no por el sistema. La corrección de fondo es
ponderar por skill histórico en vez de por consenso, y eso es imposible sin
un registro: no se puede ponderar por un skill que nunca se anotó.

El dato además se degrada rápido. Los reportes de estación de la DMC/DGA de
las horas del evento son mucho más difíciles de reconstruir una semana
después, y las notas de prensa se editan o desaparecen. Por eso esto es
append-only y guarda la PROCEDENCIA de cada registro (URL, código de
estación, o "manual"): un registro sin procedencia no sirve para calibrar.

Tres tipos de registro, deliberadamente separados:
  pronostico  lo que cada modelo dijo, por ciclo y punto (automático)
  observacion lo que se midió (estación, o reporte con su URL)
  evento      lo que PASÓ (evacuación, activación de quebrada, aluvión)

La separación importa: un pronóstico acertado de lluvia no implica un
pronóstico acertado de flujo. Hoy tenemos verificada la lluvia y NO el
footprint del ABM, y el registro tiene que poder decir eso.

Uso:
    python3 verif_log.py pronostico --puntos puntos.json [--ciclo latest]
    python3 verif_log.py observacion --punto freirina --mm 55.9 \
        --hasta 2026-07-19T12:00Z --fuente "Puente Nicolasa (DGA)" --url ...
    python3 verif_log.py evento --lugar freirina --que "activación quebrada" \
        --cuando 2026-07-19T15:47Z --url ...
    python3 verif_log.py resumen [--desde 2026-07-01]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REGISTRO = os.path.expanduser("~/nowcast-forecast/verificacion/registro.jsonl")


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def anotar(rec):
    """Append-only: la historia NO se reescribe, para que el registro sea
    auditable. Un registro equivocado se corrige anotando otro que lo
    corrija, no borrando el anterior."""
    os.makedirs(os.path.dirname(REGISTRO), exist_ok=True)
    rec["registrado_utc"] = _ahora()
    with open(REGISTRO, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def leer():
    if not os.path.exists(REGISTRO):
        return []
    out = []
    for i, ln in enumerate(open(REGISTRO), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            print(f"[verif] línea {i} ilegible, se omite", file=sys.stderr)
    return out


# ── pronóstico ────────────────────────────────────────────────────────────
def cmd_pronostico(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from runout_localidad import point_value
    import risk_map_localidad as R

    base = os.path.realpath(os.path.expanduser(a.forecast))
    ciclo = os.path.basename(base)
    puntos = json.load(open(a.puntos))
    n = 0
    for nombre, info in puntos.items():
        lon, lat, dom = info["lon"], info["lat"], info["domain"]
        rec = {"tipo": "pronostico", "ciclo": ciclo, "punto": nombre,
               "lon": lon, "lat": lat, "dominio": dom, "modelos": {}}
        for sub, lbl in (("", "gfs"), ("ecmwf", "ecmwf"), ("icon", "icon")):
            p = os.path.join(base, dom, sub, "rain_liquid_total.tif")
            if not os.path.exists(p):
                continue
            v = point_value(p, lon, lat)
            rec["modelos"][lbl] = {
                "lluvia_total_mm": round(float(v), 1) if v == v else None,
                "ventana": R.ventana_de_fuente(os.path.join(base, dom), sub),
            }
        # peligro/excedencia del producto ya calculado, si existe
        kp = os.path.expanduser(f"~/nowcast-forecast/risk_maps/{nombre}/riesgo.json")
        if os.path.exists(kp):
            k = json.load(open(kp))
            rec["peligro_no_saturado"] = k.get("peligro_no_saturado")
            rec["exceedancia_id"] = (k.get("exceedancia_id") or {}).get("exceedancia")
            ac = k.get("acuerdo_modelos") or {}
            rec["acuerdo"] = ac.get("acuerdo") or ac.get("confianza")
        anotar(rec)
        n += 1
    print(f"anotados {n} pronósticos del ciclo {ciclo} → {REGISTRO}")


# ── observación ───────────────────────────────────────────────────────────
def cmd_observacion(a):
    if not (a.fuente or a.url):
        sys.exit("una observación SIN procedencia no sirve para calibrar: "
                 "indicar --fuente y/o --url")
    anotar({"tipo": "observacion", "punto": a.punto, "lluvia_mm": a.mm,
            "acumulado_hasta": a.hasta, "desde": a.desde,
            "fuente": a.fuente, "url": a.url, "nota": a.nota})
    print(f"observación anotada: {a.punto} {a.mm} mm ({a.fuente or a.url})")


# ── evento ────────────────────────────────────────────────────────────────
def cmd_evento(a):
    if not (a.fuente or a.url):
        sys.exit("un evento SIN procedencia no es evidencia: indicar --fuente y/o --url")
    anotar({"tipo": "evento", "lugar": a.lugar, "que": a.que, "cuando": a.cuando,
            "fuente": a.fuente, "url": a.url, "nota": a.nota})
    print(f"evento anotado: {a.lugar} — {a.que}")


# ── resumen ───────────────────────────────────────────────────────────────
def cmd_resumen(a):
    regs = leer()
    if not regs:
        sys.exit("registro vacío")
    pron = [r for r in regs if r["tipo"] == "pronostico"]
    obs = [r for r in regs if r["tipo"] == "observacion"]
    evs = [r for r in regs if r["tipo"] == "evento"]
    print(f"registro: {len(pron)} pronósticos · {len(obs)} observaciones · {len(evs)} eventos")

    # error por modelo donde haya observación para el mismo punto
    por_punto = {}
    for o in obs:
        por_punto.setdefault(o["punto"], []).append(o)
    if not por_punto:
        print("\nsin observaciones todavía: no se puede calcular skill")
        return
    print(f"\n{'punto':18s} {'obs':>7s} {'GFS':>8s} {'ECMWF':>8s}   (mm)")
    resumen_modelo = {}
    for punto, olist in sorted(por_punto.items()):
        omax = max(o["lluvia_mm"] for o in olist if o.get("lluvia_mm") is not None)
        cands = [p for p in pron if p["punto"] == punto]
        if not cands:
            print(f"{punto:18s} {omax:7.1f}   (sin pronóstico registrado)")
            continue
        ult = cands[-1]
        fila = f"{punto:18s} {omax:7.1f}"
        for m in ("gfs", "ecmwf"):
            v = (ult["modelos"].get(m) or {}).get("lluvia_total_mm")
            fila += f" {v:8.1f}" if v is not None else f" {'—':>8s}"
            if v is not None:
                resumen_modelo.setdefault(m, []).append(v - omax)
        print(fila)
    print("\nsesgo medio (pronosticado − observado), + = sobreestima:")
    for m, difs in resumen_modelo.items():
        print(f"  {m:6s} {sum(difs)/len(difs):+7.1f} mm  (n={len(difs)})")
    print("\nOJO: el pronóstico es del total de su ventana y la observación suele ser "
          "parcial (evento en curso), así que un sesgo positivo puede no ser error. "
          "Comparar solo cuando el evento haya cerrado.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pronostico", help="anota lo que dijo cada modelo (automático)")
    p.add_argument("--puntos", required=True, help='JSON {"nombre": {"lon":..,"lat":..,"domain":..}}')
    p.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    p.set_defaults(func=cmd_pronostico)

    p = sub.add_parser("observacion", help="anota lluvia medida")
    p.add_argument("--punto", required=True)
    p.add_argument("--mm", type=float, required=True)
    p.add_argument("--hasta", help="UTC hasta la que acumula, ISO")
    p.add_argument("--desde", help="UTC desde la que acumula, ISO")
    p.add_argument("--fuente", help="estación o institución")
    p.add_argument("--url")
    p.add_argument("--nota")
    p.set_defaults(func=cmd_observacion)

    p = sub.add_parser("evento", help="anota lo que PASÓ en terreno")
    p.add_argument("--lugar", required=True)
    p.add_argument("--que", required=True)
    p.add_argument("--cuando", help="UTC del hecho, ISO")
    p.add_argument("--fuente")
    p.add_argument("--url")
    p.add_argument("--nota")
    p.set_defaults(func=cmd_evento)

    p = sub.add_parser("resumen", help="skill acumulado")
    p.add_argument("--desde")
    p.set_defaults(func=cmd_resumen)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
