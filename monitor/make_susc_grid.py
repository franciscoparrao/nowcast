#!/usr/bin/env python3
"""Susceptibilidad de terreno agregada a la grilla del monitor (SUSC_TIF).

El monitor corre con `--uniform-susc 1.0` (monitor de TIMING: el peligro se
reduce al factor de gatillo I-D). Este script produce el GeoTIFF que lo
convierte en monitor de peligro: la susceptibilidad real de terreno (modelos
por cuenca de paper1, 30 m) agregada a la MISMA grilla 0.1° de los feeds IMERG/
GOES, para que `nowcast run --susc` la acepte sin reproyectar nada en caliente.

NOTA HISTÓRICA: `config.env` decía "cuando exista el raster regenerado, apuntar
SUSC_TIF" — el raster nunca se perdió (R3-M11, resuelto 2026-07-13): vive en el
disco externo KINGSTON, que es de donde toda la cadena de análisis lo viene
leyendo. Lo que faltaba era agregarlo a la grilla del monitor.

## Por qué el criterio de agregación NO es "promedio"
Bajar 30 m → ~11 km es el cuello de botella ya documentado del backtest
distribuido (`docs/`/CLAUDE.md: "la susceptibilidad 30 m promediada a 5 km
pierde su filo", AUC≈0.48). Medido sobre el bbox piloto (121 celdas):

    criterio    rango de la celda   contraste (p75-p25)/mediana   celdas saturadas
    mean        0.197 – 0.397       0.146                          0
    p90         0.285 – 0.634       0.273                          0
    p95         0.302 – 0.709       0.245                          0
    max         0.360 – 0.965       0.105                          3

El promedio aplana (una celda de 11 km mezcla la quebrada con el fondo de valle)
y el máximo satura (basta un píxel inestable en 135 000 para dar ~1.0). El
**p90** es el que más separa celdas entre sí: responde "¿cuán susceptible es el
terreno REALMENTE expuesto de esta celda?" sin dejarse llevar por un píxel.

## Por qué imprime un ALERT_LEVEL y no solo el ráster
El motor alerta cuando `susceptibilidad × T(E) >= ALERT_LEVEL`, y `T(1)=0.5`
exactamente sobre la curva I-D. Con susceptibilidad uniforme 1.0, el
`ALERT_LEVEL=0.5` de `config.env` significa "alerta al cruzar Caine". Al
enchufar un campo de susceptibilidad <1 SIN tocar el umbral, el mismo número
pasa a exigir `T >= 0.5/susc`: con el promedio (mediana 0.325) eso es T>=1.54,
imposible — **el monitor quedaría mudo para siempre, en silencio**. Por eso
aquí el ráster y el umbral se derivan JUNTOS:

    ALERT_LEVEL = mediana(susc) × 0.5

que mantiene el punto de operación global (la celda de terreno TÍPICO sigue
alertando justo sobre Caine) y deja que el terreno solo REDISTRIBUYA: donde
amplifica se alerta bajo Caine, donde protege se exige más. Con p90 sobre el
piloto: peor terreno E=0.93 (−7%), típico E=1.00, mejor terreno E=1.76 (+76%).

El script ABORTA si alguna celda quedaría permanentemente muda (`ALERT_LEVEL >
susc_celda`, es decir su peligro máximo posible no alcanza el umbral ni con
lluvia infinita) — es el modo de falla silencioso que motivó todo esto.

Uso:
    python3 make_susc_grid.py --out ~/nowcast-monitor/susc_0p1.tif
    python3 make_susc_grid.py --stat mean --bbox="-70.75,-34.25,-69.75,-33.25"
"""

import argparse
import glob
import math
import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetch_goes_qpe import target_grid  # noqa: E402  (convenio de grilla IMERG)

KINGSTON = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors"
# Límite comunal oficial — misma fuente que comuna_impact.py; sirve de máscara
# de tierra para distinguir "nodata porque es mar" de "nodata porque falta el
# modelo" (R4-M2).
CENSUS_GPKG = os.path.expanduser(
    "~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")
RES = 0.1
SUB = 400          # subceldas por celda 0.1° → ~28 m, la resolución nativa
MIN_COVERAGE = 0.05  # bajo esto la celda no tiene terreno suficiente para hablar
# Techo de píxeles de la grilla fina. Un dominio regional (Maule+Ñuble-Biobío,
# 24×30 celdas) a SUB=400 son 115M px y ~4 copias en memoria; el muestreo se
# baja automáticamente hasta caber, y se INFORMA (el p90 es estable al
# submuestreo —verificado— pero el número usado no puede quedar oculto).
MAX_FINE_PX = 40_000_000


