#!/usr/bin/env python3
"""Fetcher de pluviómetros in-situ (API DMC Servicios Climáticos) para el monitor.

Tercera forzante del monitor (fase A del plan EWS): pluviómetros terrestres con
latencia ~20-60 min — la única observación sub-horaria in-situ disponible.

FUENTE (verificada 2026-07-11 contra la API real): las EMAs PROPIAS de la DMC
vía `getDatosRecientesEma/{codigo}` — ~12 h de registros minutarios, newest-
first, timezone UTC, valores con unidad embebida ("0.4 mm"). El campo clave es
`aguaCaidaDelMinuto` (lámina por minuto); fallback `aguaCaida24Horas`
(acumulado con reset diario). NOTA HONESTA: los códigos de las estaciones DGA
republicadas (El Yeso 330149, Laguna Negra 330146, …) responden "Información
no disponible" en esta API — la DMC publica su *metadata*, no sus datos. Los
pluviómetros DGA de alta cordillera quedan como mejora futura vía HIDROlínea
(snia.mop.gob.cl/sat — JSF; el mapa inline trae solo el último valor). Los
defaults son EMAs DMC dentro del dominio piloto con buen gradiente:
El Colorado (2750 m), San José Guayacán (928 m), La Florida (670 m).

REQUIERE registro gratuito: crear cuenta en
https://climatologia.meteochile.gob.cl/application/usuario/registroUsuario y
poner DMC_USUARIO (email) + DMC_TOKEN (API key) en config.env. Sin token el
feed se declara deshabilitado en status.json y monitor.sh sigue sin él.

Cada ciclo:
  1. Por estación configurada (DGA_STATIONS = "codigo:lon:lat:nombre,…"), GET
     `getDatosRecientesEma/{codigo}` → ~12 h de registros.
  2. Extrae la serie de precipitación: primero por las claves REALES del
     esquema (aguaCaidaDelMinuto → intervalo; aguaCaida24Horas → acumulado con
     reset), y si ninguna existe cae al parser genérico defensivo (nombres que
     matcheen precipitación/agua caída, modo auto-detectado). Anomalías de
     parseo son RUIDOSAS (log + conteo en status.json), nunca silenciosas.
  3. Rasteriza cada paso a la MISMA grilla 0.1° de los otros feeds (convenio
     IMERG, mismo geotransform → `same_grid` del CLI acepta la fusión) por IDW
     (potencia 2) entre las estaciones con dato. Grueso a propósito: 3
     pluviómetros no son un campo de lluvia; es la pata in-situ del noisy-OR,
     no un QPE.
  4. Huecos viejos → cero MARCADO (gapmark_*), rotación y status.json — el
     mismo contrato que los feeds IMERG y GOES. Nota de arranque en frío: la
     API solo entrega 12 h, así que el feed tarda ~2 días en llenar su ventana
     de 48 h; mientras tanto su gap_fraction lo declara degradado (honesto).

Config por variables de entorno (las exporta monitor.sh desde config.env):
BBOX, WORK_DIR (raíz del feed, p.ej. $MON/feeds/dga), WINDOW_HOURS,
DMC_USUARIO, DMC_TOKEN, DGA_STATIONS, DGA_IDW_POWER. Selftest sin red:
    python3 fetch_dga.py --selftest
"""

import glob
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

from fetch_goes_qpe import target_grid, zero_raster_like

