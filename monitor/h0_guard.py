#!/usr/bin/env python3
"""¿Puede este ciclo del monitor reportar la isoterma cero (H0) en sombra?

`nowcast run --h0-csv` reporta las señales de H0 junto a cada paso SIN tocar el
peligro (modo sombra; ver el crate nowcast-h0: fusionarlo por producto hundía
~88 % de las alertas de invierno). Este guard decide si pasarle esos flags al
motor, y la regla es que H0 NUNCA puede tumbar el ciclo: es un diagnóstico, y
abortar por él apagaría las alertas de lluvia. Si algo no cuadra, se apaga H0,
se dice por qué y la lluvia sigue igual.

Se apaga cuando:
  · H0_CSV no existe o no tiene días utilizables;
  · el dominio (BBOX) no cae entero en 30–36 °S, el único rango donde la serie
    tiene gradiente vertical para ajustarse (Atacama y Aysén necesitan otro);
  · la serie está VIEJA: su último día es anterior a hoy − H0_STALE_DAYS. Hoy
    esto no es teórico — h0_centro_diaria_v3.csv termina el 2026-08-31 porque
    nada la regenera a diario, y sin esta guarda el monitor reportaría
    "missing" todos los días sin que nadie supiera por qué.

Salida (una línea por stdout):
    ok <YYYY-MM-DDTHH:MM>     hora UTC del paso 0, para --start
    off <motivo>

Uso:
    python3 monitor/h0_guard.py --csv H0.csv --bbox "lon0,lat0,lon1,lat1" \\
        --fusion-json state/fusion.json [--stale-days 3]
    python3 monitor/h0_guard.py --selftest
"""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LAT_MIN, LAT_MAX = -36.0, -30.0  # cobertura de la serie H0 (30–36 °S)
STAMP_FMT = "%Y%m%dT%H%M"         # claves de paso del monitor (window_meta.py)


def last_day(csv_path):
    """Último día con h0_m utilizable. Columnas POR NOMBRE, no por posición: el
    repo de río arriba puede agregar o reordenar columnas (la lección del id de
    agromet que cambió bajo nuestros pies)."""
    ultimo = None
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                h0 = float(row.get("h0_m") or "")
                d = date.fromisoformat((row.get("fecha") or "").strip()[:10])
            except ValueError:
                continue  # hueco o fila mala: no cuenta como día utilizable
            if h0 == h0 and (ultimo is None or d > ultimo):  # h0 == h0 descarta NaN
                ultimo = d
    return ultimo


def in_domain(bbox):
    lon0, lat0, lon1, lat1 = (float(x) for x in bbox.split(","))
    lo, hi = min(lat0, lat1), max(lat0, lat1)
    return LAT_MIN <= lo and hi <= LAT_MAX, (lo, hi)


def decide(csv_path, bbox, fusion_json, stale_days, today=None):
    today = today or datetime.now(timezone.utc).date()
    if not csv_path:
        return "off", "H0_CSV vacío"
    p = Path(csv_path)
    if not p.is_file():
        return "off", f"H0_CSV no existe ({csv_path})"
    try:
        ok, (lo, hi) = in_domain(bbox)
    except ValueError:
        return "off", f"BBOX ilegible ({bbox!r})"
    if not ok:
        return "off", (f"dominio fuera de 30–36 °S (lat {lo}..{hi}); la serie H0 "
                       "no vale ahí")
    ultimo = last_day(p)
    if ultimo is None:
        return "off", f"H0_CSV sin días utilizables ({csv_path})"
    if ultimo < today - timedelta(days=stale_days):
        return "off", (f"serie H0 vieja: último día {ultimo}, hoy {today} "
                       f"(tolerancia {stale_days} d) — nada la está regenerando")
    try:
        span0 = json.load(open(fusion_json))["span"][0]
        t0 = datetime.strptime(str(span0), STAMP_FMT)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        return "off", f"sin hora del paso 0 en {fusion_json} ({e.__class__.__name__})"
    return "ok", t0.strftime("%Y-%m-%dT%H:%M")


def selftest():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    serie = tmp / "h0.csv"
    # columnas en otro orden y una fila hueca al final: no debe contar
    serie.write_text("tormenta,h0_m,fecha\n1,3600,2026-09-20\n0,3500,2026-09-23\n1,,2026-09-24\n")
    fusion = tmp / "fusion.json"
    fusion.write_text(json.dumps({"span": ["20260925T1400", "20260927T1930"]}))
    rm = "-70.75,-34.25,-69.75,-33.25"
    hoy = date(2026, 9, 25)

    assert last_day(serie) == date(2026, 9, 23), last_day(serie)
    assert decide(str(serie), rm, str(fusion), 3, hoy) == ("ok", "2026-09-25T14:00")
    # vieja: 23-sep con tolerancia 1 d el 25-sep
    est, why = decide(str(serie), rm, str(fusion), 1, hoy)
    assert est == "off" and "vieja" in why, why
    # Atacama (27 °S) queda fuera del rango de la serie
    est, why = decide(str(serie), "-71.3,-29.7,-69.1,-25.9", str(fusion), 3, hoy)
    assert est == "off" and "30–36" in why, why
    # un bbox que cruza el borde tampoco pasa (-36.5 < -36)
    assert decide(str(serie), "-71,-36.5,-70,-35", str(fusion), 3, hoy)[0] == "off"
    for malo in ["", str(tmp / "no.csv")]:
        assert decide(malo, rm, str(fusion), 3, hoy)[0] == "off"
    assert decide(str(serie), "basura", str(fusion), 3, hoy)[0] == "off"
    assert decide(str(serie), rm, str(tmp / "no.json"), 3, hoy)[0] == "off"
    vacia = tmp / "vacia.csv"
    vacia.write_text("fecha,h0_m\n2026-09-24,\n2026-09-25,nan\n")
    assert decide(str(vacia), rm, str(fusion), 3, hoy)[0] == "off"
    print("[h0_guard] selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", default="")
    ap.add_argument("--bbox", default="")
    ap.add_argument("--fusion-json", default="")
    ap.add_argument("--stale-days", type=int, default=3)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    estado, detalle = decide(a.csv, a.bbox, a.fusion_json, a.stale_days)
    print(estado, detalle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