def source_rasters(basins):
    """Rásters de susceptibilidad de las cuencas pedidas (o todas las que haya).
    Cada cuenca trae el algoritmo que ganó su validación cruzada (XGBoost /
    RandomForest / Ridge), así que se busca por patrón, no por nombre fijo."""
    out = []
    for b in basins:
        hits = sorted(glob.glob(os.path.join(KINGSTON, b, "susceptibility_*.tif")))
        hits = [h for h in hits if "classes" not in os.path.basename(h)]
        if not hits:
            print(f"[susc] AVISO: cuenca {b} sin ráster de susceptibilidad", file=sys.stderr)
        out.extend(hits)
    return out


def fine_mosaic(paths, lons, lats, sub):
    """Mosaico a resolución nativa sobre el bbox destino. Donde dos cuencas se
    solapan gana el MÁXIMO (mismo criterio que susc_overlay.aggregate: el borde
    de una cuenca no debe borrar el dato de la vecina)."""
    fine_res = RES / sub
    W, H = len(lons) * sub, len(lats) * sub
    tr = from_origin(lons[0] - RES / 2, lats[-1] + RES / 2, fine_res, fine_res)
    acc = np.full((H, W), np.nan, dtype="float32")
    for p in paths:
        dst = np.full((H, W), np.nan, dtype="float32")
        with rasterio.open(p) as src:
            reproject(source=rasterio.band(src, 1), destination=dst,
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=tr, dst_crs="EPSG:4326",
                      src_nodata=src.nodata, dst_nodata=np.nan,
                      # nearest: agregamos nosotros más abajo con el estadístico
                      # elegido; un remuestreo promediador aquí ya aplanaría el
                      # dato antes de poder elegir el criterio.
                      resampling=Resampling.nearest)
        ok = np.isfinite(dst)
        acc[ok] = np.where(np.isfinite(acc[ok]), np.maximum(acc[ok], dst[ok]), dst[ok])
    return acc, tr


def aggregate(fine, nlat, nlon, stat, sub):
    """Bloques SUB×SUB → un valor por celda 0.1°, con el estadístico pedido
    calculado SOLO sobre los píxeles válidos del bloque."""
    b = fine.reshape(nlat, sub, nlon, sub).transpose(0, 2, 1, 3).reshape(nlat, nlon, sub * sub)
    valid = np.isfinite(b)
    cov = valid.mean(axis=2)
    fn = {"mean": np.mean, "max": np.max,
          "p90": lambda v: np.percentile(v, 90),
          "p95": lambda v: np.percentile(v, 95)}[stat]
    out = np.full((nlat, nlon), np.nan, dtype="float32")
    for i in range(nlat):
        for j in range(nlon):
            v = b[i, j][valid[i, j]]
            if v.size and cov[i, j] >= MIN_COVERAGE:
                out[i, j] = fn(v)
    return out, cov


