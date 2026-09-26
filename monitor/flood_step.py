#!/usr/bin/env python3
"""Paso VIVO del path de crecidas dentro del ciclo del monitor.

## Qué resuelve

El canal I-D del monitor mide intensidad de lluvia SOBRE LA CELDA y la cruza con
la susceptibilidad de ladera. En la costa de Atacama ese es el mecanismo
equivocado: el aluvión reportado en Puerto Viejo (Caldera, 17-ago-2026) ocurrió
con 1.5 mm de lluvia satelital en su propia celda mientras la cabecera andina
recibía ~30 mm, a 100 km al este. El agua llega RUTEADA. Este paso corre el canal
correcto —producción Hortoniana sobre la cuenca aportante y excedencia Q/Q_c— en
paralelo al I-D, sobre la misma ventana de forzante ya ingerida.

## Qué NO hace, y por qué importa

No reemplaza al I-D: son mecanismos distintos y ambos son reales (el antecedente
del Huasco 2015 describe quebradas laterales que taponan y avulsionan el cauce
principal — ocurrieron los dos, acoplados). Y no notifica por su cuenta salvo que
`FLOOD_NOTIFY=1`: el default es MODO SOMBRA, porque Q_c viene de un cuantil de la
serie SIMULADA (no hay aforo en estas cuencas) y el Hortoniano solo tiene
validación event-céntrica (n=3 en dos cuencas). Meter eso al canal de avisos sin
temporada de sombra erosiona la confianza del único canal que llega al teléfono.

## La ventana y la cola de ruteo

La serie viva se arma como **baldes rodantes de 24 h que terminan en el paso más
nuevo**, no como días calendario: el día calendario en curso está incompleto y
subestimaría la respuesta. Q_c se derivó de la serie CR2MET diaria, que a paso
diario es la misma definición, así que los dos son comparables.

Al final se agregan pasos en cero (`--cola`) para que la convolución del
hidrograma unitario complete el ruteo: con tc ~12 h, la escorrentía de un balde
descarga parcialmente en el siguiente, y sin la cola se truncaría justo el peak.

Uso (lo invoca monitor.sh; también corre a mano):
    WORK_DIR=~/nowcast-monitor-atacama python3 flood_step.py
    python3 flood_step.py --work ~/nowcast-monitor-atacama --json-out /tmp/f.json
"""

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STEP = dt.timedelta(minutes=30)


def key_time(k):
    return dt.datetime.strptime(k, "%Y%m%dT%H%M").replace(tzinfo=dt.timezone.utc)


def pasos_de(dirs):
    """{clave: (ruta, es_relleno)} tomando el PRIMER feed que tenga cada paso.

    El orden de `dirs` es la prioridad: primario primero. Un paso marcado como
    relleno (gapmark) en el primario pero real en un secundario se toma del
    secundario — es la misma lógica de unión que usa la fusión del I-D.
    """
    out = {}
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "step_*.tif"))):
            k = os.path.basename(p)[len("step_"):-len(".tif")]
            relleno = os.path.exists(os.path.join(d, f"gapmark_{k}"))
            if k not in out or (out[k][1] and not relleno):
                out[k] = (p, relleno)
    return out


def serie_cuenca(pasos, celdas):
    """Lluvia media PONDERADA por área sobre la cuenca, paso a paso."""
    import rasterio

    filas = np.array([c[0] for c in celdas])
    cols = np.array([c[1] for c in celdas])
    pesos = np.array([c[2] for c in celdas], dtype="f8")
    pesos = pesos / pesos.sum()
    claves = sorted(pasos)
    vals, rellenos = [], 0
    for k in claves:
        ruta, es_relleno = pasos[k]
        with rasterio.open(ruta) as ds:
            a = ds.read(1)
        if filas.max() >= a.shape[0] or cols.max() >= a.shape[1]:
            raise SystemExit(
                f"desalineación de grilla: el artefacto de cuenca indexa "
                f"({filas.max()},{cols.max()}) y el raster es {a.shape}. "
                "Regenerar con make_flood_basins.py contra la grilla vigente.")
        v = float(np.nansum(a[filas, cols] * pesos))
        vals.append(max(v, 0.0))
        rellenos += int(es_relleno)
    return claves, np.array(vals), rellenos


