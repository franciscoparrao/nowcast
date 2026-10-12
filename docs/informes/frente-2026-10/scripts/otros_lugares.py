#!/usr/bin/env python3
"""Celdas que cruzan 0.5 por dominio y modelo (umbral regional a*=5.5), con comuna,
hora del máximo (Chile) y lluvia líquida de 120 h; y celdas con acuerdo de ≥2 modelos.
Uso: otros_lugares.py <dir del ciclo>"""
import glob, json, os, sys
from datetime import datetime, timedelta
import numpy as np, rasterio
from shapely.geometry import shape, Point
from shapely.strtree import STRtree
C = os.path.expanduser(sys.argv[1])
g = json.load(open(f"{C}/comuna_impact/comuna_impact.geojson"))
geoms = [shape(f["geometry"]) for f in g["features"]]; names = [f["properties"]["COMUNA"] for f in g["features"]]
tree = STRtree(geoms)
def comuna_of(x, y):
    p = Point(x, y)
    for i in tree.query(p):
        if geoms[i].contains(p): return names[i]
    return "— (fuera del barrido comunal: mar, cordillera o Argentina)"
def chile(key, h): return (datetime.strptime(key, "%Y%m%dT%H%M") + timedelta(hours=h) - timedelta(hours=3)).strftime("%a %d %H:%M")
def load(dom, m):
    base = "" if m == "gfs" else f"{m}/"
    files = sorted(glob.glob(f"{C}/{dom}/{base}hazard_regional-a5.5/*.tif"))
    if not files: return None
    st = []
    for f in files:
        with rasterio.open(f) as d: st.append(d.read(1)); tr = d.transform
    st = np.where(np.isfinite(st), st, 0)
    meta = json.load(open(f"{C}/{dom}/{base}{'forecast' if m == 'gfs' else m}.json"))["ventana"]
    with rasterio.open(f"{C}/{dom}/{base}rain_liquid_total.tif") as d: rain = d.read(1)
    return {"mx": st.max(0), "arg": st.argmax(0), "tr": tr, "desde": meta["desde"], "dt": meta["horas"] / meta["n_pasos"], "rain": rain}
for dom in ("rm", "coquimbo", "chile-centro-sur", "araucania", "nuble-biobio"):
    L = {m: load(dom, m) for m in ("gfs", "ecmwf", "icon")}; L = {m: v for m, v in L.items() if v}
    print(f"\n=== {dom}")
    for m, lay in L.items():
        cells = sorted([(float(lay["mx"][r, c]), r, c) for r in range(lay["mx"].shape[0]) for c in range(lay["mx"].shape[1]) if lay["mx"][r, c] >= 0.5], reverse=True)
        print(f"  {m}: {len(cells)} celdas ≥0.5")
        for v, r, c in cells[:8]:
            x, y = lay["tr"] * (c + 0.5, r + 0.5)
            print(f"     {v:.2f} ({x:.2f},{y:.2f}) {comuna_of(x, y):22s} máx {chile(lay['desde'], lay['arg'][r, c] * lay['dt'])}  lluvia120h {lay['rain'][r, c]:.0f} mm")
    ref = L["gfs"]; n = 0
    for r in range(ref["mx"].shape[0]):
        for c in range(ref["mx"].shape[1]):
            x, y = ref["tr"] * (c + 0.5, r + 0.5)
            hits = []
            for m, lay in L.items():
                try:
                    rr, cc = rasterio.transform.rowcol(lay["tr"], x, y); 
                    if lay["mx"][rr, cc] >= 0.5: hits.append(m)
                except Exception: pass
            if len(hits) >= 2:
                n += 1; print(f"  ACUERDO {hits} ({x:.2f},{y:.2f}) {comuna_of(x, y)}")
    if not n: print("  sin celdas con ≥2 modelos ≥0.5")
