#!/usr/bin/env python3
"""Retención de los ciclos de pronóstico: libera espacio SIN perder el registro.

El problema que resuelve (medido 2026-08-01): `forecast_all.sh` + `analysis_all.sh`
corren 4×/día y producen ~9 directorios de ciclo diarios de ~1.1 GB cada uno.
NADA los podaba: en 19 días `~/nowcast-forecast` llegó a 80 GB y el disco de la
máquina a 100% (7 GB libres), con la cadena a punto de romperse sola.

## Qué se borra y por qué se puede
El peso de un ciclo NO está en sus productos científicos:

    .tif de 30 m (hazard_x_susc_30m_*)   58.8 GB   ← el 98% del volumen
    .grib2 / .bz2 (modelo crudo)         14.4 GB
    .tif de 0.1° (hazard_max, ...)        0.23 MB por dominio
    .json (forecast, watchlist, comuna)   40 MB TOTAL en 179 ciclos

Los cruces de 30 m son **caché, no dato**. `susc_overlay.py:319` los produce así:

    cross30 = hazard_0.1° (remuestreado a 30 m) × susceptibilidad_30m (KINGSTON)

y AMBOS insumos sobreviven la poda — el hazard 0.1° pesa 0.23 MB y se conserva,
la susceptibilidad vive permanente en KINGSTON. Regenerar un ciclo concreto es
`python3 susc_overlay.py --forecast <ciclo>`, minutos y sin red. Por eso se
borran los viejos: se pueden reconstruir exactamente.

El crudo (`.grib2`/`.bz2`) SÍ es irrecuperable —NOMADS conserva ~10 días— pero
ya cumplió su función: de él salió el hazard 0.1° que se conserva. Solo haría
falta para re-derivar con OTRO umbral I-D, y para eso están los ciclos recientes.

## El GeoJSON comunal se ADELGAZA, no se borra (2026-09-28)
`comuna_impact/comuna_impact.geojson` pesa ~78 MB por ciclo y el 100% es
geometría: los mismos polígonos del censo reescritos en cada ciclo. En 187
ciclos sumaba 14.7 GB — el mayor volumen del directorio — y la tabla de arriba
no lo veía porque aquella medición contó `*.json` y no `*.geojson`. Lo que es
DATO del ciclo son sus columnas (peak_gfs/ecmwf/icon/terreno/worst, nivel,
población…). Pasada su retención se escribe `comuna_impact_attrs.csv` con TODAS
las propiedades de cada comuna y, solo si el CSV se verificó (mismas filas que
features), se borra el GeoJSON. La geometría se reconstruye desde el GPKG del
censo por CUT. El historial queda completo.

## Qué NUNCA se toca
`informes/` (el registro de constancia HTML+PDF con su hash de integridad),
`runouts/`, todos los `.json`, los rásters de 0.1°, y cualquier ciclo dentro de
la ventana de retención o marcado como protegido (`--keep`, para eventos que se
quieran conservar íntegros — p.ej. Coelemu 31-jul-2026).

MARCHA EN SECO POR DEFECTO: sin `--apply` solo informa. Es borrado de datos del
usuario; que haya que pedirlo explícitamente es deliberado.

Uso:
    python3 prune_forecast.py                      # informe, no borra nada
    python3 prune_forecast.py --apply
    python3 prune_forecast.py --dias-30m 7 --dias-crudo 10 --keep 20260731 --apply
"""

import argparse
import csv
import json
import glob
import os
import re
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.expanduser("~/nowcast-forecast")
# Nunca entran en ningún patrón de borrado, estén donde estén.
INTOCABLES = ("informes", "runouts")


def ciclos(base):
    """Directorios de ciclo (2026....), sin symlinks (`latest` apunta a uno)."""
    out = []
    for d in sorted(glob.glob(os.path.join(base, "2026*"))):
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        m = re.match(r"(\d{8})", os.path.basename(d))
        if m:
            out.append((d, m.group(1)))
    return out


GEOJSON_REL = os.path.join("comuna_impact", "comuna_impact.geojson")
ATTRS_REL = os.path.join("comuna_impact", "comuna_impact_attrs.csv")


def geojson_a_csv(geojson_path, csv_path):
    """Escribe las propiedades de cada feature (sin geometría) y verifica.

    Devuelve el número de filas escritas; lanza si no cuadra con las features,
    y en ese caso el GeoJSON NO debe borrarse. Escritura atómica: un corte a
    mitad deja el .tmp, nunca un CSV mordido que parezca válido.
    """
    with open(geojson_path) as f:
        feats = json.load(f)["features"]
    cols = []
    for ft in feats:
        for k in ft.get("properties") or {}:
            if k not in cols:
                cols.append(k)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for ft in feats:
            w.writerow(ft.get("properties") or {})
    with open(tmp, newline="") as f:
        n = sum(1 for _ in csv.DictReader(f))
    if n != len(feats):
        os.remove(tmp)
        raise ValueError(f"{csv_path}: {n} filas vs {len(feats)} features")
    os.replace(tmp, csv_path)
    return n