STEP = timedelta(minutes=30)
API = "https://climatologia.meteochile.gob.cl/application/servicios/getDatosRecientesEma"
PRECIP_RE = re.compile(r"precipita|agua\s*ca[ií]da|aguacaida|\brr\b", re.IGNORECASE)
TIME_KEY_RE = re.compile(r"^(momento|fecha|hora|time|instante)", re.IGNORECASE)
# EMAs DMC dentro del dominio piloto con pluviómetro minutario verificado
# (getEstacionesRedEma + prueba real 2026-07-11). Río Clarillo (330075) quedó
# fuera: su registro no trae ningún campo de precipitación.
DEFAULT_STATIONS = (
    "330077:-70.294:-33.350:El Colorado 2750m,"
    "330112:-70.351:-33.615:San Jose Guayacan,"
    "330122:-70.548:-33.545:Aguas Andinas La Florida"
)
# Claves de precipitación del esquema real, en orden de preferencia.
INTERVAL_KEY = "aguaCaidaDelMinuto"   # lámina por minuto → modo intervalo
ACCUM_KEY = "aguaCaida24Horas"        # acumulado 24 h con reset → modo diff


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"fetch_dga: falta la variable {name}")
    return v


def step_key(dt):
    return dt.strftime("%Y%m%dT%H%M")