def baldes_24h(claves, vals, n_baldes):
    """Totales de 24 h rodantes, contiguos y terminando en el paso más nuevo."""
    if not claves:
        return [], []
    t = [key_time(k) for k in claves]
    fin = t[-1]
    etiquetas, totales = [], []
    for b in range(n_baldes - 1, -1, -1):
        hi = fin - dt.timedelta(hours=24 * b)
        lo = hi - dt.timedelta(hours=24)
        tot = sum(v for ti, v in zip(t, vals) if lo < ti <= hi)
        etiquetas.append(hi.strftime("%Y%m%dT%H%M"))
        totales.append(float(tot))
    return etiquetas, totales


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.environ.get(
        "WORK_DIR", os.path.expanduser("~/nowcast-monitor-atacama")))
    ap.add_argument("--runner", default=os.environ.get(
        # instalado fuera de target/ (un borrado de target/ tumbó el monitor
        # 23 días en sep-2026): install -Dm755 target/release/nowcast-flood ~/nowcast-monitor/bin/
        "NOWCAST_FLOOD_BIN", os.path.expanduser("~/nowcast-monitor/bin/nowcast-flood")))
    ap.add_argument("--baldes", type=int, default=2,
                    help="baldes de 24 h a evaluar (la ventana de 54 h da 2)")
    ap.add_argument("--cola", type=int, default=3,
                    help="pasos en cero al final para completar el ruteo del UH")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()

    basins_dir = os.path.join(a.work, "flood_basins")
    # Se valida por CONTENIDO, no por nombre. Filtrar "*_hist_forzante.json" por
    # sufijo parecía suficiente hasta que este mismo script empezó a dejar su
    # forzante VIVO en el mismo directorio y el ciclo siguiente lo leyó como
    # cuenca (KeyError: 'celdas_0p1', 17-ago-2026). El forzante vivo se movió a
    # out/, pero la guardia se queda: cualquier JSON que no traiga las claves de
    # un artefacto de cuenca se ignora, venga de donde venga.
    CLAVES = ("slug", "celdas_0p1", "q_crit", "cn", "tc_horas")
    artefactos = []
    for p in sorted(glob.glob(os.path.join(basins_dir, "*.json"))):
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and all(k in d for k in CLAVES):
            artefactos.append(p)
    if not artefactos:
        print(f"[flood] sin artefactos de cuenca en {basins_dir} — "
              "correr make_flood_basins.py una vez en este nodo", file=sys.stderr)
        return 3
    if not os.access(a.runner, os.X_OK):
        print(f"[flood] runner ausente: {a.runner}", file=sys.stderr)
        return 3

    # Unión de feeds: primario + todos los secundarios que existan.
    dirs = [os.path.join(a.work, "steps")]
    dirs += sorted(glob.glob(os.path.join(a.work, "feeds", "*", "steps")))
    dirs = [d for d in dirs if os.path.isdir(d)]
    pasos = pasos_de(dirs)
    if not pasos:
        print("[flood] no hay pasos de forzante en la ventana", file=sys.stderr)
        return 3

    resultados, peor = [], None
    for ruta in artefactos:
        art = json.load(open(ruta))
        claves, vals, rellenos = serie_cuenca(pasos, art["celdas_0p1"])
        etiquetas, totales = baldes_24h(claves, vals, a.baldes)
        if not totales:
            continue
        serie = totales + [0.0] * a.cola
        etq = etiquetas + [f"cola{i+1}" for i in range(a.cola)]
        # NO en flood_basins/: ese directorio es de artefactos de cuenca y meter
        # acá un forzante hacía que el ciclo siguiente lo leyera como cuenca.
        forz = os.path.join(a.work, "out", f"flood_{art['slug']}_vivo_forzante.json")
        os.makedirs(os.path.dirname(forz), exist_ok=True)
        with open(forz, "w") as f:
            json.dump({"fechas": etq, "pr": serie}, f)
        out = subprocess.run(
            [a.runner, "--forzante", forz,
             "--cn", str(art["cn"]), "--tc-h", str(art["tc_horas"]),
             "--lambda", art.get("lambda", "arido"),
             "--dt-dias", str(art.get("dt_dias", 1.0)),
             "--q-crit", str(art["q_crit"])],
            capture_output=True, text=True)
        if out.returncode != 0:
            print(f"[flood] {art['slug']}: runner falló — {out.stderr.strip()}", file=sys.stderr)
            continue
        rj = json.loads(out.stdout)
        r = {
            "cuenca": art["slug"],
            "comunas": art["comunas"],
            "area_km2": art["area_km2"],
            "lluvia_24h_mm": totales[-1],
            "lluvia_baldes_mm": totales,
            "q_mm_dia": rj["q_max"],
            "q_crit": rj["q_crit"],
            "e_max": rj["e_max"],
            "excede": rj["excede"],
            "paso_max": rj["etiqueta_max"],
            "n_pasos_forzante": len(claves),
            "n_pasos_relleno": rellenos,
            "sin_aforo": art.get("sin_aforo", True),
            "q_crit_origen": art.get("q_crit_origen", ""),
        }
        resultados.append(r)
        if peor is None or r["e_max"] > peor["e_max"]:
            peor = r

    if not resultados:
        print("[flood] ninguna cuenca produjo resultado", file=sys.stderr)
        return 3

    salida = {
        "utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modelo": "hortoniano-scs-cn",
        "modo": "notifica" if os.environ.get("FLOOD_NOTIFY", "0") == "1" else "sombra",
        "n_cuencas": len(resultados),
        "excede_alguna": any(r["excede"] for r in resultados),
        "peor": peor["cuenca"],
        "e_max": peor["e_max"],
        "cuencas": resultados,
        "rotulo": ("Q_c es cuantil de serie SIMULADA, sin aforo en estas cuencas: la "
                   "excedencia mide cuán inusual es la respuesta para ESTE modelo en ESTA "
                   "cuenca, no una magnitud absoluta. Validación del Hortoniano: "
                   "event-céntrica, n=3 en dos cuencas."),
    }
    destino = a.json_out or os.path.join(a.work, "out", "flood.json")
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w") as f:
        json.dump(salida, f, indent=1)

    for r in sorted(resultados, key=lambda x: -x["e_max"]):
        marca = "EXCEDE" if r["excede"] else "bajo   "
        print(f"[flood] {marca} {r['cuenca']:8s} E={r['e_max']:5.2f} "
              f"Q={r['q_mm_dia']:.3f}/{r['q_crit']:.3f} mm/día  "
              f"lluvia24h={r['lluvia_24h_mm']:.1f} mm  ({r['n_pasos_forzante']} pasos, "
              f"{r['n_pasos_relleno']} rellenos)")
    print(f"[flood] {salida['modo']} — peor {peor['cuenca']} E={peor['e_max']:.2f} → {destino}")
    # 2 = alguna cuenca excede (mismo convenio que `nowcast run`), 0 = ninguna.
    return 2 if salida["excede_alguna"] else 0


if __name__ == "__main__":
    sys.exit(main())
