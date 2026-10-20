#!/usr/bin/env python3
"""Qué dijeron monitor@rm y monitor@ohiggins el 20-oct-2026, desde su evidencia
congelada (freeze_evidence.py, copiada en evidencia/monitor_<dom>/<racha>/ sin la
subcarpeta ema/) y del extracto de la bitácora del día (monitor_log_20261020.txt).

Por dominio: (1) avisos ntfy entregados (prioridad y título, hora Chile); (2) por
racha: celdas que cruzan el nivel, su comuna (punto en polígono sobre
comunas_rm_vi.geojson del paquete web) y la lámina de cada feed en la celda del
máximo durante la racha."""
import glob, json, os, re, warnings
from datetime import datetime, timedelta
import numpy as np, rasterio
from shapely.geometry import Point, shape

warnings.filterwarnings("ignore")
E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia")
COM = [(shape(f["geometry"]), f["properties"]["COMUNA"].title())
       for f in json.load(open(os.path.join(E, "comunas_rm_vi.geojson")))["features"]]


def comuna(lon, lat):
    p = Point(lon, lat)
    hit = [n for g, n in COM if g.contains(p)]
    if hit:
        return hit[0]
    d, n = min((g.distance(p), n) for g, n in COM)
    return f"{n} (centro fuera de comuna, a {d * 111:.0f} km)"


def chile(k):
    return (datetime.strptime(k, "%Y%m%dT%H%M") - timedelta(hours=3)).strftime("%d %H:%M")


for dom, nivel in (("rm", 0.267), ("ohiggins", 0.318)):
    D = os.path.join(E, f"monitor_{dom}")
    print(f"\n{'=' * 20} monitor@{dom} (nivel {nivel}) {'=' * 20}")
    log = open(os.path.join(D, "monitor_log_20261020.txt")).read().splitlines()
    av = [l for l in log if "NOTIFY[" in l]
    print(f"avisos entregados el 20-oct UTC: {len(av)}")
    for l in av:
        t = datetime.strptime(l[1:20], "%Y-%m-%dT%H:%M:%S") - timedelta(hours=3)
        pr = re.search(r"NOTIFY\[(\w+)\]", l).group(1)
        tit = l.split("nowcast-monitor: ", 1)[1].split(" — ")[0]
        print(f"  {t:%H:%M} Chile [{pr:7s}] {tit}")
    ver = [re.search(r"corroboración: (\d) \(([^)]*)\)", l) for l in log if "corroboración:" in l]
    print(f"veredictos de corroboración: {len(ver)} ciclos con alerta; corroborados {sum(v.group(1) == '1' for v in ver)};"
          f" sostenidos por: " + ", ".join(f"{s}×{n}" for s, n in sorted(
              {s: sum(v.group(2) == s for v in ver) for s in {v.group(2) for v in ver}}.items())))
    for ep in sorted(glob.glob(os.path.join(D, "2026*"))):
        man = json.load(open(os.path.join(ep, "manifest.json")))
        r0, r1 = man["racha"]["ep_first"], man["racha"]["ep_last"]
        ult = man["ultimo"]
        print(f"\n  racha {chile(r0)} → {chile(r1)} Chile | {man['n_ciclos']} ciclos | último veredicto: "
              f"corroborada={ult['corroborada']} ({ult['sostienen']})")
        celdas, best = {}, (0, None, None)
        for f in sorted(glob.glob(os.path.join(ep, "hazard", "hazard_*.tif"))):
            k = os.path.basename(f)[7:-4]
            if not (r0 <= k <= r1):
                continue
            with rasterio.open(f) as s:
                h = s.read(1)
                for i, j in np.argwhere(h >= nivel):
                    lon, lat = s.xy(i, j)
                    c = (round(lon, 2), round(lat, 2))
                    celdas[c] = max(celdas.get(c, 0), float(h[i, j]))
                    if h[i, j] > best[0]:
                        best = (float(h[i, j]), c, (i, j))
        for c, v in sorted(celdas.items(), key=lambda x: -x[1]):
            print(f"    celda ({c[0]:.2f}, {c[1]:.2f}) peligro máx {v:.2f} → {comuna(*c)}")
        if best[1] is None:
            continue
        i, j = best[2]
        lam = {}
        for feed in ("primary", "goes", "dga"):
            tot = 0.0
            for f in glob.glob(os.path.join(ep, "feeds", feed, "step_*.tif")):
                k = os.path.basename(f)[5:-4]
                if r0 <= k <= r1:
                    with rasterio.open(f) as s:
                        tot += float(s.read(1)[i, j])
            lam["IMERG" if feed == "primary" else feed.upper()] = round(tot, 1)
        print(f"    lámina en la celda del máximo durante la racha: {lam}")