def key_time(key):
    return datetime.strptime(key, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def parse_stations(spec):
    out = []
    for item in spec.split(","):
        parts = item.strip().split(":")
        if len(parts) < 3:
            sys.exit(f"fetch_dga: estación malformada en DGA_STATIONS: {item!r}")
        out.append({
            "code": parts[0],
            "lon": float(parts[1]),
            "lat": float(parts[2]),
            "name": parts[3] if len(parts) > 3 else parts[0],
        })
    return out


def parse_time(text):
    """Timestamp DMC → aware UTC. La API declara timezone UTC en su cabecera;
    se aceptan los formatos vistos en los productos DMC."""
    text = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _num(raw):
    """Primer número de un valor DMC ("0.4 mm", "58 %", 1.2, None) o None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(float(raw)) else None
    m = _NUM_RE.search(str(raw))
    return float(m.group(0).replace(",", ".")) if m else None


def _harvest_precip(node):
    """Valores de precipitación dentro de un registro (dict/list anidado).

    Acepta tanto {"nombreParametro": "Agua Caída", "valor": 1.2} como pares
    directos {"aguaCaida": "1.2"}. Devuelve lista de floats encontrados.
    """
    found = []
    if isinstance(node, dict):
        name = node.get("nombreParametro") or node.get("parametro") or node.get("nombre")
        if name is not None and PRECIP_RE.search(str(name)):
            v = _num(node.get("valor", node.get("value")))
            if v is not None:
                found.append(v)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                found += _harvest_precip(v)
            elif PRECIP_RE.search(str(k)):
                fv = _num(v)
                if fv is not None:
                    found.append(fv)
    elif isinstance(node, list):
        for item in node:
            found += _harvest_precip(item)
    return found


def extract_series(payload):
    """Payload getDatosRecientesEma → ([(utc, mm_valor)] ordenado, modo).

    Primero por las claves REALES del esquema verificado: `aguaCaidaDelMinuto`
    (lámina por intervalo) con fallback `aguaCaida24Horas` (acumulado con
    reset). Si ninguna aparece, cae al parser genérico defensivo (nombres que
    matcheen precipitación) con modo auto-detectado (`None`). El orden de la
    API es newest-first; aquí se reordena cronológico.
    """
    records = []

    def walk(node):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            tkeys = [k for k in node[0] if TIME_KEY_RE.search(k)]
            if tkeys:
                records.append((node, tkeys[0]))
                return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    for key, mode in ((INTERVAL_KEY, "interval"), (ACCUM_KEY, "accumulated")):
        series = []
        for node, tkey in records:
            for rec in node:
                t = parse_time(rec.get(tkey))
                v = _num(rec.get(key))
                if t is not None and v is not None:
                    series.append((t, v))
        if series:
            series.sort(key=lambda p: p[0])
            return series, mode
    series = []
    for node, tkey in records:
        for rec in node:
            t = parse_time(rec.get(tkey))
            if t is None:
                continue
            vals = _harvest_precip(rec)
            if vals:
                series.append((t, vals[0]))
    series.sort(key=lambda p: p[0])
    return series, None


def series_to_step_depths(series, mode=None):
    """[(utc, valor)] → {step_key: lámina mm}.

    `mode` viene de `extract_series` cuando la clave del esquema lo define
    (interval/accumulated). Con `mode=None` (parser genérico) se auto-detecta:
    serie casi no-decreciente = ACUMULADO (lámina = diferencias positivas; un
    salto negativo es reset del pluviómetro y el valor siguiente arranca la
    nueva base — la lluvia entre el último dato y el reset se pierde:
    subestima, nunca inventa); si abundan los deltas negativos, POR INTERVALO
    (se suman los valores). El modo usado viaja en status.json por estación.
    """
    if len(series) < 2:
        return {}, "insufficient"
    if mode is None:
        vals = np.array([v for _, v in series])
        deltas = np.diff(vals)
        negatives = (deltas < -0.01).sum()
        mode = "accumulated" if negatives <= max(1, 0.2 * len(deltas)) else "interval"
    depths = {}

    def add(t, mm):
        if mm <= 0:
            return
        k = step_key(t - timedelta(minutes=t.minute % 30, seconds=t.second,
                                   microseconds=t.microsecond))
        depths[k] = depths.get(k, 0.0) + mm

    if mode == "accumulated":
        for (t0, v0), (t1, v1) in zip(series, series[1:]):
            d = v1 - v0
            if d > 0:
                add(t1, d)
            # d < 0: reset del acumulador — la nueva base es v1, nada que sumar.
    else:
        for t, v in series:
            add(t, v)
    return depths, mode


def idw_field(stations_mm, lons, lats, power):
    """IDW de los valores por estación a la grilla (norte arriba)."""
    nrows, ncols = lats.size, lons.size
    grid = np.zeros((nrows, ncols))
    glon, glat = np.meshgrid(lons, lats)
    num = np.zeros_like(grid)
    den = np.zeros_like(grid)
    for st, mm in stations_mm:
        d2 = (glon - st["lon"]) ** 2 + (glat - st["lat"]) ** 2
        w = 1.0 / np.maximum(d2, 1e-8) ** (power / 2.0)
        num += w * mm
        den += w
    grid = np.where(den > 0, num / den, 0.0)
    return grid[::-1, :]  # S→N a north-up


def write_step(out_tif, grid, lons, lats):
    import rasterio
    from rasterio.transform import from_origin

    transform = from_origin(lons[0] - 0.05, lats[-1] + 0.05, 0.1, 0.1)
    with rasterio.open(
        out_tif, "w", driver="GTiff", height=lats.size, width=lons.size, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(grid.astype("float32"), 1)


def selftest():
    """Extracción + modo + IDW + GeoTIFF sin red, con tres variantes de esquema."""
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    # Variante REAL (verificada 2026-07-11): registros planos newest-first con
    # aguaCaidaDelMinuto y unidades embebidas → modo interval por clave.
    recs_real = [
        {"momento": (now + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
         "temperatura": "3.0 °C", "aguaCaidaDelMinuto": f"{v} mm",
         "aguaCaida24Horas": "9.9 mm"}
        for i, v in enumerate([0.0, 0.2, 0.4, 0.0])
    ][::-1]  # newest-first como la API real
    series, mode = extract_series({"datosEstaciones": {"estacion": {}, "datos": recs_real}})
    assert mode == "interval", mode
    assert len(series) == 4 and series[0][0] < series[-1][0], "orden cronológico"
    depths, mode = series_to_step_depths(series, mode)
    assert abs(sum(depths.values()) - 0.6) < 1e-9, depths  # 0.2+0.4, no el 24h
    # Variante A: parámetros nombrados; serie acumulada con reset (genérico).
    recs_a = []
    accum = [0.0, 1.5, 3.0, 3.0, 0.5, 2.5]
    for i, v in enumerate(accum):
        recs_a.append({
            "momento": (now + timedelta(minutes=30 * i)).strftime("%Y-%m-%d %H:%M:%S"),
            "datos": [
                {"nombreParametro": "Temperatura del Aire", "valor": "5.2"},
                {"nombreParametro": "Agua Caída Acumulada", "valor": str(v)},
            ],
        })
    series, mode = extract_series({"estacion": {"codigo": 330149}, "datosEstaciones": recs_a})
    assert len(series) == 6, f"extracción variante A: {len(series)}"
    assert mode is None, mode  # sin clave del esquema → auto-detección
    depths, mode = series_to_step_depths(series, mode)
    assert mode == "accumulated", mode
    # diffs positivos: +1.5, +1.5, (reset), +2.0 → 5.0 mm en total
    assert abs(sum(depths.values()) - 5.0) < 1e-9, depths
    # Variante B: clave directa estilo "aguaCaida", por intervalo.
    recs_b = [
        {"fechaHora": (now + timedelta(minutes=30 * i)).strftime("%d-%m-%Y %H:%M"),
         "aguaCaida": str(v), "tempAire": "4.0"}
        for i, v in enumerate([0.0, 2.0, 0.0, 1.0])
    ]
    series_b, _ = extract_series({"datos": recs_b})
    assert len(series_b) == 4, f"extracción variante B: {len(series_b)}"
    # IDW + escritura
    lons, lats = target_grid((-70.75, -34.25, -69.75, -33.25))
    st = parse_stations(DEFAULT_STATIONS)
    grid = idw_field([(st[0], 10.0), (st[1], 0.0)], lons, lats, 2.0)
    assert grid.shape == (11, 11) and grid.max() <= 10.0 + 1e-9 and grid.min() >= 0.0
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        write_step(os.path.join(td, "t.tif"), grid, lons, lats)
        import rasterio

        with rasterio.open(os.path.join(td, "t.tif")) as src:
            assert abs(src.transform.c - -70.80) < 1e-9 and abs(src.transform.f - -33.20) < 1e-9
    print("fetch_dga selftest: OK (extracción A/B, modo acumulado con reset, IDW, grilla)")


def main():
    bbox = tuple(float(x) for x in env("BBOX").split(","))
    work = env("WORK_DIR")
    window_h = float(env("WINDOW_HOURS", "54"))
    usuario = os.environ.get("DMC_USUARIO", "")
    token = os.environ.get("DMC_TOKEN", "")
    # `or`: en config.env la variable existe con valor "" (= usar defaults);
    # un env() a secas tomaría el string vacío y fallaría el parseo.
    stations = parse_stations(os.environ.get("DGA_STATIONS") or DEFAULT_STATIONS)
    power = float(env("DGA_IDW_POWER", "2.0"))

    steps_dir = os.path.join(work, "steps")
    os.makedirs(steps_dir, exist_ok=True)
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=window_h)
    first = (t0 + (STEP - timedelta(minutes=t0.minute % 30, seconds=t0.second,
                                    microseconds=t0.microsecond)) % STEP)

    if not usuario or not token:
        status = {
            "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": "dga-dmc",
            "disabled": True,
            "reason": "sin DMC_USUARIO/DMC_TOKEN (registro gratuito en "
                      "climatologia.meteochile.gob.cl, sección Servicios Climáticos)",
        }
        with open(os.path.join(work, "status.json"), "w") as f:
            json.dump(status, f, indent=1)
        print("[dga] feed deshabilitado: falta DMC_USUARIO/DMC_TOKEN")
        return

    lons, lats = target_grid(bbox)
    per_station = {}
    modes = {}
    fetch_errors = 0
    for st in stations:
        q = urllib.parse.urlencode({"usuario": usuario, "token": token})
        url = f"{API}/{st['code']}?{q}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                payload = json.load(r)
        except Exception as exc:
            fetch_errors += 1
            print(f"[dga] ERROR consultando {st['code']} ({st['name']}): {exc}",
                  file=sys.stderr)
            continue
        if isinstance(payload, dict) and "bloqueda" in json.dumps(payload):
            fetch_errors += 1
            print(f"[dga] ERROR: la API rechazó las credenciales para {st['code']}: "
                  f"{payload.get('mensaje', payload)}", file=sys.stderr)
            continue
        series, key_mode = extract_series(payload)
        if not series:
            fetch_errors += 1
            print(f"[dga] ERROR: sin serie de precipitación parseable en "
                  f"{st['code']} ({st['name']}) — esquema inesperado; "
                  f"claves raíz: {list(payload)[:8]}", file=sys.stderr)
            continue
        depths, mode = series_to_step_depths(series, key_mode)
        per_station[st["code"]] = (st, depths)
        modes[st["code"]] = mode
        print(f"[dga] {st['code']} ({st['name']}): {len(series)} registros, "
              f"modo={mode}, {len(depths)} pasos con lluvia")

    # Pasos cubiertos por la ventana de 12 h de la API: escribir/actualizar.
    # El eje DEBE quedar alineado a la media hora (mismas claves que los otros
    # feeds) y solo se escriben pasos COMPLETOS (fin del bloque <= ahora): un
    # paso en curso subestimaría la lluvia del bloque sin marcarlo.
    written = 0
    if per_station:
        api_first = max(first, now - timedelta(hours=12))
        t = api_first + (STEP - timedelta(minutes=api_first.minute % 30,
                                          seconds=api_first.second,
                                          microseconds=api_first.microsecond)) % STEP
        while t + STEP <= now:
            k = step_key(t)
            with_data = [(st, depths.get(k, 0.0)) for st, depths in per_station.values()]
            grid = idw_field(with_data, lons, lats, power)
            write_step(os.path.join(steps_dir, f"step_{k}.tif"), grid, lons, lats)
            mark = os.path.join(steps_dir, f"gapmark_{k}")
            if os.path.exists(mark):
                os.remove(mark)  # un paso real reemplaza su hueco marcado
            written += 1
            t += STEP

    # Huecos viejos (incluida la cola pre-horizonte de la API en frío) → cero
    # MARCADO; el gap_fraction resultante declara el arranque en frío.
    have = {
        os.path.basename(p)[len("step_"):-len(".tif")]
        for p in glob.glob(os.path.join(steps_dir, "step_*.tif"))
    }
    real = {k for k in have if not os.path.exists(os.path.join(steps_dir, f"gapmark_{k}"))}
    newest_real = max((key_time(k) for k in real), default=None)
    if newest_real is not None:
        ref = sorted(glob.glob(os.path.join(steps_dir, "step_*.tif")))
        t = first
        while t <= newest_real:
            k = step_key(t)
            if k not in have and ref:
                out = os.path.join(steps_dir, f"step_{k}.tif")
                zero_raster_like(ref[0], out)
                open(os.path.join(steps_dir, f"gapmark_{k}"), "w").close()
                print(f"[dga] hueco {k} rellenado con 0 mm (MARCADO)")
            t += STEP

    for p in glob.glob(os.path.join(steps_dir, "step_*.tif")):
        k = os.path.basename(p)[len("step_"):-len(".tif")]
        if key_time(k) < first:
            os.remove(p)
            mark = os.path.join(steps_dir, f"gapmark_{k}")
            if os.path.exists(mark):
                os.remove(mark)

    n_steps = len(glob.glob(os.path.join(steps_dir, "step_*.tif")))
    n_gap_marks = len(glob.glob(os.path.join(steps_dir, "gapmark_*")))
    stale_min = (now - newest_real).total_seconds() / 60 if newest_real else None
    status = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed": "dga-dmc",
        "stations_ok": len(per_station),
        "stations_total": len(stations),
        "station_modes": modes,
        "fetch_errors": fetch_errors,
        "newest_step": step_key(newest_real) if newest_real else None,
        "stale_minutes": stale_min,
        "n_steps": n_steps,
        "n_gaps": n_gap_marks,
        "gap_fraction": (n_gap_marks / n_steps) if n_steps else 1.0,
        "steps_written_this_cycle": written,
    }
    with open(os.path.join(work, "status.json"), "w") as f:
        json.dump(status, f, indent=1)
    print(f"[dga] status: {json.dumps(status)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        main()
