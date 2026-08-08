#!/usr/bin/env python3
"""QPF por ensemble (pySTEPS) sobre el feed de baja latencia — Fase B.

Es la pieza que convierte el sistema de **monitor de eventos** a **alerta
temprana**: hasta ahora toda la forzante era observada (IMERG ~5 h de latencia,
GOES ~30 min), así que el peligro se calculaba sobre lluvia que YA cayó y el
mejor caso posible era latencia cero. Este paso extrapola los últimos cuadros
del feed rápido hacia adelante y produce N realizaciones plausibles de las
próximas horas, que entran al motor por el mismo trait `Forcing` que todo lo
demás. La salida del motor sobre ese ensemble es una probabilidad de
excedencia — un pronóstico con incertidumbre explícita, no un número único.

Método: pySTEPS (Pulkkinen et al. 2019). Flujo óptico Lucas-Kanade sobre los
últimos cuadros → campo de movimiento; luego STEPS (Bowler et al. 2006):
cascada espectral multiescala + perturbaciones estocásticas, que es lo que
genera la dispersión entre miembros. Horizonte útil de un nowcast por
extrapolación: ~0-6 h, decayendo rápido; más allá manda el modelo numérico.

LÍMITES QUE HAY QUE DECIR (ninguno es un detalle):
  - Extrapolación, no física: un nowcast por advección mueve la lluvia que ya
    existe. NO genera convección nueva, y en un frontal en desarrollo eso
    subestima. El horizonte honesto es 1-3 h, no 6.
  - Hereda el sesgo de GOES QPE, que es un hidroestimador geoestacionario
    (sub/sobreestima según tipo de nube — ya documentado en las limitaciones
    del monitor). Un QPF sobre un QPE sesgado arrastra el sesgo.
  - La dispersión del ensemble mide incertidumbre DE LA EXTRAPOLACIÓN, no
    incertidumbre total: no incluye el error del propio QPE ni el de la
    susceptibilidad.
  - Sin verificación todavía: la probabilidad de excedencia que sale de aquí no
    está calibrada contra eventos reales. Usar `nowcast calibrate` con la
    temporada en modo sombra antes de darle valor probabilístico.

Convención de unidades: los pasos de los feeds son **mm acumulados en el paso**
(media hora), igual que IMERG/GOES. pySTEPS trabaja en dBR; la conversión de
ida y vuelta se hace aquí y los ceros se preservan como "sin lluvia".

Uso (necesita el venv con pysteps — ver README):
    ~/.venvs/nowcast-qpf/bin/python qpf_ensemble.py \
        --steps ~/nowcast-monitor/feeds/goes/steps \
        --out ~/nowcast-monitor/qpf --members 12 --leadsteps 6
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio

STEP = timedelta(minutes=30)
KEY = "%Y%m%dT%H%M"
# Bajo esto se considera sin lluvia. En mm/media hora: 0.05 mm ≈ 0.1 mm/h, el
# piso de detectabilidad razonable de un QPE geoestacionario.
UMBRAL_MM = 0.05


def dbr(x, umbral):
    """mm/paso → dBR, con el suelo como 'sin lluvia' (convención pySTEPS)."""
    piso = 10.0 * np.log10(umbral)
    return np.where(x >= umbral, 10.0 * np.log10(np.maximum(x, umbral)), piso - 5.0)


def desde_dbr(d, umbral):
    piso = 10.0 * np.log10(umbral)
    return np.where(d > piso - 1e-9, np.power(10.0, d / 10.0), 0.0)


def leer_cuadros(steps_dir, n, as_of=None):
    """Últimos n pasos REALES (ignora los marcados como hueco) en orden.

    `as_of` (clave YYYYmmddTHHMM) corta la serie ahí: sirve para REPLAY honesto
    —correr el QPF como si fuera ese instante, sin ver el futuro— que es la
    única forma de medir lead time retrospectivamente sin hacer trampa."""
    marcas = {os.path.basename(p)[len("gapmark_"):]
              for p in glob.glob(os.path.join(steps_dir, "gapmark_*"))}
    pares = []
    for p in sorted(glob.glob(os.path.join(steps_dir, "step_*.tif"))):
        k = re.search(r"step_(\d{8}T\d{4})", p).group(1)
        if k not in marcas and (as_of is None or k <= as_of):
            pares.append((k, p))
    if not pares:
        sys.exit("[qpf] el feed no tiene pasos reales")
    pares = pares[-n:]
    arrs = []
    for _, p in pares:
        with rasterio.open(p) as s:
            arrs.append(s.read(1).astype(float))
            perfil = s.profile
    # Continuidad: la extrapolación asume cuadros equiespaciados; si el feed
    # tiene saltos, el campo de movimiento sale mal y NO se avisa solo.
    claves = [k for k, _ in pares]
    ts = [datetime.strptime(k, KEY) for k in claves]
    saltos = sum(1 for a, b in zip(ts, ts[1:]) if (b - a) != STEP)
    return np.array(arrs), claves, perfil, saltos


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", required=True, help="dir de pasos del feed rápido (GOES)")
    ap.add_argument("--out", required=True, help="dir de salida del ensemble")
    ap.add_argument("--members", type=int, default=12)
    ap.add_argument("--leadsteps", type=int, default=6, help="pasos futuros (30 min c/u)")
    ap.add_argument("--frames", type=int, default=6, help="cuadros de entrada para el flujo óptico")
    ap.add_argument("--seed", type=int, default=42, help="reproducibilidad del ensemble")
    ap.add_argument("--umbral-mm", type=float, default=UMBRAL_MM)
    ap.add_argument("--as-of", help="correr como si fuera este instante (YYYYmmddTHHMM): "
                                    "ignora todo cuadro posterior. Para replay/lead time.")
    a = ap.parse_args()

    frames, claves, perfil, saltos = leer_cuadros(a.steps, a.frames, a.as_of)
    t_ultimo = datetime.strptime(claves[-1], KEY)
    print(f"[qpf] {len(frames)} cuadros de entrada, último {claves[-1]} · "
          f"grilla {frames.shape[1]}×{frames.shape[2]}")
    if saltos:
        print(f"[qpf] AVISO: {saltos} salto(s) temporal(es) entre los cuadros de entrada — "
              f"el campo de movimiento asume paso constante de 30 min y saldrá sesgado",
              file=sys.stderr)

    lluvia_reciente = float(np.nanmax(frames[-1]))
    if lluvia_reciente < a.umbral_mm:
        # Sin lluvia que extrapolar, un nowcast por advección no tiene nada que
        # mover: se dice y se sale, en vez de emitir un ensemble de ceros que
        # parecería un pronóstico de "no va a llover".
        print(f"[qpf] último cuadro seco (máx {lluvia_reciente:.3f} mm < {a.umbral_mm}) — "
              f"no hay campo que extrapolar; sin ensemble")
        json.dump({"status": "seco", "ultimo": claves[-1], "members": 0},
                  open(os.path.join(a.out, "qpf.json"), "w") if os.path.isdir(a.out)
                  else sys.stdout)
        return 0

    from pysteps import motion, nowcasts

    d = dbr(frames, a.umbral_mm)
    V = motion.get_method("lucaskanade")(d)
    st = nowcasts.get_method("steps")
    # kmperpixel: la grilla es 0.1° ≈ 11 km en latitud; se le pasa el valor
    # nominal porque STEPS lo usa para escalar las perturbaciones, no para
    # georreferenciar (eso lo lleva el perfil del ráster).
    salida = st(d, V, timesteps=a.leadsteps, n_ens_members=a.members,
                n_cascade_levels=6, precip_thr=10.0 * np.log10(a.umbral_mm),
                kmperpixel=11.0, timestep=30, seed=a.seed)

    mm = desde_dbr(salida, a.umbral_mm)          # (miembros, pasos, y, x)
    mm = np.nan_to_num(mm, nan=0.0, posinf=0.0, neginf=0.0)

    shutil.rmtree(a.out, ignore_errors=True)
    os.makedirs(a.out, exist_ok=True)
    perfil = dict(perfil, dtype="float32", count=1, compress="deflate")
    claves_fut = [(t_ultimo + STEP * (i + 1)).strftime(KEY) for i in range(a.leadsteps)]
    for m in range(mm.shape[0]):
        d_m = os.path.join(a.out, f"member_{m:02d}")
        os.makedirs(d_m, exist_ok=True)
        for i, k in enumerate(claves_fut):
            with rasterio.open(os.path.join(d_m, f"step_{k}.tif"), "w", **perfil) as dst:
                dst.write(mm[m, i].astype("float32"), 1)

    pico = mm.max(axis=(2, 3))                   # (miembros, pasos)
    meta = {
        "status": "ok",
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ultimo_observado": claves[-1],
        "pasos_futuros": claves_fut,
        "members": int(mm.shape[0]),
        "leadsteps": int(a.leadsteps),
        "frames_entrada": len(frames),
        "saltos_en_entrada": saltos,
        "seed": a.seed,
        # dispersión entre miembros del pico por paso: si es ~0 el ensemble no
        # está aportando incertidumbre y hay que mirar por qué antes de creerle
        "pico_mm_por_paso_media": [round(float(x), 3) for x in pico.mean(axis=0)],
        "pico_mm_por_paso_spread": [round(float(x), 3) for x in pico.std(axis=0)],
        "nota": ("QPF por extrapolación (pySTEPS/STEPS): mueve la lluvia existente, "
                 "NO genera convección nueva; horizonte honesto 1-3 h. Hereda el sesgo "
                 "de GOES QPE. La dispersión es incertidumbre de la extrapolación, no "
                 "total. Probabilidad SIN calibrar."),
    }
    json.dump(meta, open(os.path.join(a.out, "qpf.json"), "w"), indent=1, ensure_ascii=False)
    print(f"[qpf] {mm.shape[0]} miembros × {a.leadsteps} pasos → {a.out}")
    print(f"[qpf] pico medio por paso (mm/30min): "
          + " ".join(f"{x:.2f}" for x in pico.mean(axis=0)))
    print(f"[qpf] dispersión entre miembros:      "
          + " ".join(f"{x:.2f}" for x in pico.std(axis=0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
