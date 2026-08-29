#!/usr/bin/env python3
"""Cruza la estación El Colorado (2750 m) entre sus DOS fuentes independientes.

El monitor consume esa estación por la API de la DMC (código 330077), con token,
y el acumulador `agromet-scraper` la scrapea en paralelo desde agrometeorologia.cl
(INIA, id 108). Son el mismo sensor físico: mismas coordenadas (−70.294, −33.350)
y misma cota. Que dos rutas de datos coincidan no es obvio, y saberlo sirve para
dos cosas concretas:

  1. Si coinciden, agromet es un RESPALDO local de la única estación de cordillera
     del dominio piloto, sin depender de token ni de que la API responda.
  2. Si discrepan, hay un problema de datos en la fuente que el monitor usa HOY,
     y conviene enterarse por un cruce y no por una alerta rara.

Método. La API de la DMC entrega ~12 h y es minutaria en UTC; agromet guarda
totales horarios. Se agrega el minutario a horas y se comparan las horas comunes.
La temperatura se usa además como RELOJ: su ciclo diurno permite estimar el
desfase horario entre las dos fuentes por correlación cruzada, en vez de asumir la
zona horaria de agromet, que no está documentada. Un desfase distinto de cero no
es un error de nadie: es una convención que hay que conocer antes de fusionar.

Uso:
    python3 monitor/crosscheck_elcolorado.py                    # usa config.local.env
    python3 monitor/crosscheck_elcolorado.py --horas 12
Sale 0 si las fuentes son consistentes, 1 si divergen o falta información.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

API = "https://climatologia.meteochile.gob.cl/application/servicios/getDatosRecientesEma"
DMC_CODE = "330077"      # El Colorado en la red EMA de la DMC
AGROMET_ID = "108"       # El Colorado en agrometeorologia.cl (INIA)
AGROMET_DB = Path.home() / "proyectos/agromet-scraper/data/agromet.db"
MONITOR = Path(__file__).resolve().parent

# Tolerancias: por debajo de esto las fuentes se consideran de acuerdo.
TOL_TA_C = 1.0     # °C — sensores y promediado distinto justifican décimas
TOL_PP_MM = 0.3    # mm — resolución típica de un pluviómetro de balancín


def load_local_env():
    """Lee monitor/config.local.env sin exportar nada al entorno global."""
    creds = {}
    path = MONITOR / "config.local.env"
    if not path.exists():
        return creds
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds


def fetch_dmc(usuario, token):
    """Serie reciente de la DMC → {datetime UTC: {'ta': float, 'pp': float}}."""
    q = urllib.parse.urlencode({"usuario": usuario, "token": token})
    with urllib.request.urlopen(f"{API}/{DMC_CODE}?{q}", timeout=60) as r:
        payload = json.load(r)
    if "bloqueda" in json.dumps(payload):
        raise SystemExit(f"la API rechazó las credenciales: {payload.get('mensaje', payload)}")

    recs = payload.get("datosEstaciones", {}).get("datos", [])
    if not recs:
        raise SystemExit(f"sin registros para {DMC_CODE}; claves: {list(payload)[:8]}")

    def num(raw):
        if raw is None:
            return None
        s = str(raw).split()[0].replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    out = {}
    for rec in recs:
        stamp = rec.get("momento") or rec.get("fecha") or rec.get("momentoUTC")
        if not stamp:
            continue
        txt = str(stamp).replace("T", " ")[:16]
        try:
            dt = datetime.strptime(txt, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out[dt] = {"ta": num(rec.get("temperatura")),
                   "pp": num(rec.get("aguaCaidaDelMinuto"))}
    return out


def dmc_hourly(minutely):
    """Minutario → horario: PP se suma, TA se promedia. Clave: hora truncada."""
    acc = defaultdict(lambda: {"pp": 0.0, "ta": [], "n": 0})
    for dt, v in minutely.items():
        h = dt.replace(minute=0, second=0, microsecond=0)
        if v["pp"] is not None:
            acc[h]["pp"] += v["pp"]
        if v["ta"] is not None:
            acc[h]["ta"].append(v["ta"])
        acc[h]["n"] += 1
    return {h: {"pp": a["pp"],
                "ta": (sum(a["ta"]) / len(a["ta"])) if a["ta"] else None,
                "n": a["n"]}
            for h, a in acc.items()}


def fetch_agromet():
    """Serie horaria de agromet → {datetime: {'ta': float, 'pp': float}}."""
    if not AGROMET_DB.exists():
        raise SystemExit(f"no existe la base de agromet: {AGROMET_DB}")
    con = sqlite3.connect(f"file:{AGROMET_DB}?mode=ro", uri=True)
    out = defaultdict(dict)
    q = ("SELECT fecha, metrica, valor FROM mediciones "
         "WHERE estacion_id=? AND stack='HOUR' AND metrica IN ('TA-AVG','PP-SUM')")
    for fecha, metrica, valor in con.execute(q, (AGROMET_ID,)):
        try:
            dt = datetime.strptime(fecha[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out[dt]["ta" if metrica == "TA-AVG" else "pp"] = valor
    con.close()
    return dict(out)


def best_offset(dmc_h, agro):
    """Desfase horario que mejor alinea las TA (correlación por error medio abs).

    El ciclo diurno es un reloj natural: si una fuente está en UTC y la otra en
    hora local, el mínimo del error cae en el desfase correspondiente.
    """
    best = None
    for off in range(-6, 7):
        pairs = []
        for h, a in agro.items():
            if a.get("ta") is None:
                continue
            d = dmc_h.get(h + timedelta(hours=off))
            if d and d["ta"] is not None:
                pairs.append(abs(a["ta"] - d["ta"]))
        if len(pairs) >= 3:
            mae = sum(pairs) / len(pairs)
            if best is None or mae < best[1]:
                best = (off, mae, len(pairs))
    return best


def check_dst(offset):
    """¿El desfase medido es hora local de Chile? Y si lo es, ¿cuándo cambia?

    Que agromet guarde hora LOCAL y no UTC no es un detalle de estilo: Chile
    cambia de huso dos veces al año, así que un desfase fijado a mano se rompe
    solo, en silencio, la madrugada del cambio. Esto lo detecta y avisa con
    fecha, en vez de dejar una nota en un comentario que envejece.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return
    tz = ZoneInfo("America/Santiago")
    now = datetime.now()
    cur = int(now.replace(tzinfo=tz).utcoffset().total_seconds() // 3600)
    if offset != -cur:
        print(f"    (el desfase NO coincide con America/Santiago, hoy UTC{cur:+d} — "
              f"revisar: puede no ser hora local)")
        return
    print(f"    coincide con America/Santiago (hoy UTC{cur:+d}): agromet guarda hora LOCAL.")
    for d in range(1, 400):
        dt = now + timedelta(days=d)
        nxt = int(dt.replace(tzinfo=tz).utcoffset().total_seconds() // 3600)
        if nxt != cur:
            print(f"    ⚠ el {dt:%Y-%m-%d} Chile pasa a UTC{nxt:+d}: el desfase será "
                  f"{-nxt:+d} h, no {offset:+d}. Un valor fijado a mano se rompe ese día.")
            break


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horas", type=int, default=12, help="ventana a comparar")
    args = ap.parse_args()

    creds = load_local_env()
    usuario = os.environ.get("DMC_USUARIO") or creds.get("DMC_USUARIO")
    token = os.environ.get("DMC_TOKEN") or creds.get("DMC_TOKEN")
    if not usuario or not token:
        print("faltan DMC_USUARIO/DMC_TOKEN (monitor/config.local.env o entorno)")
        return 1

    print(f"El Colorado 2750 m — DMC {DMC_CODE} vs agromet/INIA {AGROMET_ID}\n")

    dmc_min = fetch_dmc(usuario, token)
    dmc_h = dmc_hourly(dmc_min)
    agro = fetch_agromet()
    print(f"  DMC     : {len(dmc_min)} registros minutarios → {len(dmc_h)} horas "
          f"({min(dmc_min):%Y-%m-%d %H:%M} .. {max(dmc_min):%Y-%m-%d %H:%M})")
    print(f"  agromet : {len(agro)} horas "
          f"({min(agro):%Y-%m-%d %H:%M} .. {max(agro):%Y-%m-%d %H:%M})")

    # --- 1. desfase horario, medido y no supuesto ---------------------------
    off = best_offset(dmc_h, agro)
    if not off:
        print("\n  sin horas comunes de TA suficientes para estimar el desfase.")
        return 1
    offset, mae, npairs = off
    print(f"\n  desfase estimado por el ciclo diurno de TA: {offset:+d} h "
          f"(MAE {mae:.2f} °C sobre {npairs} horas)")
    if offset == 0:
        print("    → ambas fuentes usan la MISMA convención horaria.")
    else:
        print(f"    → agromet va {offset:+d} h respecto de la DMC (UTC). "
              f"Hay que aplicarlo antes de fusionar.")
        check_dst(offset)

    # --- 2. comparación hora a hora en la ventana común ---------------------
    print(f"\n  {'hora (agromet)':18s} {'TA agro':>8s} {'TA DMC':>8s} {'dif':>7s} "
          f"{'PP agro':>8s} {'PP DMC':>8s} {'dif':>7s}")
    print("  " + "-" * 70)
    ta_diffs, pp_diffs, rows = [], [], 0
    for h in sorted(agro)[-args.horas * 2:]:
        d = dmc_h.get(h + timedelta(hours=offset))
        if not d:
            continue
        a = agro[h]
        ta_a, ta_d = a.get("ta"), d["ta"]
        pp_a, pp_d = a.get("pp"), d["pp"]
        dta = (ta_a - ta_d) if (ta_a is not None and ta_d is not None) else None
        dpp = (pp_a - pp_d) if (pp_a is not None and pp_d is not None) else None
        if dta is not None:
            ta_diffs.append(abs(dta))
        if dpp is not None:
            pp_diffs.append(abs(dpp))
        rows += 1
        f = lambda v, u="": f"{v:{u}}" if v is not None else "    --"
        print(f"  {h:%Y-%m-%d %H:%M}   {f(ta_a,'8.1f')} {f(ta_d,'8.1f')} "
              f"{f(dta,'7.1f')} {f(pp_a,'8.1f')} {f(pp_d,'8.1f')} {f(dpp,'7.1f')}")

    if not rows:
        print("\n  sin horas comunes: las ventanas no se solapan.")
        return 1

    # --- 3. veredicto --------------------------------------------------------
    print(f"\n  horas comparadas: {rows}")
    ok = True
    if ta_diffs:
        mx = max(ta_diffs)
        print(f"  TA — dif media {sum(ta_diffs)/len(ta_diffs):.2f} °C, máx {mx:.2f} °C "
              f"(tolerancia {TOL_TA_C})")
        ok &= mx <= TOL_TA_C
    if pp_diffs:
        mx = max(pp_diffs)
        tot_a = sum(agro[h].get("pp") or 0 for h in agro)
        print(f"  PP — dif máx {mx:.2f} mm (tolerancia {TOL_PP_MM}); "
              f"acumulado agromet en toda su serie: {tot_a:.1f} mm")
        if tot_a == 0 or not any(pp_diffs):
            print("    AVISO: sin lluvia en la ventana común. La coincidencia en PP "
                  "es de ceros y NO valida el pluviómetro; repetir tras un evento.")
        ok &= mx <= TOL_PP_MM

    print("\n  " + ("FUENTES CONSISTENTES — agromet sirve de respaldo local de esta estación."
                    if ok else
                    "DIVERGEN — revisar antes de confiar en la fuente que usa el monitor."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
