#!/usr/bin/env python3
"""Archiva el minutario de una EMA de la DMC antes de que la API lo olvide.

POR QUÉ EXISTE. `getDatosRecientesEma` sirve una ventana de ~12 h y nada más.
Eso basta para operar, pero hace imposible cualquier verificación a posteriori:
cuando uno quiere mirar un evento de lluvia, ya pasó y la API no lo tiene. El
2026-09-02 nos topamos con la pared al querer cruzar El Colorado contra agromet
sobre la lluvia del 31-ago: la lluvia existía en agromet, pero la ventana de la
DMC ya iba en el 2-sep y solo había ceros que comparar.

Este archivador corre seguido, guarda lo que la API muestra en ese momento y
deja de tirar los datos. A partir de ahí el cruce se hace OFFLINE sobre
cualquier evento pasado (`crosscheck_elcolorado.py --desde/--hasta`), en vez de
depender de que un humano esté despierto mientras llueve.

FORMATO. Un JSONL por estación y mes: `data/ema/<code>-<YYYY-MM>.jsonl`, un
registro crudo por línea. Se archiva el registro TAL CUAL lo entrega la API, con
un campo `momento` normalizado al frente: no sabemos hoy qué campo va a
importar mañana, y reinterpretar es gratis mientras el crudo esté guardado.
Idempotente: releer y reescribir la misma ventana no duplica nada.

Uso:
    python3 monitor/archive_ema.py                 # El Colorado (330077)
    python3 monitor/archive_ema.py --code 330020
    python3 monitor/archive_ema.py --selftest      # sin red
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

API = "https://climatologia.meteochile.gob.cl/application/servicios/getDatosRecientesEma"
DEFAULT_CODE = "330077"          # El Colorado 2750 m
MONITOR = Path(__file__).resolve().parent
ARCHIVE_DIR = MONITOR / "data" / "ema"


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


def credentials():
    creds = load_local_env()
    usuario = os.environ.get("DMC_USUARIO") or creds.get("DMC_USUARIO")
    token = os.environ.get("DMC_TOKEN") or creds.get("DMC_TOKEN")
    return usuario, token


def num(raw):
    """'0.4 mm' → 0.4. La API embebe la unidad en el string."""
    if raw is None:
        return None
    s = str(raw).split()[0].replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def momento_of(rec):
    """Timestamp normalizado 'YYYY-MM-DD HH:MM' (UTC) o None si no se puede leer."""
    stamp = rec.get("momento") or rec.get("fecha") or rec.get("momentoUTC")
    if not stamp:
        return None
    txt = str(stamp).replace("T", " ")[:16]
    try:
        datetime.strptime(txt, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return txt


def fetch_ema_raw(usuario, token, code):
    """Registros crudos de la ventana reciente de la API, sin interpretar."""
    q = urllib.parse.urlencode({"usuario": usuario, "token": token})
    with urllib.request.urlopen(f"{API}/{code}?{q}", timeout=60) as r:
        payload = json.load(r)
    if "bloqueda" in json.dumps(payload):
        raise SystemExit(f"la API rechazó las credenciales: {payload.get('mensaje', payload)}")
    recs = payload.get("datosEstaciones", {}).get("datos", [])
    if not recs:
        raise SystemExit(f"sin registros para {code}; claves: {list(payload)[:8]}")
    return recs


def parse_records(recs):
    """Crudos → {datetime: {'ta': float, 'pp': float}}, la vista que usa el análisis."""
    out = {}
    for rec in recs:
        m = momento_of(rec)
        if not m:
            continue
        out[datetime.strptime(m, "%Y-%m-%d %H:%M")] = {
            "ta": num(rec.get("temperatura")),
            "pp": num(rec.get("aguaCaidaDelMinuto")),
        }
    return out


def month_path(code, momento):
    return ARCHIVE_DIR / f"{code}-{momento[:7]}.jsonl"


def read_archive(code, desde=None, hasta=None):
    """Lee del archivo los registros crudos en [desde, hasta] → {datetime: {...}}.

    `desde`/`hasta` son 'YYYY-MM-DD' o 'YYYY-MM-DD HH:MM' (UTC). Sin archivo
    para el rango pedido devuelve vacío, y el que llama decide si eso es un
    error: aquí no se inventa nada.
    """
    if not ARCHIVE_DIR.exists():
        return []
    lo = (desde or "0000")[:16]
    hi = (hasta or "9999")[:16]
    if hasta and len(hasta) == 10:      # 'YYYY-MM-DD' incluye el día entero
        hi = hasta + " 23:59"
    out = []
    for path in sorted(ARCHIVE_DIR.glob(f"{code}-*.jsonl")):
        if not (lo[:7] <= path.stem.split("-", 1)[1] <= hi[:7]):
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = rec.get("momento")
            if m and lo <= m <= hi:
                out.append(rec)
    return out


def archive(code, recs):
    """Append idempotente por mes → (nuevos, ya_presentes). Escritura atómica."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    por_mes = defaultdict(dict)
    for rec in recs:
        m = momento_of(rec)
        if not m:
            continue
        rec = {"momento": m, **{k: v for k, v in rec.items() if k != "momento"}}
        por_mes[month_path(code, m)][m] = rec

    nuevos = repetidos = 0
    for path, frescos in por_mes.items():
        vistos = set()
        previas = []
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    m = json.loads(line).get("momento")
                except json.JSONDecodeError:
                    continue          # línea corrupta: se descarta al reescribir
                if m and m not in vistos:
                    vistos.add(m)
                    previas.append(line)
        agregar = []
        for m, rec in frescos.items():
            if m in vistos:
                repetidos += 1
            else:
                agregar.append(json.dumps(rec, ensure_ascii=False))
                nuevos += 1
        if not agregar:
            continue
        # atómico: un corte de luz a mitad no debe dejar el archivo mordido
        body = "\n".join(sorted(previas + agregar,
                                key=lambda s: json.loads(s)["momento"])) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    return nuevos, repetidos