def adelgazables(base, corte_geojson, protegidos):
    """(geojson, bytes) de cada GeoJSON comunal pasado su retención. No toca."""
    for d, fecha in ciclos(base):
        if any(os.path.basename(d).startswith(p) for p in protegidos):
            continue
        g = os.path.join(d, GEOJSON_REL)
        if fecha < corte_geojson and os.path.isfile(g):
            yield g, os.path.getsize(g)


def borrables(base, corte_30m, corte_crudo, protegidos):
    """(ruta, bytes, motivo) de cada archivo podable. No borra."""
    for d, fecha in ciclos(base):
        nombre = os.path.basename(d)
        if any(nombre.startswith(p) for p in protegidos):
            continue
        # R4-L6: la protección de informes/runouts es ESTRUCTURAL (el glob
        # 2026* no los matchea); esta aserción la vuelve verificable en vez de
        # aparentar un chequeo por nombre que nunca disparaba.
        assert os.path.basename(d) not in INTOCABLES, f"glob rompió el invariante: {d}"
        if fecha < corte_30m:
            for p in glob.glob(os.path.join(d, "*", "hazard_x_susc_30m_*.tif")):
                yield p, os.path.getsize(p), "cruce 30 m (regenerable)"
        if fecha < corte_crudo:
            for pat in ("**/*.grib2", "**/*.bz2"):
                for p in glob.glob(os.path.join(d, pat), recursive=True):
                    yield p, os.path.getsize(p), "modelo crudo"


def main():
    hoy = datetime.now(timezone.utc)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--dias-30m", type=int, default=7,
                    help="conservar los cruces de 30 m de los últimos N días (default 7)")
    ap.add_argument("--dias-crudo", type=int, default=10,
                    help="conservar el modelo crudo de los últimos N días (default 10)")
    ap.add_argument("--dias-geojson", type=int, default=7,
                    help="conservar el GeoJSON comunal completo de los últimos N "
                         "días; los anteriores pasan a CSV sin geometría (default 7)")
    ap.add_argument("--keep", action="append", default=[],
                    help="prefijo de ciclo a conservar ÍNTEGRO, repetible "
                         "(p.ej. --keep 20260731 para un evento de interés)")
    ap.add_argument("--apply", action="store_true",
                    help="borrar de verdad (sin esto solo informa)")
    a = ap.parse_args()

    if not os.path.isdir(a.base):
        sys.exit(f"[prune] no existe {a.base}")
    c30 = (hoy - timedelta(days=a.dias_30m)).strftime("%Y%m%d")
    ccr = (hoy - timedelta(days=a.dias_crudo)).strftime("%Y%m%d")
    cgj = (hoy - timedelta(days=a.dias_geojson)).strftime("%Y%m%d")
    print(f"[prune] {len(ciclos(a.base))} ciclos en {a.base}")
    print(f"[prune] cruces de 30 m: se borran los anteriores a {c30} (regenerables "
          f"con susc_overlay.py)")
    print(f"[prune] modelo crudo:   se borra el anterior a {ccr}")
    print(f"[prune] GeoJSON comunal: anterior a {cgj} pasa a CSV sin geometría")
    if a.keep:
        print(f"[prune] protegidos íntegros: {', '.join(a.keep)}")

    n = b = 0
    por_motivo = {}
    for p, sz, motivo in borrables(a.base, c30, ccr, a.keep):
        n += 1
        b += sz
        m = por_motivo.setdefault(motivo, [0, 0])
        m[0] += 1
        m[1] += sz
        if a.apply:
            try:
                os.remove(p)
            except OSError as e:
                print(f"[prune] no se pudo borrar {p}: {e}", file=sys.stderr)
    for g, sz in adelgazables(a.base, cgj, a.keep):
        motivo = "GeoJSON comunal → CSV"
        if a.apply:
            try:
                geojson_a_csv(g, os.path.join(os.path.dirname(os.path.dirname(g)), ATTRS_REL))
                os.remove(g)
            except (OSError, ValueError, KeyError) as e:
                print(f"[prune] NO se adelgazó {g} (queda intacto): {e}", file=sys.stderr)
                continue
        n += 1
        b += sz
        m = por_motivo.setdefault(motivo, [0, 0])
        m[0] += 1
        m[1] += sz
    for motivo, (cn, cb) in sorted(por_motivo.items()):
        print(f"[prune]   {motivo:26s} {cn:6d} archivos · {cb/2**30:6.2f} GB")
    verbo = "LIBERADOS" if a.apply else "se liberarían (marcha en seco)"
    print(f"[prune] {verbo}: {b/2**30:.2f} GB en {n} archivos")
    print(f"[prune] intactos siempre: {', '.join(INTOCABLES)}/, *.json, rásters 0.1° "
          f"(el GeoJSON comunal conserva sus columnas en comuna_impact_attrs.csv)")
    if not a.apply and n:
        print("[prune] nada se borró — repetir con --apply")


if __name__ == "__main__":
    main()
