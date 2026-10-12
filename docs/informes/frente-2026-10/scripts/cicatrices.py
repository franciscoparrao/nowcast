#!/usr/bin/env python3
"""Cicatrices registradas en monitor/data/postfire/scars.json: vigencia, hectáreas, cuerpos
quemados mayores (con comuna del centroide) y QA de las escenas."""
import json, os, sys
import numpy as np, rasterio
from scipy import ndimage
from rasterio.warp import transform as tr
from shapely.geometry import shape, Point
from shapely.strtree import STRtree
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
REG = os.path.join(ROOT, "monitor", "data", "postfire", "scars.json")
COM = os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else None   # comuna_impact.geojson (opcional)
tree = None
if COM:
    g = json.load(open(COM)); geoms = [shape(f["geometry"]) for f in g["features"]]
    names = [f["properties"]["COMUNA"] for f in g["features"]]; tree = STRtree(geoms)
def com(lon, lat):
    if tree is None: return ""
    p = Point(lon, lat)
    for i in tree.query(p):
        if geoms[i].contains(p): return names[i]
    return "fuera del barrido comunal"
for r in json.load(open(REG)):
    meta = json.load(open(os.path.join(os.path.dirname(REG), os.path.dirname(r["mask"]), "meta.json")))
    print(f"== {r['nombre']}: cuenca {r['basin']}, factor {r['factor']}, incendio {r['fecha_incendio']}, vigente hasta {r['valid_until']}")
    print(f"   {r['fuente']}; QA enmascarada pre/post {meta['fraccion_enmascarada_qa']}; dNBR mediana en la máscara {meta['dnbr_p50_en_mascara']}")
    with rasterio.open(os.path.join(os.path.dirname(REG), r["mask"])) as d:
        a = d.read(1); t = d.transform; crs = d.crs
    m = a == 1; lab, n = ndimage.label(m); sizes = ndimage.sum(m, lab, range(1, n + 1)) * abs(t.a * t.e) / 1e4
    print(f"   máscara: {m.sum() * abs(t.a * t.e) / 1e4:.0f} ha en {n} cuerpos; sin dato {np.mean(a == 255):.1%}")
    for i in np.argsort(sizes)[::-1][:4]:
        ys, xs = np.where(lab == i + 1)
        xx, yy = t * (np.array([xs.min(), xs.max(), xs.mean()]), np.array([ys.min(), ys.max(), ys.mean()]))
        lon, lat = tr(crs, "EPSG:4326", list(xx), list(yy))
        print(f"     cuerpo {sizes[i]:7.0f} ha  lon {min(lon):.3f}..{max(lon):.3f}  lat {min(lat):.3f}..{max(lat):.3f}  {com(lon[2], lat[2])}")
