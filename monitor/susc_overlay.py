#!/usr/bin/env python3
"""Capa de análisis nivel-3: peligro pronosticado × susceptibilidad real.

Cruza el `hazard_max` del pronóstico GFS (excedencia del gatillo I-D, la
mitad CLIMÁTICA) con los modelos de susceptibilidad por cuenca del usuario
(paper1, XGBoost/RandomForest a 30 m — la mitad de TERRENO), agregados a la
grilla 0.1° del dominio. El producto responde "¿dónde preocuparse de verdad?":
lluvia gatillante sobre ladera que puede fallar.

ROTULADO HONESTO: es una capa de ANÁLISIS para GIS, no el índice operacional
del monitor (que corre con susceptibilidad uniforme durante el evento — no se
cambia la semántica del umbral en caliente). La agregación 30 m → 0.1° se
entrega en dos sabores porque ninguno es inocente: MEAN diluye el filo de las
laderas (hallazgo documentado del backtest distribuido) y MAX sobre-pesa el
píxel peor. Donde el dominio no tiene cuenca modelada queda NaN (desconocido),
nunca 0 (que se leería como "seguro").

Uso:
    python3 susc_overlay.py --forecast ~/nowcast-forecast/latest [--domain X]

Salidas en <forecast>/<dominio>/:
    susc_mean_0p1.tif / susc_max_0p1.tif      terreno agregado
    hazard_x_susc_mean.tif / hazard_x_susc_max.tif   el cruce (con la
        variante regional-a5.5 del hazard, la calibrada para forzante gruesa)
"""

import argparse
import glob
import os
import sys

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

KINGSTON = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors"
# Dominio → rasters de cuenca que lo cubren (rutas relativas a KINGSTON).
DOMAIN_BASINS = {
    # 3 cuencas PROPIAS (sin transfer) — Atacama es la mejor cubierta de los
    # dominios nuevos: Salado (norte), Copiapó (centro, el análogo histórico
    # del evento 2015 que calibra swarm-abm), Huasco (sur).
    "atacama": ["04_rio_salado/susceptibility_XGBoost.tif",
                "05_rio_copiapo/susceptibility_Ridge.tif",
                "06_rio_huasco/susceptibility_XGBoost.tif"],
    "coquimbo": ["07_rio_elqui/susceptibility_XGBoost.tif",
                 "08_rio_limari/susceptibility_XGBoost.tif"],
    "rm": ["09_rio_maipo/susceptibility_RandomForest.tif"],
    "nuble-biobio": ["12_rio_biobio/susceptibility_RandomForest.tif"],
    # araucania: ninguna de las 15 cuencas del paper1 la cubre. Se usa una capa
    # por TRANSFERENCIA desde la cuenca adyacente del Biobio (RandomForest del
    # paper1 re-aplicado sobre factores DEM de Araucania, Copernicus GLO-90).
    # TRANSFERENCIA SIN VALIDAR en destino — ver susceptibility_transfer_meta.json.
    "araucania": ["16_araucania_transfer/susceptibility_transfer_biobio.tif"],
    # Barrido Coquimbo→Los Ríos: mosaico de TODAS las cuencas continuas
    # disponibles (huecos entre cuencas quedan NaN — Aconcagua,
    # Itata, Imperial/Toltén no tienen modelo propio todavía).
    "chile-centro-sur": [
        "07_rio_elqui/susceptibility_XGBoost.tif",
        "08_rio_limari/susceptibility_XGBoost.tif",
        "09_rio_maipo/susceptibility_RandomForest.tif",
        "10_rio_rapel/susceptibility_RandomForest.tif",
        "11_rio_maule/susceptibility_Ridge.tif",
        "12_rio_biobio/susceptibility_RandomForest.tif",
        "16_araucania_transfer/susceptibility_transfer_biobio.tif",
        "13_rio_bueno/susceptibility_XGBoost.tif",
        # Cordillera de la Costa Valparaíso→Los Ríos por TRANSFERENCIA del RF
        # del Biobío (SIN VALIDAR, fuera de dominio litológico — ver
        # 17_costa_transfer/susceptibility_transfer_meta.json). Va al FINAL de
        # la lista para que las cuencas reales ganen en solapes; OJO: aggregate()
        # mosaica con MÁXIMO en solapes, así que el orden solo decide qué capa
        # "siembra" la celda — en solape una transfer alta puede superar a la
        # cuenca real. Los 3 tramos N/C/S se solapan 0.05° entre sí.
        "17_costa_transfer/susceptibility_transfer_biobio_norte.tif",
        "17_costa_transfer/susceptibility_transfer_biobio_centro.tif",
        "17_costa_transfer/susceptibility_transfer_biobio_sur.tif",
    ],
}
HAZARD_VARIANT = "hazard_max_regional-a5.5.tif"


