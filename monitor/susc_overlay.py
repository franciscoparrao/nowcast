#!/usr/bin/env python3
"""Capa de análisis nivel-3: peligro pronosticado × susceptibilidad real.

Cruza el `hazard_max` del peor pronóstico disponible entre GFS, ECMWF e ICON
(excedencia del gatillo I-D, la mitad CLIMÁTICA) con los modelos de susceptibilidad por cuenca del usuario
(paper1, 15 cuencas: XGBoost en 8, RandomForest en 4, Ridge en 3, elegido por
validación cruzada en cada cuenca — a 30 m, la mitad de TERRENO), agregados a la
grilla 0.1° del dominio. El producto responde "¿dónde preocuparse de verdad?":
lluvia gatillante sobre ladera que puede fallar.

ROTULADO HONESTO: es una capa de ANÁLISIS para GIS, no el índice operacional
del monitor. Desde 2026-07-29 el monitor TAMBIÉN cruza terreno, pero por otra
vía y a propósito: `make_susc_grid.py` produce un ráster estático en la grilla
del monitor (p90 por celda, con su ALERT_LEVEL derivado) que se genera UNA vez
por nodo — la semántica del umbral no se cambia en caliente. La agregación
30 m → 0.1° se
entrega en dos sabores porque ninguno es inocente: MEAN diluye el filo de las
laderas (hallazgo documentado del backtest distribuido) y MAX sobre-pesa el
píxel peor. Donde el dominio no tiene cuenca modelada queda NaN (desconocido),
nunca 0 (que se leería como "seguro").

Uso:
    python3 susc_overlay.py --forecast ~/nowcast-forecast/latest [--domain X]

Salidas en <forecast>/<dominio>/:
    susc_mean_0p1.tif / susc_max_0p1.tif      terreno agregado
    hazard_x_susc_mean.tif / hazard_x_susc_max.tif   el cruce

VARIANTE DE UMBRAL POR FUENTE (2026-07-16): GFS y ECMWF se reproyectan por
bilineal a 0.25°/0.1° — la interpolación diluye los picos de intensidad, así
que usan `regional-a5.5` (intercepto I-D calibrado para esa forzante gruesa,
ver docstring de forecast_gfs.py). ICON se reproyecta por vecino-más-cercano
desde su malla nativa ~13 km (ver fetch_icon.py) — no diluye picos, así que
aplicarle el mismo intercepto permisivo satura el hazard a 1.0 casi en
cualquier evento con lluvia real (verificado: 272/275 comunas en ALTO,
peak_terreno del watchlist saturado 0.999-1.0 en el top-20 completo, sin
poder discriminar nada). Para ICON se usa `caine-global` (a=14.82, el
intercepto sin diluir), que sigue alto durante este evento pero SÍ discrimina
entre dominios (0.61-0.99 en vez de 1.0 plano). Ninguna de las dos variantes
está "más validada" que la otra para ICON — es un ajuste razonado de
consistencia de resolución, no una recalibración con backtesting propio;
queda pendiente evaluarlo contra inventario cuando termine el evento.
"""

import argparse
import glob
import os
import sys

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

# Sobreescribible por entorno para el autotest (`selftest.py --susc-overlay`);
# en operación NO se define y vale el disco externo.
KINGSTON = os.environ.get(
    "KINGSTON_FACTORS",
    "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors")
