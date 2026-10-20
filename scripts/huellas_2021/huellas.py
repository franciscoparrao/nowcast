#!/usr/bin/env python3
"""Huellas de los flujos del temporal del 29–31 de enero de 2021 (RM) por cambio
espectral Sentinel-2, con un umbral derivado del RUIDO, no de un manual.

1. Cambio por banda d = post − pre (B02 B03 B04 B08 B11 B12, reflectancia).
2. Ruido: el mismo cambio entre dos fechas PREVIAS sin evento (par de ruido),
   que trae lo que cambia sin flujo: fenología, sombra topográfica por el sol de
   otra fecha, humedad, georregistro. σ_b = 1.4826·MAD del par de ruido.
3. Magnitud de cambio χ = sqrt(media_b (d_b/σ_b)²) en los dos pares; umbral =
   percentil `--pct` de χ en el par de ruido. Un píxel "cambió" si χ ≥ umbral y
   es válido en las cuatro fechas (SCL vegetación/suelo/no clasificado).
4. Componentes 8-conexos ≥ `--min-px`. Se ASOCIAN a un punto del catálogo si lo
   tocan dentro de `--buffer` m. Mismo procedimiento sobre el par de ruido: la
   fracción de puntos con un componente "de ruido" cerca es la tasa de
   asociación espuria, contra la que se lee la del evento.

Salidas en data/huellas_2021/out/: chi_evento.tif, cambio_evento.tif (uint8),
huellas_enero2021.geojson (componentes asociados, con los COD_RM que tocan) y
resumen.json. Los puntos sirven para ELEGIR componentes, así que la recuperación
de puntos no es una validación independiente: la tasa del par de ruido es el
control, y la inspección visual (chips) la segunda.

Uso: python3 huellas.py --pre 2021-01-23 --post 2021-02-02 --ruido 2021-01-18 2021-01-23
"""
import argparse, json, os

import numpy as np
import rasterio
from rasterio import features
from rasterio.warp import transform as warp_xy
from scipy import ndimage

H = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(H, "..", "..", "data", "huellas_2021")
VALID_SCL = (4, 5, 7)  # vegetación, suelo desnudo, no clasificado


def cargar(fecha):
    with rasterio.open(os.path.join(D, "s2", f"{fecha}.tif")) as s:
        a = s.read()
        return a[:6].astype("float32") / 10000.0, a[6], s.transform, s.crs


def chi(pre, post, sigma, ok):
    z = (post - pre) / sigma[:, None, None]
    c = np.sqrt((z ** 2).mean(0))
    c[~ok] = np.nan
    return c


def componentes(mask, min_px):
    lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return lab, 0
    tam = ndimage.sum(mask, lab, index=np.arange(1, n + 1))
    chicos = np.flatnonzero(tam < min_px) + 1
    lab[np.isin(lab, chicos)] = 0
    return lab, int((tam >= min_px).sum())