def aggregate(basins, ref_profile, resampling):
    """Reproyecta/agrega cada cuenca a la grilla del dominio y mosaica
    (celda válida gana; solapes → máximo, conservador)."""
    out = np.full((ref_profile["height"], ref_profile["width"]), np.nan, "float32")
    for path in basins:
        src_path = os.path.join(KINGSTON, path)
        if not os.path.exists(src_path):
            print(f"  AVISO: falta {src_path}, se omite", file=sys.stderr)
            continue
        with rasterio.open(src_path) as src:
            dst = np.full_like(out, np.nan)
            reproject(
                source=rasterio.band(src, 1), destination=dst,
                dst_transform=ref_profile["transform"], dst_crs=ref_profile["crs"],
                resampling=resampling, dst_nodata=np.nan,
            )
        valid = np.isfinite(dst)
        out[valid] = np.where(np.isfinite(out[valid]),
                              np.maximum(out[valid], dst[valid]), dst[valid])
    return np.clip(out, 0.0, 1.0, out=out, where=np.isfinite(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", required=True,
                    help="directorio del ciclo (p.ej. ~/nowcast-forecast/latest)")
    ap.add_argument("--domain", action="append",
                    help="repetible; default: todos los cubiertos")
    a = ap.parse_args()

    fdir = os.path.realpath(os.path.expanduser(a.forecast))
    domains = a.domain or [d for d in DOMAIN_BASINS
                           if os.path.isdir(os.path.join(fdir, d))]
    for dom in domains:
        basins = DOMAIN_BASINS.get(dom, [])
        ddir = os.path.join(fdir, dom)
        hz_path = os.path.join(ddir, HAZARD_VARIANT)
        if not os.path.exists(hz_path):
            print(f"{dom}: sin {HAZARD_VARIANT}, se omite", file=sys.stderr)
            continue
        if not basins:
            print(f"{dom}: sin cuenca modelada en paper1 — sin capa de terreno "
                  f"(transfer pendiente, paper4)")
            continue
        with rasterio.open(hz_path) as src:
            hazard = src.read(1)
            profile = dict(src.profile)
        profile.update(dtype="float32", nodata=np.nan)

        for tag, resampling in [("mean", Resampling.average), ("max", Resampling.max)]:
            susc = aggregate(basins, profile, resampling)
            with rasterio.open(os.path.join(ddir, f"susc_{tag}_0p1.tif"), "w",
                               **profile) as dst:
                dst.write(susc, 1)
            cross = (hazard * susc).astype("float32")
            with rasterio.open(os.path.join(ddir, f"hazard_x_susc_{tag}.tif"), "w",
                               **profile) as dst:
                dst.write(cross, 1)
            cov = float(np.isfinite(susc).mean())
            peak = float(np.nanmax(cross)) if np.isfinite(cross).any() else float("nan")
            print(f"{dom} [{tag}]: cobertura de cuenca {cov*100:.0f}% | "
                  f"susc máx {np.nanmax(susc):.2f} | hazard×susc pico {peak:.2f}")

        # --- Producto de ALTA RESOLUCIÓN (30 m, por cuenca) --------------------
        # El cruce en la grilla NATIVA de la susceptibilidad: el gatillo
        # climático (suave por naturaleza: GFS 25 km → 0.1°) se interpola
        # bilineal sobre los 30 m y se multiplica por el terreno, que aporta
        # todo el detalle espacial (quebradas, laderas). Es el mapa "dónde
        # preocuparse" que sí se lee en un GIS — rotulado: la estructura fina
        # viene 100% del terreno; el clima sigue siendo de 25 km.
        for path in basins:
            src_path = os.path.join(KINGSTON, path)
            if not os.path.exists(src_path):
                continue
            # nombre único por ARCHIVO (no por dir de cuenca): los transfers por
            # tramos comparten dir y colisionaban sobrescribiéndose entre sí.
            stem = os.path.splitext(os.path.basename(path))[0]
            basin = path.split("/")[0]
            if stem not in (f"susceptibility_XGBoost", "susceptibility_RandomForest",
                            "susceptibility_Ridge"):
                basin = f"{basin}_{stem.split('_')[-1]}"
            with rasterio.open(src_path) as s:
                susc30 = s.read(1)
                p30 = dict(s.profile)
                hz30 = np.full(susc30.shape, np.nan, "float32")
                with rasterio.open(hz_path) as h:
                    reproject(
                        source=rasterio.band(h, 1), destination=hz30,
                        dst_transform=p30["transform"], dst_crs=p30["crs"],
                        resampling=Resampling.bilinear, dst_nodata=np.nan,
                    )
            cross30 = (hz30 * np.clip(susc30, 0.0, 1.0)).astype("float32")
            p30.update(dtype="float32", nodata=np.nan, compress="deflate",
                       predictor=3, tiled=True)
            out30 = os.path.join(ddir, f"hazard_x_susc_30m_{basin}.tif")
            with rasterio.open(out30, "w", **p30) as dst:
                dst.write(cross30, 1)
            n_hot = int((cross30 > 0.5).sum())
            # área real de la celda desde el transform (30 m u 90 m según cuenca)
            cell_km2 = abs(p30["transform"].a * p30["transform"].e) * 1e-6
            px = round(abs(p30["transform"].a))
            print(f"{dom} [{px}m {basin}]: pico {np.nanmax(cross30):.2f} | "
                  f"{n_hot} celdas de {px} m sobre 0.5 "
                  f"({n_hot * cell_km2:.1f} km²) → {os.path.basename(out30)}")


if __name__ == "__main__":
    main()
