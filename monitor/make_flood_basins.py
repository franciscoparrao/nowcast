#!/usr/bin/env python3
"""Preparación OFFLINE de las cuencas del path de crecidas (una vez por nodo).

## Por qué existe y por qué es offline

El paso vivo (`flood_step.py`) corre cada 30 min y NO puede depender de KINGSTON:
el disco puede no estar montado, y delinear una cuenca por retroceso D8 sobre un
DEM de 30 m toma minutos. Así que todo lo que necesita el DEM se calcula acá, una
vez, y se persiste:

  - la geometría (área, L, H) y el **tc de Giandotti**, que es el único parámetro
    temporal del Hortoniano y sale de la geometría, no de un hidrograma;
  - las **celdas de 0.1°** que cubren la cuenca, CON PESO por fracción de área,
    para que el paso vivo promedie la lluvia del monitor sobre la cuenca sin
    volver a mirar el DEM;
  - el **Q_c**, derivado corriendo el modelo sobre la serie CR2MET histórica.

## El rótulo que viaja con el artefacto

Q_c es un cuantil de la serie **SIMULADA**, no de caudal aforado: no hay estación
de Atacama en CAMELS-CL en este disco. La excedencia significa "cuán inusual es
esta respuesta para lo que ESTE modelo produce en ESTA cuenca". El artefacto
guarda `q_crit_origen` y `sin_aforo: true` para que el rótulo no se pierda por el
camino, y `flood_step.py` lo re-emite en cada ciclo.

## Los parámetros, y por qué ninguno se ajusta

CN 88: regolito desnudo / roca con grupo hidrológico D cae en 85–95 (ver
`hortonian.rs`). Es una estimación de la CLASE de suelo, no un ajuste — no hay
hidrograma observado contra el cual ajustar, y si lo hubiera, ajustar un CN
contra un único evento sería sobreajuste. lambda = 0.05 (Woodward et al. 2003,
revisión árida) en vez del 0.20 clásico. tc del DEM por Giandotti.

Uso:
    python3 make_flood_basins.py                    # las tres cuencas
    python3 make_flood_basins.py --solo copiapo
    python3 make_flood_basins.py --anios 1990 2021
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Las tres cuencas troncales del dominio atacama y las comunas que cuelgan de
# cada una. El outlet es un punto COSTERO aproximado: `delinear` lo engancha al
# cauce mapeado más cercano (snap), así que no hace falta precisión — pero el
# área resultante se compara contra `area_esperada_km2` como guardia de cordura,
# porque un snap a la quebrada equivocada produce una cuenca plausible y falsa.
CUENCAS = {
    "salado": dict(
        basin="04_rio_salado", outlet=(-70.62, -26.35), cn=88.0,
        comunas=["Chañaral", "Diego de Almagro"], area_esperada_km2=7500,
    ),
    "copiapo": dict(
        basin="05_rio_copiapo", outlet=(-70.87, -27.30), cn=88.0,
        comunas=["Copiapó", "Tierra Amarilla", "Caldera (Puerto Viejo)"],
        area_esperada_km2=18400,
    ),
    "huasco": dict(
        basin="06_rio_huasco", outlet=(-71.22, -28.46), cn=88.0,
        comunas=["Vallenar", "Freirina", "Huasco", "Alto del Carmen"],
        area_esperada_km2=9850,
    ),
}

TOL_AREA = 0.45   # 45 %: el snap y la resolución del DEM mueven el área; más que
                  # esto significa que se enganchó a otra cuenca, no ruido.


def celdas_0p1(mask, tr, crs, grid_tif):
    """Celdas de la grilla del monitor (0.1°) que cubren la cuenca, con peso.

    Devuelve [(fila, col, peso)] con los pesos sumando 1: la fracción del área de
    la cuenca que cae en cada celda. El paso vivo hace el promedio PONDERADO, que
    es lo correcto — una celda que toca la cuenca en una esquina no debe pesar
    igual que una que la cubre entera.
    """
    import rasterio
    from pyproj import Transformer

    rs, cs = np.nonzero(mask)
    if rs.size == 0:
        raise SystemExit("la máscara de la cuenca está vacía")
    # Centroide de cada celda del DEM dentro de la cuenca → lon/lat.
    xs, ys = rasterio.transform.xy(tr, rs, cs)
    tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = tf.transform(np.asarray(xs), np.asarray(ys))
    with rasterio.open(grid_tif) as ds:
        gtr, H, W = ds.transform, ds.height, ds.width
    filas, cols = rasterio.transform.rowcol(gtr, lons, lats)
    filas = np.asarray(filas)
    cols = np.asarray(cols)
    dentro = (filas >= 0) & (filas < H) & (cols >= 0) & (cols < W)
    if not dentro.any():
        raise SystemExit("la cuenca no intersecta la grilla del monitor")
    fuera = int((~dentro).sum())
    pares, cuenta = np.unique(
        np.stack([filas[dentro], cols[dentro]], axis=1), axis=0, return_counts=True
    )
    peso = cuenta / cuenta.sum()
    celdas = [[int(f), int(c), float(w)] for (f, c), w in zip(pares, peso)]
    celdas.sort(key=lambda x: -x[2])
    return celdas, fuera, float(dentro.sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", choices=sorted(CUENCAS), help="una sola cuenca")
    ap.add_argument("--anios", nargs=2, type=int, default=[1979, 2021])
    ap.add_argument("--q-crit-p", type=float, default=0.995,
                    help="cuantil de la serie SIMULADA para Q_c")
    ap.add_argument("--work", default=os.path.expanduser("~/nowcast-monitor-atacama"))
    ap.add_argument("--grid", default=None, help="GeoTIFF que define la grilla 0.1° "
                                                "(default: <work>/susc_0p1.tif)")
    ap.add_argument("--runner", default=os.environ.get(
        # instalado fuera de target/ (un borrado de target/ tumbó el monitor
        # 23 días en sep-2026): install -Dm755 target/release/nowcast-flood ~/nowcast-monitor/bin/
        "NOWCAST_FLOOD_BIN", os.path.expanduser("~/nowcast-monitor/bin/nowcast-flood")))
    a = ap.parse_args()

    grid = a.grid or os.path.join(a.work, "susc_0p1.tif")
    if not os.path.exists(grid):
        raise SystemExit(f"no existe la grilla de referencia {grid}")
    if not os.access(a.runner, os.X_OK):
        raise SystemExit(f"no existe o no es ejecutable el runner {a.runner}\n"
                         "  compilar: cargo build --release -p nowcast-rainflow "
                         "--bin nowcast-flood")

    import flood_salado as fs

    destino = os.path.join(a.work, "flood_basins")
    os.makedirs(destino, exist_ok=True)
    slugs = [a.solo] if a.solo else sorted(CUENCAS)
    resumen = []

    for slug in slugs:
        cfg = CUENCAS[slug]
        print(f"\n=== {slug} ({cfg['basin']}) ===", flush=True)
        mask, tr, crs, area, (rs, cs), facc_area = fs.delinear(
            basin=cfg["basin"], outlet=cfg["outlet"])
        print(f"  área delineada: {area:,.0f} km²  (esperada ~{cfg['area_esperada_km2']:,} km²)")
        rel = abs(area - cfg["area_esperada_km2"]) / cfg["area_esperada_km2"]
        if rel > TOL_AREA:
            print(f"  ABORTA: el área difiere {100*rel:.0f} % de la esperada — el outlet "
                  f"probablemente se enganchó a otra cuenca. Ajustar --outlet.", file=sys.stderr)
            continue

        res = abs(tr.a)
        _, L_km, H_m, tc_k, tc_g = fs.tiempo_concentracion(mask, cfg["basin"], res)
        print(f"  L={L_km:.1f} km  H={H_m:.0f} m  tc Kirpich={tc_k:.1f} h  Giandotti={tc_g:.1f} h")

        celdas, fuera, n_dem = celdas_0p1(mask, tr, crs, grid)
        print(f"  celdas 0.1°: {len(celdas)} (peso máx {celdas[0][2]:.3f}); "
              f"{fuera:,} celdas DEM fuera de la grilla del monitor")

        fechas, pr = fs.serie_cr2met(mask, tr, crs, a.anios[0], a.anios[1])
        print(f"  serie CR2MET: {len(fechas)} días {fechas[0]}…{fechas[-1]}  "
              f"máx {pr.max():.1f} mm/día")

        # Q_c con el MISMO binario y el MISMO dt que usará el ciclo vivo, para que
        # la definición del umbral no pueda divergir entre preparación y operación.
        forz = os.path.join(destino, f"{slug}_hist_forzante.json")
        with open(forz, "w") as f:
            json.dump({"fechas": [str(x) for x in fechas], "pr": [float(x) for x in pr]}, f)
        out = subprocess.run(
            [a.runner, "--forzante", forz, "--cn", str(cfg["cn"]),
             "--tc-h", f"{tc_g:.4f}", "--q-crit-p", str(a.q_crit_p)],
            capture_output=True, text=True)
        if out.returncode != 0:
            print(f"  runner falló: {out.stderr.strip()}", file=sys.stderr)
            continue
        rj = json.loads(out.stdout)
        print(f"  Q_c(p={a.q_crit_p}) = {rj['q_crit']:.3f} mm/día   "
              f"máx histórico {rj['q_max']:.2f} el {rj['etiqueta_max']}")

        art = {
            "slug": slug,
            "basin_kingston": cfg["basin"],
            "comunas": cfg["comunas"],
            "outlet_pedido": list(cfg["outlet"]),
            "area_km2": float(area),
            "L_km": float(L_km), "H_m": float(H_m),
            "tc_kirpich_h": float(tc_k), "tc_giandotti_h": float(tc_g),
            "cn": cfg["cn"], "tc_horas": float(tc_g), "lambda": "arido",
            "dt_dias": 1.0,
            "q_crit": rj["q_crit"],
            "q_crit_p": a.q_crit_p,
            "q_crit_origen": rj["q_crit_origen"],
            "sin_aforo": True,
            "hist_n": rj["n_steps"],
            "hist_desde": str(fechas[0]), "hist_hasta": str(fechas[-1]),
            "hist_q_max": rj["q_max"], "hist_q_max_fecha": rj["etiqueta_max"],
            "celdas_0p1": celdas,
            "grid_ref": os.path.abspath(grid),
            "generado_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nota": ("Q_c es cuantil de la serie SIMULADA (no hay aforo en estas cuencas): "
                     "la excedencia es 'cuán inusual es esta respuesta para ESTE modelo en "
                     "ESTA cuenca', no una magnitud absoluta. Producción Hortoniana SCS-CN "
                     "con parámetros estimados de la física de la cuenca, ninguno ajustado "
                     "contra un hidrograma observado."),
        }
        ruta = os.path.join(destino, f"{slug}.json")
        with open(ruta, "w") as f:
            json.dump(art, f, indent=1)
        print(f"  → {ruta}")
        resumen.append((slug, area, tc_g, rj["q_crit"], len(celdas)))

    print("\n=== resumen ===")
    print(f"{'cuenca':10s} {'área km²':>10s} {'tc h':>6s} {'Q_c mm/d':>9s} {'celdas':>7s}")
    for s, ar, tc, qc, nc in resumen:
        print(f"{s:10s} {ar:10,.0f} {tc:6.1f} {qc:9.3f} {nc:7d}")
    if not resumen:
        raise SystemExit("ninguna cuenca se preparó con éxito")
    return 0


if __name__ == "__main__":
    sys.exit(main())