def selftest():
    """Verifica idempotencia y rango sin tocar la red."""
    global ARCHIVE_DIR
    with tempfile.TemporaryDirectory() as td:
        ARCHIVE_DIR = Path(td)
        recs = [{"momento": "2026-08-31 09:00", "temperatura": "1.5 °C",
                 "aguaCaidaDelMinuto": "0.6 mm"},
                {"momento": "2026-08-31 09:01", "temperatura": "1.6 °C",
                 "aguaCaidaDelMinuto": "0.0 mm"},
                {"momento": "2026-09-01 00:00", "temperatura": "2.0 °C",
                 "aguaCaidaDelMinuto": None},
                {"fecha": None, "temperatura": "9.9 °C"}]           # sin momento: se ignora
        n, r = archive("TEST", recs)
        assert (n, r) == (3, 0), (n, r)
        n, r = archive("TEST", recs)
        assert (n, r) == (0, 3), f"no es idempotente: {(n, r)}"     # re-archivar no duplica
        assert len(read_archive("TEST")) == 3
        assert len(read_archive("TEST", "2026-08-31", "2026-08-31")) == 2, "rango de día"
        assert len(read_archive("TEST", "2026-08-31 09:01")) == 2, "rango con hora"
        assert read_archive("TEST", "2030-01-01") == [], "rango vacío"
        p = parse_records(recs)
        assert p[datetime(2026, 8, 31, 9, 0)] == {"ta": 1.5, "pp": 0.6}, p
        assert p[datetime(2026, 9, 1, 0, 0)]["pp"] is None, "None no debe volverse 0.0"
        assert len(list(Path(td).glob("TEST-*.jsonl"))) == 2, "debe partir por mes"
    print("[archive_ema] selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", default=DEFAULT_CODE, help="código EMA de la DMC")
    ap.add_argument("--selftest", action="store_true", help="autotest sin red")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    usuario, token = credentials()
    if not usuario or not token:
        print("faltan DMC_USUARIO/DMC_TOKEN (monitor/config.local.env o entorno)")
        return 1

    recs = fetch_ema_raw(usuario, token, args.code)
    nuevos, repetidos = archive(args.code, recs)
    momentos = [m for m in (momento_of(r) for r in recs) if m]
    ventana = f"{min(momentos)} .. {max(momentos)}" if momentos else "sin fechas"
    print(f"[archive_ema] {args.code}: API sirvió {len(recs)} registros ({ventana}); "
          f"nuevos {nuevos}, ya archivados {repetidos} → {ARCHIVE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
