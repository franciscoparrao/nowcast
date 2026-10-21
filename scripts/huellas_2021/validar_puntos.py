#!/usr/bin/env python3
"""Validación del runout ABM (runout_ondemand) contra los puntos del catálogo
nacional de SERNAGEOMIN del temporal del 29–31 de enero de 2021 (RM).

Sin huellas mapeadas no hay IoU; lo que sí se puede medir es si la huella
simulada ALCANZA los lugares donde hubo flujos. Para cada ventana (un grupo de
puntos), preset de parámetros y semilla:

  acierto(tol) = fracción de puntos con una celda de huella a ≤ tol metros
  nulo de cauce(tol) = lo mismo para puntos al azar en la franja junto a los
      cauces del propio runner (streams.tif), con ancho = percentil 90 de la
      distancia de los puntos reales al cauce. Es el nulo exigente: cualquier
      modelo que siga el drenaje le gana al azar uniforme; aquí se pregunta si
      la huella elige las quebradas que fallaron entre todas las del valle.
  nulo uniforme(tol) = fracción de la ventana a ≤ tol de la huella.
  lift = acierto / nulo de cauce.

Entradas fijas (supuestos, ver informe): lluvia diaria 29/30/31-ene en hora
local de la estación más cercana (agromet, data/agromet.db), isoterma 4500 m
(serie H0 4274–5155 m esos días, menos su sesgo de ~+250 m), sedimento 0.5,
susceptibilidad RandomForest del Maipo.
Salida: data/huellas_2021/runout/<corrida>/ y data/huellas_2021/validacion_puntos.json
"""
import json, os, subprocess, sys
import numpy as np
import rasterio
from rasterio.warp import transform as warp_xy
from scipy import ndimage

H = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(H, "..", ".."))
D = os.path.join(ROOT, "data", "huellas_2021")
BIN = os.path.join(ROOT, "target", "release", "examples", "runout_ondemand")
SUSC = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif"
LLUVIA = {"guayacan": "14.0,67.0,16.6", "el_colorado": "2.5,14.4,12.3", "la_platina": "24.1,28.6,2.3"}
VENTANAS = [  # nombre, lon, lat, lado km, lluvias a probar
    ("cajon", -70.15, -33.77, 22.0, ["guayacan"]),
    ("las_vertientes", -70.33, -33.65, 12.0, ["guayacan"]),
    ("el_arrayan", -70.42, -33.35, 14.0, ["el_colorado", "la_platina"]),
]
PRESETS = ["chanaral_enhanced", "de"]
SEMILLAS = [1, 2, 3]
TOLS = [0, 60, 150]
RNG = np.random.default_rng(20210130)


def correr(nombre, lon, lat, lado, lluvia, preset, semilla):
    out = os.path.join(D, "runout", f"{nombre}_{lluvia}_{preset}_s{semilla}")
    if not os.path.exists(os.path.join(out, "stats.json")):
        cmd = [BIN, f"--lon={lon}", f"--lat={lat}", "--size-km", str(lado), "--rain-mm-per-day", LLUVIA[lluvia],
               "--isotherm-m", "4500", "--sediment", "0.5", "--susceptibility", SUSC, "--params", preset,
               "--seed", str(semilla), "--out", out, "--write-layers"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit(f"runner falló en {out}: {p.stderr[-300:]}")
    return out


def main():
    gj = json.load(open(os.path.join(D, "puntos_enero2021.geojson")))["features"]
    gj = [f for f in gj if f["properties"]["DIA_EVENTO"] != "5 al 7"]  # evento de febrero, otro gatillo
    filas, usados = [], set()
    for nombre, lon, lat, lado, lluvias in VENTANAS:
        for lluvia in lluvias:
            for preset in PRESETS:
                for s in SEMILLAS:
                    out = correr(nombre, lon, lat, lado, lluvia, preset, s)
                    with rasterio.open(os.path.join(out, "runout_footprint.tif")) as r:
                        fp = r.read(1) == 1; T = r.transform; crs = r.crs
                    with rasterio.open(os.path.join(out, "streams.tif")) as r:
                        st = r.read(1) > 0
                    px = T.a
                    xs, ys = warp_xy("EPSG:4326", crs, [f["properties"]["LONGITUD"] for f in gj],
                                     [f["properties"]["LATITUD"] for f in gj])
                    rc = [(~T) * (x, y) for x, y in zip(xs, ys)]
                    dentro = [(int(r_), int(c_), f["properties"]["COD_RM"]) for (c_, r_), f in zip(rc, gj)
                              if 0 <= r_ < fp.shape[0] and 0 <= c_ < fp.shape[1]]
                    if not dentro:
                        continue
                    usados |= {d[2] for d in dentro}
                    d_fp = ndimage.distance_transform_edt(~fp) * px
                    d_st = ndimage.distance_transform_edt(~st) * px
                    dist_pts_st = np.array([d_st[r_, c_] for r_, c_, _ in dentro])
                    ancho = float(np.percentile(dist_pts_st, 90))
                    franja = np.argwhere(d_st <= ancho)
                    nul = franja[RNG.choice(len(franja), size=min(3000, len(franja)), replace=False)]
                    fila = {"ventana": nombre, "lluvia": lluvia, "preset": preset, "semilla": s,
                            "n_puntos": len(dentro), "huella_frac": float(fp.mean()),
                            "ancho_franja_m": ancho}
                    for t in TOLS:
                        ac = float(np.mean([d_fp[r_, c_] <= t for r_, c_, _ in dentro]))
                        nc = float(np.mean(d_fp[nul[:, 0], nul[:, 1]] <= t))
                        nu = float(np.mean(d_fp <= t))
                        fila[f"acierto_{t}"] = ac; fila[f"nulo_cauce_{t}"] = nc; fila[f"nulo_unif_{t}"] = nu
                    filas.append(fila)
    json.dump({"filas": filas, "puntos_usados": len(usados), "puntos_total": len(gj)},
              open(os.path.join(D, "validacion_puntos.json"), "w"), indent=1)
    # resumen: media sobre semillas
    print(f"puntos dentro de alguna ventana: {len(usados)} de {len(gj)}")
    print(f"{'ventana':15s} {'lluvia':12s} {'preset':18s} {'n':>3s} {'huella':>7s} "
          + " ".join(f"{'ac'+str(t):>6s} {'nc'+str(t):>6s} {'lift'+str(t):>7s}" for t in TOLS))
    claves = sorted({(f["ventana"], f["lluvia"], f["preset"]) for f in filas}, key=lambda k: (k[0], k[1], k[2]))
    for k in claves:
        g = [f for f in filas if (f["ventana"], f["lluvia"], f["preset"]) == k]
        m = lambda c: float(np.mean([f[c] for f in g]))
        linea = f"{k[0]:15s} {k[1]:12s} {k[2]:18s} {g[0]['n_puntos']:3d} {m('huella_frac'):7.1%} "
        for t in TOLS:
            nc = m(f"nulo_cauce_{t}")
            linea += f"{m(f'acierto_{t}'):6.2f} {nc:6.2f} {(m(f'acierto_{t}') / nc if nc > 0 else float('nan')):7.2f} "
        print(linea)


if __name__ == "__main__":
    main()
