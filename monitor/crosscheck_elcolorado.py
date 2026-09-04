#!/usr/bin/env python3
"""Cruza la estación El Colorado (2750 m) entre sus DOS RUTAS DE ENTREGA.

El monitor consume esa estación por la API de la DMC (código 330077), con token,
y el acumulador `agromet-scraper` la recoge en paralelo desde agrometeorologia.cl.

QUÉ SON EXACTAMENTE LAS DOS FUENTES (corregido 2026-09-02). Al principio esto se
escribió como si fueran dos fuentes independientes. No lo son: en agromet la
estación figura con `institucion='DMC'` y con prefijo `EXT-`, que es como ese
acumulador marca las estaciones REPUBLICADAS de terceros (DMC, SERVIMET, CEAZA…)
frente a su red propia `INIA-`. O sea es el MISMO sensor y la MISMA cadena de la
DMC, servido por dos caminos distintos. Eso acota lo que el cruce puede probar:

  ✓ valida la RUTA DE ENTREGA. Si coinciden, agromet es un respaldo utilizable
    cuando la API se cae o el token vence — que es el modo de falla realista.
  ✗ NO valida el SENSOR. Un pluviómetro tapado o descalibrado se ve idéntico por
    los dos caminos. Para eso hace falta una fuente de verdad independiente
    (satélite IMERG/GOES, o una estación vecina de otra institución).

Por eso el acuerdo esperado no es "parecido dentro de tolerancia" sino
prácticamente exacto; una diferencia grande acusa a la ruta, no al clima.

Método. La API de la DMC entrega ~12 h y es minutaria en UTC; agromet guarda
totales horarios. Se agrega el minutario a horas y se comparan las horas comunes.
La temperatura se usa además como RELOJ: su ciclo diurno permite estimar el
desfase horario entre las dos fuentes por correlación cruzada, en vez de asumir la
zona horaria de agromet, que no está documentada. Un desfase distinto de cero no
es un error de nadie: es una convención que hay que conocer antes de fusionar.

Uso:
    python3 monitor/crosscheck_elcolorado.py                    # API, ventana ~12 h
    python3 monitor/crosscheck_elcolorado.py --horas 12
    # Un evento pasado solo se puede mirar si estaba archivado (ver archive_ema.py):
    python3 monitor/crosscheck_elcolorado.py --desde 2026-09-02 --hasta 2026-09-02
Sale 0 si las fuentes son consistentes, 1 si divergen o falta información.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from math import cos, hypot, radians
from pathlib import Path

# El acceso a la EMA vive en archive_ema (capa de datos) y acá solo se consume.
# Tener dos copias del fetch era exactamente cómo nacen las hermanas divergentes
# que este repo ya persiguió seis veces.
from archive_ema import (credentials, fetch_ema_raw, parse_records,
                         read_archive)

DMC_CODE = "330077"      # El Colorado en la red EMA de la DMC
AGROMET_DB = Path.home() / "proyectos/agromet-scraper/data/agromet.db"
MONITOR = Path(__file__).resolve().parent

# La estación se busca por DÓNDE ESTÁ, no por su id. El id de agromet ya cambió
# una vez bajo nuestros pies: era "108" hasta el 2026-08-31, cuando el scraper
# descubrió que los ids numéricos colisionan entre instituciones (INIA-78 Visviri
# vs EXT-78 Portezuelo) y pasó a prefijarlos por API de origen → "EXT-108". Este
# script pedía "108", recibía cero filas y moría. Un identificador ajeno es un
# contrato que el otro repo puede renegociar; las coordenadas del cerro, no.
EL_COLORADO_LONLAT = (-70.29361, -33.35)
MATCH_RADIUS_KM = 1.0

# Tolerancias: por debajo de esto las fuentes se consideran de acuerdo.
TOL_TA_C = 1.0     # °C — sensores y promediado distinto justifican décimas
TOL_PP_MM = 0.3    # mm — resolución típica de un pluviómetro de balancín


def load_dmc(desde=None, hasta=None):
    """Minutario de la DMC → ({datetime: {...}}, origen).

    Sin rango va a la API, que es lo que sirve para la operación del día. Con
    rango lee el ARCHIVO local: es la única manera de mirar un evento pasado,
    porque la API solo recuerda ~12 h.
    """
    if desde or hasta:
        recs = read_archive(DMC_CODE, desde, hasta)
        if not recs:
            raise SystemExit(
                f"no hay nada archivado para {DMC_CODE} en el rango pedido.\n"
                "El archivo se llena con `monitor/archive_ema.py`, que hay que tener "
                "corriendo ANTES del evento:\n"
                "la API de la DMC no sirve historia, así que un evento no archivado "
                "está perdido para siempre.")
        return parse_records(recs), "archivo local"
    usuario, token = credentials()
    if not usuario or not token:
        raise SystemExit("faltan DMC_USUARIO/DMC_TOKEN (monitor/config.local.env o entorno)")
    return parse_records(fetch_ema_raw(usuario, token, DMC_CODE)), "API DMC (ventana ~12 h)"


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


def resolve_agromet_station(con):
    """Encuentra El Colorado en agromet por CERCANÍA, no por id → (id, nombre, inst).

    Falla ruidosamente si no hay nada dentro del radio: mejor un error que dice
    "cambió el catálogo río arriba" que una comparación vacía que parece un
    problema de clima.
    """
    lon0, lat0 = EL_COLORADO_LONLAT
    km_per_deg_lat = 111.32
    km_per_deg_lon = km_per_deg_lat * cos(radians(lat0))
    best = None
    q = ("SELECT id, nombre, institucion, longitud, latitud FROM estaciones "
         "WHERE longitud IS NOT NULL AND latitud IS NOT NULL")
    for sid, nombre, inst, lon, lat in con.execute(q):
        dkm = hypot((lon - lon0) * km_per_deg_lon, (lat - lat0) * km_per_deg_lat)
        if best is None or dkm < best[0]:
            best = (dkm, sid, nombre, inst)
    if best is None or best[0] > MATCH_RADIUS_KM:
        got = f"la más cercana quedó a {best[0]:.1f} km ({best[2]})" if best else "catálogo vacío"
        raise SystemExit(
            f"no encontré El Colorado en {AGROMET_DB} dentro de {MATCH_RADIUS_KM} km "
            f"de ({lon0}, {lat0}): {got}.\n"
            "Puede que el scraper haya cambiado el catálogo de estaciones o que la "
            "estación saliera de línea. Revisar ~/proyectos/agromet-scraper.")
    dkm, sid, nombre, inst = best
    return sid, nombre, inst, dkm


def fetch_agromet(con, station_id):
    """Serie horaria de agromet → {datetime: {'ta': float, 'pp': float}}."""
    out = defaultdict(dict)
    q = ("SELECT fecha, metrica, valor FROM mediciones "
         "WHERE estacion_id=? AND stack='HOUR' AND metrica IN ('TA-AVG','PP-SUM')")
    for fecha, metrica, valor in con.execute(q, (station_id,)):
        try:
            dt = datetime.strptime(fecha[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out[dt]["ta" if metrica == "TA-AVG" else "pp"] = valor
    if not out:
        raise SystemExit(
            f"la estación {station_id} existe en el catálogo de agromet pero no tiene "
            "mediciones horarias TA-AVG/PP-SUM. ¿El scraper dejó de correr?")
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


# agromet etiqueta sus horas en UTC−4 FIJO, no en hora local de Chile.
AGROMET_FIXED_OFFSET_H = 4


def check_dst(offset):
    """¿El desfase de agromet sigue el horario de verano, o es fijo?

    CORREGIDO 2026-09-04. Esto antes afirmaba que agromet guardaba hora LOCAL y
    avisaba de que el desfase se rompería en el próximo cambio de huso. Era
    falso, y falso de la manera más fácil de creer: el hallazgo se hizo en
    agosto, cuando Chile está en UTC−4 y el offset fijo COINCIDE con el local.

    Dos evidencias, ambas sobre la base histórica y no sobre la teoría:

      1. En las 18 transiciones de huso entre 2018 y 2026 la estación tiene 24
         registros por día, siempre. Con hora local habría un día de 23 h en
         cada adelanto y uno de 25 h (o un colapso de PK) en cada atraso.
      2. La hora etiquetada del máximo diario de temperatura es 13:00 todos los
         meses del año. Con hora local se correría a 14:00 en los meses con
         horario de verano; el sol no cambia de horario.

    Consecuencia práctica: el desfase agromet↔DMC es constante (+4 h) y NO hay
    que hacer nada en los cambios de huso. Lo que sí hay que vigilar es que
    agromet no cambie de convención sin avisar, que es lo que esta función mide
    ahora.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return
    tz = ZoneInfo("America/Santiago")
    now = datetime.now()
    local = int(now.replace(tzinfo=tz).utcoffset().total_seconds() // 3600)

    if offset == AGROMET_FIXED_OFFSET_H:
        print(f"    es el offset FIJO esperado (UTC−{AGROMET_FIXED_OFFSET_H}). agromet NO "
              f"sigue el horario de verano:")
        print("      · 24 registros/día en las 18 transiciones de huso 2018-2026;")
        print("      · máximo diario de TA a las 13:00 todo el año, sin corrimiento estival.")
        if local != -AGROMET_FIXED_OFFSET_H:
            print(f"      (hoy Chile está en UTC{local:+d}, así que el desfase NO coincide "
                  f"con la hora local — es lo esperado, no un error.)")
        return

    print(f"    ⚠ el desfase es {offset:+d} h y se esperaba "
          f"+{AGROMET_FIXED_OFFSET_H} (UTC−{AGROMET_FIXED_OFFSET_H} fijo).")
    if offset == -local:
        print(f"      Coincide con la hora local de Chile (UTC{local:+d}): agromet pudo "
              f"haber CAMBIADO de convención\n      a hora local. Verificar contando "
              f"registros por día en la próxima transición de huso.")
    else:
        print("      No coincide ni con el offset fijo ni con la hora local — revisar "
              "antes de fusionar.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horas", type=int, default=12, help="ventana a comparar")
    ap.add_argument("--desde", help="'YYYY-MM-DD[ HH:MM]' UTC — lee del archivo local")
    ap.add_argument("--hasta", help="'YYYY-MM-DD[ HH:MM]' UTC — lee del archivo local")
    args = ap.parse_args()

    if not AGROMET_DB.exists():
        raise SystemExit(f"no existe la base de agromet: {AGROMET_DB}")
    con = sqlite3.connect(f"file:{AGROMET_DB}?mode=ro", uri=True)
    sid, nombre, inst, dkm = resolve_agromet_station(con)
    agro = fetch_agromet(con, sid)
    con.close()

    print(f"El Colorado 2750 m — DMC {DMC_CODE} vs agromet «{nombre}» [{sid}]\n")
    print(f"  agromet resolvió la estación por coordenadas: a {dkm*1000:.0f} m del "
          f"punto esperado, institución «{inst}».")
    if inst and "DMC" in inst.upper():
        print("    → OJO: es la MISMA estación de la DMC, republicada. El cruce valida "
              "la ruta de entrega,\n       no el sensor. Se espera coincidencia casi "
              "exacta, no solo 'dentro de tolerancia'.")

    dmc_min, origen = load_dmc(args.desde, args.hasta)
    if not dmc_min:
        raise SystemExit(f"{origen}: no devolvió registros fechados.")
    dmc_h = dmc_hourly(dmc_min)
    print(f"\n  origen DMC: {origen}")
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
    pp_win_a = pp_win_d = 0.0     # lluvia DENTRO de la ventana comparada
    # Solo las horas de agromet que solapan con la ventana DMC: agromet trae años
    # de serie y tomar "las últimas N" no funciona cuando se mira un evento pasado.
    lo = min(dmc_h) - timedelta(hours=offset)
    hi = max(dmc_h) - timedelta(hours=offset)
    for h in [x for x in sorted(agro) if lo <= x <= hi][-args.horas * 2:]:
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
        pp_win_a += pp_a or 0.0
        pp_win_d += pp_d or 0.0
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
        print(f"  PP — dif máx {mx:.2f} mm (tolerancia {TOL_PP_MM}); acumulado EN LA "
              f"VENTANA COMPARADA: agromet {pp_win_a:.1f} mm / DMC {pp_win_d:.1f} mm")
        if pp_win_a == 0.0 and pp_win_d == 0.0:
            print("    AVISO: sin lluvia en la ventana común. La coincidencia en PP es "
                  "de ceros y no\n    dice nada de la entrega de precipitación; repetir "
                  "tras un evento.\n    OJO: la API de la DMC solo sirve ~12 h, así que "
                  "hay que correrlo DURANTE la\n    lluvia o pocas horas después — días "
                  "más tarde la ventana ya no la contiene.")
        ok &= mx <= TOL_PP_MM

    print("\n  " + ("RUTAS CONSISTENTES — agromet sirve de respaldo de ENTREGA de esta "
                    "estación\n  (si cae la API o vence el token), no de verificación del "
                    "sensor."
                    if ok else
                    "DIVERGEN — revisar antes de confiar en la fuente que usa el monitor."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