def asociar(lab, pts_rc, buf_px):
    """COD_RM por etiqueta de componente que cae a ≤ buf_px del punto."""
    out = {}
    H_, W_ = lab.shape
    for cod, (r, c) in pts_rc.items():
        r0, r1, c0, c1 = max(r - buf_px, 0), min(r + buf_px + 1, H_), max(c - buf_px, 0), min(c + buf_px + 1, W_)
        if r0 >= r1 or c0 >= c1:
            continue
        win = lab[r0:r1, c0:c1]
        yy, xx = np.mgrid[r0:r1, c0:c1]
        cerca = (yy - r) ** 2 + (xx - c) ** 2 <= buf_px ** 2
        for l in np.unique(win[cerca & (win > 0)]):
            out.setdefault(int(l), []).append(cod)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", required=True); ap.add_argument("--post", required=True)
    ap.add_argument("--ruido", nargs=2, required=True)
    ap.add_argument("--pct", type=float, default=99.5)
    ap.add_argument("--min-px", type=int, default=10)  # 0.1 ha
    ap.add_argument("--buffer", type=float, default=150.0)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    out = os.path.join(D, "out" + (f"_{a.tag}" if a.tag else ""))
    os.makedirs(out, exist_ok=True)

    pre, scl_pre, T, crs = cargar(a.pre)
    post, scl_post, *_ = cargar(a.post)
    n0, scl_n0, *_ = cargar(a.ruido[0])
    n1, scl_n1, *_ = cargar(a.ruido[1])
    ok = np.ones(scl_pre.shape, bool)
    for s in (scl_pre, scl_post, scl_n0, scl_n1):
        ok &= np.isin(s, VALID_SCL)
    dn = (n1 - n0)[:, ok]
    sigma = 1.4826 * np.median(np.abs(dn - np.median(dn, 1, keepdims=True)), 1)
    c_ev = chi(pre, post, sigma, ok)
    c_no = chi(n0, n1, sigma, ok)
    thr = float(np.nanpercentile(c_no, a.pct))

    gj = json.load(open(os.path.join(D, "puntos_enero2021.geojson")))
    lon = [f["properties"]["LONGITUD"] for f in gj["features"]]
    lat = [f["properties"]["LATITUD"] for f in gj["features"]]
    xs, ys = warp_xy("EPSG:4326", crs, lon, lat)
    inv = ~T
    pts = {}
    for f, x, y in zip(gj["features"], xs, ys):
        c, r = inv * (x, y)
        if 0 <= r < ok.shape[0] and 0 <= c < ok.shape[1]:
            pts[f["properties"]["COD_RM"]] = (int(r), int(c))
    buf_px = int(round(a.buffer / T.a))

    res = {"pre": a.pre, "post": a.post, "ruido": a.ruido, "pct": a.pct, "umbral_chi": thr,
           "sigma_bandas": dict(zip(["B02", "B03", "B04", "B08", "B11", "B12"], map(float, sigma))),
           "validos_frac": float(ok.mean()), "buffer_m": a.buffer, "min_px": a.min_px, "n_puntos_en_aoi": len(pts)}
    for nombre, c in (("evento", c_ev), ("ruido", c_no)):
        m = np.nan_to_num(c) >= thr
        lab, n = componentes(m, a.min_px)
        asoc = asociar(lab, pts, buf_px)
        con = {cod for v in asoc.values() for cod in v}
        res[nombre] = {"px_cambio_frac": float(m[ok].mean()), "componentes": n,
                       "componentes_asociados": len(asoc), "puntos_con_componente": len(con),
                       "frac_puntos": len(con) / max(len(pts), 1),
                       "area_asociada_ha": float(np.isin(lab, list(asoc)).sum() * T.a * T.a / 1e4)}
        if nombre == "evento":
            prof = dict(driver="GTiff", width=c.shape[1], height=c.shape[0], count=1, crs=crs, transform=T,
                        compress="deflate", tiled=True)
            with rasterio.open(os.path.join(out, "chi_evento.tif"), "w", dtype="float32", nodata=np.nan, **prof) as d:
                d.write(c.astype("float32"), 1)
            with rasterio.open(os.path.join(out, "cambio_evento.tif"), "w", dtype="uint8", nodata=255, **prof) as d:
                d.write(np.where(ok, (lab > 0).astype("uint8"), 255), 1)
            feats = []
            sel = np.isin(lab, list(asoc)).astype("uint8")
            for geom, val in features.shapes(np.where(sel, lab, 0).astype("int32"), mask=sel.astype(bool), transform=T):
                l = int(val)
                feats.append({"type": "Feature", "geometry": geom,
                              "properties": {"comp": l, "cod_rm": ",".join(sorted(asoc[l])),
                                             "n_puntos": len(asoc[l]), "px": int((lab == l).sum())}})
            json.dump({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": str(crs)}},
                       "features": feats}, open(os.path.join(out, "huellas_enero2021.geojson"), "w"))
    json.dump(res, open(os.path.join(out, "resumen.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