def land_mask(lons, lats):
    """Máscara de TIERRA en la grilla destino, desde el límite comunal oficial
    (Comunal_CPV24 del Censo 2024, la misma fuente que comuna_impact.py).

    R4-M2: sin esto, `nodata` mezcla dos cosas que no se parecen. El motor lee
    nodata como susceptibilidad 0 → celda MUDA para siempre, y eso es correcto
    en el mar (no hay laderas que fallar) y una FALLA en tierra (hueco del
    modelo: cuenca transfer sin ráster, borde mal cubierto). El abort solo
    miraba las celdas con dato, así que un hueco interior pasaba con un print.

    Devuelve None si la cartografía no está en esta máquina (nodo de despliegue
    sin el censo): el llamador degrada a la comprobación vieja y lo dice, en
    vez de reventar o de afirmar una limpieza que no verificó."""
    if not os.path.exists(CENSUS_GPKG):
        return None
    try:
        import geopandas as gpd
        from rasterio.features import rasterize
    except ImportError:
        return None
    gdf = gpd.read_file(CENSUS_GPKG, layer="Comunal_CPV24", columns=["geometry"])
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    tr = from_origin(lons[0] - RES / 2, lats[-1] + RES / 2, RES, RES)
    # all_touched: una celda de 11 km que roza la costa TIENE laderas dentro;
    # exigir que el centroide caiga en tierra descartaría comunas costeras
    # enteras, que es justo donde el hueco de modelo es más probable.
    return rasterize([(g, 1) for g in gdf.geometry if g is not None],
                     out_shape=(len(lats), len(lons)), transform=tr,
                     fill=0, dtype=np.uint8, all_touched=True) == 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", default=os.environ.get("BBOX", "-70.75,-34.25,-69.75,-33.25"),
                    help="W,S,E,N en grados (default: el del piloto en config.env)")
    ap.add_argument("--basins", default="09_rio_maipo",
                    help="cuencas KINGSTON separadas por coma")
    ap.add_argument("--stat", choices=["p90", "p95", "mean", "max"], default="p90",
                    help="agregación 30 m → 0.1° (ver docstring: p90 por defecto)")
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-monitor/susc_0p1.tif"))
    ap.add_argument("--sub", type=int, default=SUB,
                    help=f"subceldas por lado y celda 0.1° (default {SUB} ≈ 28 m). Se baja "
                         f"automáticamente si la grilla fina no cabe en memoria.")
    ap.add_argument("--trigger-k", type=float, default=float(os.environ.get("K", 4.0)),
                    help="k del TriggerModel, para derivar el ALERT_LEVEL coherente")
    ap.add_argument("--allow-mute", action="store_true",
                    help="no abortar aunque alguna celda quede permanentemente muda")
    a = ap.parse_args()

    bbox = tuple(float(x) for x in a.bbox.split(","))
    lons, lats = target_grid(bbox)
    paths = source_rasters([b.strip() for b in a.basins.split(",") if b.strip()])
    if not paths:
        sys.exit("[susc] sin rásters de susceptibilidad de origen — ¿está montado KINGSTON?")
    print(f"[susc] bbox {bbox} → grilla {len(lons)}×{len(lats)} @ {RES}°")
    for p in paths:
        print(f"[susc]   origen: {os.path.relpath(p, KINGSTON)}")

    sub = a.sub
    while sub > 25 and len(lons) * sub * len(lats) * sub > MAX_FINE_PX:
        sub //= 2
    if sub != a.sub:
        print(f"[susc] submuestreo bajado {a.sub}→{sub} (~{RES/sub*111000:.0f} m, "
              f"{sub*sub} muestras/celda) para caber en {MAX_FINE_PX/1e6:.0f}M px de grilla fina")
    fine, _ = fine_mosaic(paths, lons, lats, sub)
    susc, cov = aggregate(fine, len(lats), len(lons), a.stat, sub)

    # el ráster se escribe north-up, igual que los feeds (lats vienen S→N)
    susc_nu = susc[::-1, :]
    cov_nu = cov[::-1, :]
    transform = from_origin(lons[0] - RES / 2, lats[-1] + RES / 2, RES, RES)
    prof = dict(driver="GTiff", height=len(lats), width=len(lons), count=1,
                dtype="float32", crs="EPSG:4326", transform=transform,
                nodata=np.nan, compress="deflate")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with rasterio.open(a.out, "w", **prof) as dst:
        # clip preserva NaN (nodata) y acota el estadístico a [0,1] (R4-L1:
        # antes había un nan_to_num(nan=np.nan) no-op que confundía)
        dst.write(np.clip(susc_nu, 0.0, 1.0), 1)

    v = susc_nu[np.isfinite(susc_nu)]
    nodata = ~np.isfinite(susc_nu)
    sin_dato = int(nodata.sum())
    print(f"[susc] {a.stat}: min {v.min():.3f} · mediana {np.median(v):.3f} · max {v.max():.3f}")
    print(f"[susc] cobertura de terreno por celda: {cov_nu.min()*100:.0f}%..{cov_nu.max()*100:.0f}%"
          + (f" · {sin_dato} celda(s) SIN DATO (el motor las lee como 0 → mudas)" if sin_dato else ""))
    print(f"[susc] → {a.out}")

    # --- R4-M2: ¿esas celdas mudas son mar (benigno) o tierra (hueco de modelo)?
    tierra = land_mask(lons, lats)
    huecos_tierra = 0
    if tierra is None:
        print("[susc] AVISO: sin cartografía comunal en esta máquina — no puedo "
              "distinguir nodata de mar vs hueco de modelo (R4-M2 sin verificar)")
    else:
        # land_mask ya rasteriza con el MISMO transform north-up del ráster de
        # salida, así que comparte orientación con susc_nu: no se voltea.
        huecos = nodata & tierra
        huecos_tierra = int(huecos.sum())
        mar = int((nodata & ~tierra).sum())
        # Dos causas distintas con remedios distintos: sin modelo (cobertura 0 →
        # falta el ráster de esa zona) vs bajo el umbral de cobertura (hay
        # modelo, pero la celda es casi toda mar/fuera y MIN_COVERAGE la calla).
        sin_modelo = huecos & (cov_nu == 0)
        bajo_umbral = huecos & (cov_nu > 0)
        n_sin, n_bajo = int(sin_modelo.sum()), int(bajo_umbral.sum())
        print(f"[susc] de las {sin_dato} celdas sin dato: {mar} en el mar (correcto, "
              f"no hay laderas) · {huecos_tierra} en TIERRA")
        if huecos_tierra:
            latsr = lats[::-1]
            for nombre, m, n, remedio in (
                    ("SIN MODELO (cobertura 0)", sin_modelo, n_sin,
                     "falta el ráster de susceptibilidad de esa zona: añadir su cuenca "
                     "a --basins, o recortar el bbox para no prometer cobertura que no hay"),
                    (f"bajo MIN_COVERAGE ({MIN_COVERAGE:.0%})", bajo_umbral, n_bajo,
                     "hay modelo pero la celda es casi toda mar/fuera; bajar MIN_COVERAGE "
                     "si esa franja costera importa")):
                if not n:
                    continue
                ys, xs = np.where(m)
                muestra = ", ".join(f"({lons[x]:.2f},{latsr[y]:.2f})"
                                    for y, x in list(zip(ys, xs))[:5])
                print(f"[susc]   {n} {nombre} → {muestra}" + (" …" if n > 5 else ""))
                print(f"[susc]     remedio: {remedio}")

    # --- umbral coherente (ver docstring) ------------------------------------
    med = float(np.median(v))
    alert = round(med * 0.5, 3)
    k = a.trigger_k

    def e_for(s):
        """Exceedance I-D a la que esa celda alcanza el umbral (None = nunca)."""
        t = alert / s
        return 1.0 + math.log(t / (1.0 - t)) / k if t < 1.0 else None

    mudas = [float(x) for x in v if e_for(float(x)) is None]
    e_lo, e_med, e_hi = e_for(float(v.max())), e_for(med), e_for(float(v.min()))
    print(f"\n[susc] ALERT_LEVEL coherente = mediana × 0.5 = {alert}")
    print(f"[susc]   celda de PEOR terreno (susc {v.max():.3f}): alerta en E={e_lo:.2f} "
          f"({(e_lo-1)*100:+.0f}% vs Caine)")
    print(f"[susc]   celda TÍPICA          (susc {med:.3f}): alerta en E={e_med:.2f} "
          f"({(e_med-1)*100:+.0f}%)")
    print(f"[susc]   celda de MEJOR terreno (susc {v.min():.3f}): "
          + (f"alerta en E={e_hi:.2f} ({(e_hi-1)*100:+.0f}%)" if e_hi else "NUNCA alerta"))
    print(f"[susc] Con el ALERT_LEVEL actual (0.5) en cambio, la celda típica exigiría "
          f"T>={0.5/med:.2f}" + (" — IMPOSIBLE, monitor mudo" if 0.5 / med >= 1 else ""))
    # Las dos formas de quedar muda son distintas y ambas abortan (R4-M2): por
    # umbral (tiene dato, pero su peligro máximo no alcanza ALERT_LEVEL) o por
    # nodata EN TIERRA (el motor la lee como 0). El mar no cuenta: ahí la mudez
    # es la respuesta correcta.
    if (mudas or huecos_tierra) and not a.allow_mute:
        motivos = []
        if mudas:
            motivos.append(f"{len(mudas)} por umbral (su peligro máximo no alcanza "
                           f"{alert} ni con lluvia infinita) — probar otro --stat")
        if huecos_tierra:
            motivos.append(f"{huecos_tierra} por falta de modelo en tierra (nodata → "
                           f"el motor las lee como 0) — añadir la cuenca que cubre esa "
                           f"zona a --basins, o achicar el bbox")
        sys.exit(f"[susc] ABORTA: celdas permanentemente mudas → " + "; ".join(motivos)
                 + ". Usar --allow-mute si es deliberado.")
    print(f"\n[susc] En config.env:\n  SUSC_TIF=\"{a.out}\"\n  ALERT_LEVEL={alert}")


if __name__ == "__main__":
    main()
