#!/usr/bin/env python3
"""Cicatriz de incendio real (dNBR Landsat C2 L2) para la cascada post-incendio.

Reproduce sobre datos reales lo que `nowcast-firespread::post_fire_susceptibility`
hace sobre un incendio simulado: una MÁSCARA de celdas quemadas dentro de la
cual la susceptibilidad se multiplica por un factor (clamp [0,1]). Aquí la
máscara no sale del modelo de fuego sino de la diferencia del Normalized Burn
Ratio entre una escena anterior y una posterior al incendio:

    NBR  = (NIR − SWIR2) / (NIR + SWIR2)          (Landsat 8/9: B5, B7)
    dNBR = NBR_pre − NBR_post
    quemado = dNBR ≥ `--umbral` (0.27 = moderado-alto o más, Key & Benson 2006)

Lee SOLO la ventana del bbox por /vsicurl/ desde Planetary Computer (firma SAS
pública, sin cuenta), así que no baja escenas completas. Nubes, sombra, nieve y AGUA
se enmascaran con QA_PIXEL en cualquiera de las dos fechas. Un filtro de
mayoría 3×3 quita el moteado de píxel aislado.

Salidas (en `--out`, por defecto monitor/data/postfire/<nombre>/):
  dnbr.tif        float32, CRS de la escena, 30 m, NaN donde no hay dato
  scar_mask.tif   uint8: 1 quemado, 0 no quemado, 255 sin dato
  meta.json       escenas, umbral, hectáreas por clase, fecha del incendio
y registra la cicatriz en `monitor/data/postfire/scars.json`, que es lo que
lee `susc_overlay.py` (clave `--basin`, p.ej. 09_rio_maipo).

Uso (incendio de San Carlos de Apoquindo, 29-dic-2025 → 2-ene-2026, 1.012 ha):
  python3 postfire_scar.py --nombre san_carlos_apoquindo_2025-12-29 \
      --pre LC09_L2SP_233083_20251201_02_T1 --post LC09_L2SP_233083_20260118_02_T1 \
      --bbox -70.56 -33.46 -70.36 -33.30 --fecha-incendio 2025-12-29 \
      --basin 09_rio_maipo --factor 2.0
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRO = os.path.join(HERE, "data", "postfire", "scars.json")
# Escala Landsat Collection 2 Level-2 Surface Reflectance.
SR_SCALE, SR_OFFSET = 0.0000275, -0.2
# QA_PIXEL (C2): bit0 fill, bit1 dilated cloud, bit3 cloud, bit4 shadow, bit5 snow,
# bit7 AGUA. El agua va enmascarada desde el 2026-10-12: el mar cambia de NBR entre
# fechas (oleaje, turbidez, reflejo) y con un bbox costero el dNBR marcó como
# "quemadas" 16.237 ha de océano frente a Tomé–Dichato en un solo cuerpo y el 26 %
# de la bahía de Penco pegado a la cicatriz real; la cascada no tiene sentido
# sobre agua y la máscara la inflaba a casi el doble (41.153 → ~18.400 ha).
QA_BAD = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 7)
# Clases de severidad (Key & Benson 2006, dNBR sin escalar).
CLASES = [("no quemado", -9.0, 0.10), ("baja", 0.10, 0.27),
          ("moderada-baja", 0.27, 0.44), ("moderada-alta", 0.44, 0.66),
          ("alta", 0.66, 9.0)]


def stac_item(collection, item_id):
    with urllib.request.urlopen(f"{STAC}/collections/{collection}/items/{item_id}",
                                timeout=60) as r:
        return json.load(r)


def sas_token(collection):
    with urllib.request.urlopen(f"{SAS}/{collection}", timeout=60) as r:
        return json.load(r)["token"]


def read_window(href, token, bbox_wgs84):
    """Lee la ventana del bbox (WGS84) de un COG remoto; devuelve (arr, transform, crs)."""
    url = f"/vsicurl/{href}?{token}"
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                      CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".TIF,.tif"):
        with rasterio.open(url) as s:
            b = transform_bounds("EPSG:4326", s.crs, *bbox_wgs84)
            win = from_bounds(*b, transform=s.transform).round_offsets().round_lengths()
            arr = s.read(1, window=win)
            return arr, s.window_transform(win), s.crs


def nbr_de(item, token, bbox):
    a = item["assets"]
    nir, t, crs = read_window(a["nir08"]["href"], token, bbox)
    swir, t2, _ = read_window(a["swir22"]["href"], token, bbox)
    qa, t3, _ = read_window(a["qa_pixel"]["href"], token, bbox)
    if nir.shape != swir.shape or nir.shape != qa.shape:
        sys.exit(f"ventanas de distinto tamaño en {item['id']}: {nir.shape} {swir.shape} {qa.shape}")
    nir = nir.astype("float64") * SR_SCALE + SR_OFFSET
    swir = swir.astype("float64") * SR_SCALE + SR_OFFSET
    with np.errstate(invalid="ignore", divide="ignore"):
        nbr = (nir - swir) / (nir + swir)
    bad = (qa & QA_BAD) != 0
    nbr[bad | ~np.isfinite(nbr)] = np.nan
    return nbr.astype("float32"), t, crs, float(bad.mean())


def mayoria_3x3(mask):
    """True si ≥5 de los 9 vecinos (incluido él) son True; bordes con relleno False."""
    p = np.pad(mask.astype("uint8"), 1)
    s = sum(p[i:i + mask.shape[0], j:j + mask.shape[1]]
            for i in range(3) for j in range(3))
    return s >= 5


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--nombre", required=True)
    ap.add_argument("--pre", required=True, help="item id Landsat C2 L2 anterior al incendio")
    ap.add_argument("--post", required=True, help="item id Landsat C2 L2 posterior")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--fecha-incendio", required=True)
    ap.add_argument("--basin", required=True,
                    help="directorio de cuenca de KINGSTON al que aplica (p.ej. 09_rio_maipo)")
    ap.add_argument("--factor", type=float, default=2.0,
                    help="amplificación de la susceptibilidad en la cicatriz (couple_fire.rs usa 2.0)")
    ap.add_argument("--umbral", type=float, default=0.27, help="dNBR mínimo para 'quemado'")
    ap.add_argument("--vigencia-anios", type=float, default=2.0,
                    help="años tras el incendio en que la cicatriz sigue activa")
    ap.add_argument("--collection", default="landsat-c2-l2")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-registrar", action="store_true",
                    help="solo calcula (p.ej. chequeo de persistencia); no toca scars.json")
    a = ap.parse_args()

    out = a.out or os.path.join(HERE, "data", "postfire", a.nombre)
    os.makedirs(out, exist_ok=True)
    token = sas_token(a.collection)
    pre = stac_item(a.collection, a.pre)
    post = stac_item(a.collection, a.post)
    nbr_pre, t, crs, bad_pre = nbr_de(pre, token, a.bbox)
    nbr_post, t2, crs2, bad_post = nbr_de(post, token, a.bbox)
    if nbr_pre.shape != nbr_post.shape or crs != crs2 or t != t2:
        sys.exit(f"pre y post no comparten grilla: {nbr_pre.shape}/{nbr_post.shape} {crs}/{crs2}")
    dnbr = nbr_pre - nbr_post
    valid = np.isfinite(dnbr)
    quemado = mayoria_3x3(valid & (dnbr >= a.umbral))
    mask = np.full(dnbr.shape, 255, "uint8")
    mask[valid] = 0
    mask[quemado] = 1
    ha = abs(t.a * t.e) / 10_000.0
    clases = {}
    for nombre, lo, hi in CLASES:
        clases[nombre] = round(float(((dnbr >= lo) & (dnbr < hi) & valid).sum() * ha), 1)
    n_q = int(quemado.sum())

    prof = dict(driver="GTiff", height=dnbr.shape[0], width=dnbr.shape[1], count=1,
                crs=crs, transform=t, compress="deflate", tiled=True,
                blockxsize=256, blockysize=256)
    with rasterio.open(os.path.join(out, "dnbr.tif"), "w", dtype="float32",
                       nodata=np.nan, **prof) as d:
        d.write(np.where(valid, dnbr, np.nan).astype("float32"), 1)
    with rasterio.open(os.path.join(out, "scar_mask.tif"), "w", dtype="uint8",
                       nodata=255, **prof) as d:
        d.write(mask, 1)

    f0 = date.fromisoformat(a.fecha_incendio)
    meta = {
        "nombre": a.nombre, "fecha_incendio": a.fecha_incendio,
        "pre": a.pre, "post": a.post, "collection": a.collection,
        "bbox_wgs84": a.bbox, "crs": str(crs), "res_m": abs(t.a),
        "umbral_dnbr": a.umbral, "filtro": "mayoría 3×3",
        "fraccion_enmascarada_qa": {"pre": round(bad_pre, 4), "post": round(bad_post, 4)},
        "fraccion_sin_dato": round(float((~valid).mean()), 4),
        "ha_por_clase_dnbr": clases,
        "ha_quemadas_mascara": round(n_q * ha, 1), "celdas_quemadas": n_q,
        "dnbr_p50_en_mascara": round(float(np.nanmedian(dnbr[quemado])), 3) if n_q else None,
        "dnbr_p90_en_mascara": round(float(np.nanpercentile(dnbr[quemado], 90)), 3) if n_q else None,
    }
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)

    print(f"[postfire] {a.nombre}: ventana {dnbr.shape} a {abs(t.a):.0f} m ({crs}); "
          f"QA enmascara pre {bad_pre:.1%} / post {bad_post:.1%}")
    print(f"[postfire] ha por clase dNBR: {clases}")
    print(f"[postfire] máscara (dNBR ≥ {a.umbral}, mayoría 3×3): {n_q} celdas = "
          f"{meta['ha_quemadas_mascara']} ha; dNBR mediana {meta['dnbr_p50_en_mascara']}")
    if a.no_registrar:
        print("[postfire] --no-registrar: scars.json intacto")
        return
    reg = []
    if os.path.exists(REGISTRO):
        reg = json.load(open(REGISTRO))
    reg = [r for r in reg if r.get("nombre") != a.nombre]
    reg.append({
        "nombre": a.nombre, "basin": a.basin, "factor": a.factor,
        "mask": os.path.relpath(os.path.join(out, "scar_mask.tif"), os.path.dirname(REGISTRO)),
        "fecha_incendio": a.fecha_incendio,
        "valid_from": a.fecha_incendio,
        "valid_until": (f0 + timedelta(days=round(365.25 * a.vigencia_anios))).isoformat(),
        "ha_quemadas": meta["ha_quemadas_mascara"],
        "fuente": f"dNBR {a.collection} {a.pre} → {a.post}, umbral {a.umbral}",
    })
    os.makedirs(os.path.dirname(REGISTRO), exist_ok=True)
    with open(REGISTRO, "w") as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)

    print(f"[postfire] registrada en {REGISTRO} para {a.basin}, factor {a.factor}, "
          f"vigente hasta {reg[-1]['valid_until']}")


if __name__ == "__main__":
    main()