# Dominio → rasters de cuenca que lo cubren (rutas relativas a KINGSTON).
DOMAIN_BASINS = {
    # 3 cuencas PROPIAS (sin transfer) — Atacama es la mejor cubierta de los
    # dominios nuevos: Salado (norte), Copiapó (centro, el análogo histórico
    # del evento 2015 que calibra swarm-abm), Huasco (sur).
    "atacama": ["04_rio_salado/susceptibility_XGBoost.tif",
                "05_rio_copiapo/susceptibility_Ridge.tif",
                "06_rio_huasco/susceptibility_XGBoost.tif"],
    "coquimbo": ["07_rio_elqui/susceptibility_XGBoost.tif",
                 "08_rio_limari/susceptibility_XGBoost.tif",
                 # Provincia del Choapa (Illapel/Salamanca/Los Vilos): sin cuenca
                 # propia -- TRANSFERENCIA (sustituto RandomForest, R2=0.916
                 # contra Limari, 14/15 factores, sin s_CSI). Ver
                 # 18_choapa_transfer/susceptibility_transfer_meta.json.
                 "18_choapa_transfer/susceptibility_transfer_limari.tif"],
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
        # Provincia del Choapa — la MISMA capa de transferencia que ya estaba
        # en el dominio `coquimbo`. Faltaba acá (hallazgo 2026-07-18): este es
        # el barrido que usa select_watchlist.py para TODA la Región IV
        # (domain_for_region manda region 4 a `chile-centro-sur`, no a
        # `coquimbo`), así que sin esta línea Illapel quedaba EXCLUIDA de la
        # watchlist por "sin dato en el punto" y Canela caía al relleno
        # contaminado de 08_rio_limari en vez de a su transferencia propia.
        "18_choapa_transfer/susceptibility_transfer_limari.tif",
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
# GFS/ECMWF: bilineal, forzante diluida -> intercepto regional permisivo.
# ICON: vecino-mas-cercano, ~13 km nativo sin diluir -> intercepto global sin
# descuento (ver nota de módulo). Mismo criterio en comuna_impact.py.
from variantes import archivo_max  # noqa: E402  (una sola copia de la regla)

HAZARD_VARIANT_BY_SOURCE = {m: archivo_max(m) for m in ("gfs", "ecmwf", "icon")}
HAZARD_COMBINED = "hazard_max_worst_available.tif"
HAZARD_SOURCES = [("gfs", ""), ("ecmwf", "ecmwf"), ("icon", "icon")]


def raster_coverage_warning(path, lon, lat, window_km=3.0):
    """Heurística de QA reutilizable: ¿este punto cae en la cuenca REAL
    delineada del raster de susceptibilidad, o en el RELLENO del rectángulo
    DEM descargado alrededor de ella?

    Los rasters de paper1_susceptibilidad cubren el rectángulo completo de
    la descarga, no solo la cuenca delineada — no hay un polígono de cuenca
    real en el dataset para recortar. Fuera de la cuenca el valor es
    interpolación/extrapolación de borde del modelo, no señal de terreno.
    Hallazgo 2026-07-17: confirmado por inspección visual en 7 localidades
    de 4 cuencas distintas (Canela y Coquimbo en el reporte de Coquimbo/
    Choapa; Capitán Pastene y Contulmo en 12_rio_biobio; Caldera y
    Vallenar-ICON en 05_rio_copiapo; La Higuera-ICON en 07_rio_elqui) — en
    todos los casos el punto cae geográficamente fuera de la mancha de
    drenaje real dibujada por el raster, en una franja plana o con un
    artefacto de enrutamiento de flujo diagonal (relleno calculado sobre el
    rectángulo, no sobre la cuenca).

    Combina dos señales baratas (calibradas contra esas 7 localidades más 6
    localidades reales de control en 08_rio_limari/18_choapa_transfer/
    09_rio_maipo):
      - varianza local de textura en una ventana de `window_km` (el relleno
        es casi plano; la cuenca real tiene relieve, incluso en fondo de
        valle)
      - distancia del punto al borde del rectángulo del raster (el relleno
        por definición toca el borde de la descarga)

    LIMITACIÓN CONOCIDA, documentada a propósito: fondo de valle plano REAL
    cerca del borde de la ventana (ej. Las Cabras/Requínoa en 09_rio_maipo,
    terreno agrícola genuinamente plano del Valle Central) da FALSO
    POSITIVO — es estadísticamente indistinguible del relleno usando solo
    estas 2 señales baratas. La única distinción robusta que se encontró
    fue la inspección visual contra la forma real del drenaje, que no es
    automatizable sin un polígono de cuenca real. Por eso esto es un AVISO
    para revisión humana (igual que calibration_domain_warning o
    mass_balance_ratio en runout_localidad.py), NO un filtro duro: no
    reasignar ni descartar una localidad solo por `sospechoso=True` sin
    mirar el mapa.
    """
    from pyproj import Transformer as _Transformer

    with rasterio.open(path) as s:
        crs = s.crs
        res = s.res[0]
        left, bottom, right, top = s.bounds
        t = _Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = t.transform(lon, lat)
        dist_borde_km = min(x - left, right - x, y - bottom, top - y) / 1000.0
        col = int((x - left) / res)
        row = int((top - y) / res)
        half = max(1, int(window_km * 1000 / res))
        row0, row1 = max(0, row - half), min(s.height, row + half)
        col0, col1 = max(0, col - half), min(s.width, col + half)
        win = s.read(1, window=((row0, row1), (col0, col1)))

    finite = win[np.isfinite(win)]
    std_local = float(np.std(finite)) if finite.size else float("nan")
    sospechoso = bool(
        np.isfinite(std_local)
        and (std_local < 0.01
             or (std_local < 0.03 and dist_borde_km < 20)
             or dist_borde_km < 6)
    )
    nota = (
        f"varianza local {std_local:.3f} a {dist_borde_km:.1f} km del borde "
        "de la ventana descargada — " + (
            "posible RELLENO fuera de la cuenca real (no cuenca real): "
            "revisar visualmente antes de confiar en el valor de este punto "
            "(ver docstring de raster_coverage_warning en susc_overlay.py)"
            if sospechoso else "sin señal de relleno"
        )
    )
    return {"sospechoso": sospechoso, "std_local": round(std_local, 4),
            "distancia_borde_km": round(dist_borde_km, 1), "nota": nota}


def combined_hazard(ddir):
    """Maximo celda-a-celda entre GFS, ECMWF e ICON disponibles."""
    out = None
    profile = None
    used = []
    for label, subdir in HAZARD_SOURCES:
        variant = HAZARD_VARIANT_BY_SOURCE[label]
        path = os.path.join(ddir, subdir, variant) if subdir else os.path.join(ddir, variant)
        if not os.path.exists(path):
            continue
        with rasterio.open(path) as src:
            arr = src.read(1).astype("float32")
            if profile is None:
                profile = dict(src.profile)
                out = np.full(arr.shape, np.nan, "float32")
            elif arr.shape != out.shape:
                print(f"  AVISO: {label} tiene grilla {arr.shape}, se omite en combinado", file=sys.stderr)
                continue
        valid = np.isfinite(arr)
        out[valid] = np.where(np.isfinite(out[valid]), np.maximum(out[valid], arr[valid]), arr[valid])
        used.append(label)
    if out is None:
        return None, []
    profile.update(dtype="float32", nodata=np.nan)
    out_path = os.path.join(ddir, HAZARD_COMBINED)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)
    return out_path, used


def aggregate(basins, ref_profile, resampling):
    """Reproyecta/agrega cada cuenca a la grilla del dominio y mosaica
    (celda válida gana; solapes → máximo, conservador).

    Devuelve (mosaico, n_cuencas_leidas). Una cuenca ausente se omite con
    aviso (cobertura parcial es legítima); que NO se lea NINGUNA lo decide el
    llamador, porque un mosaico todo-NaN escrito en silencio se propaga como
    "sin terreno" a comuna_impact y la watchlist sin que nadie se entere
    (mismo patrón de los 23 días de `target/`: el disco de KINGSTON desmontado
    dejaba el ciclo "exitoso" y sin capa de terreno)."""
    out = np.full((ref_profile["height"], ref_profile["width"]), np.nan, "float32")
    n_found = 0
    for path in basins:
        src_path = os.path.join(KINGSTON, path)
        if not os.path.exists(src_path):
            print(f"  AVISO: falta {src_path}, se omite", file=sys.stderr)
            continue
        n_found += 1
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
    return np.clip(out, 0.0, 1.0, out=out, where=np.isfinite(out)), n_found


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
        hz_path, hz_sources = combined_hazard(ddir)
        if hz_path is None:
            print(f"{dom}: sin hazard_max de GFS/ECMWF/ICON, se omite", file=sys.stderr)
            continue
        print(f"{dom}: hazard combinado desde {','.join(hz_sources)}")
        if not basins:
            print(f"{dom}: sin cuenca modelada en paper1 — sin capa de terreno "
                  f"(transfer pendiente, paper4)")
            continue
        with rasterio.open(hz_path) as src:
            hazard = src.read(1)
            profile = dict(src.profile)
        profile.update(dtype="float32", nodata=np.nan)

        for tag, resampling in [("mean", Resampling.average), ("max", Resampling.max)]:
            susc, n_found = aggregate(basins, profile, resampling)
            if n_found == 0:
                # Dominio CON cuencas configuradas y NINGÚN ráster legible:
                # no es cobertura parcial, es el disco ausente. Fallar aquí hace
                # que analysis_all.sh (paso 1, DURO) aborte y avise por ntfy en
                # vez de escribir una capa de terreno vacía.
                sys.exit(f"FATAL {dom}: ninguno de los {len(basins)} rásters de "
                         f"susceptibilidad existe bajo {KINGSTON} — ¿KINGSTON "
                         "desmontado? Sin capa de terreno no se escribe nada.")
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
            # Bloque fijo: el perfil heredado trae el blocksize del origen y, si
            # ese ráster viene en franjas (no en mosaico), GDAL rechaza
            # tiled=True con "blocks must be multiples of 16".
            p30.update(dtype="float32", nodata=np.nan, compress="deflate",
                       predictor=3, tiled=True, blockxsize=256, blockysize=256)
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
