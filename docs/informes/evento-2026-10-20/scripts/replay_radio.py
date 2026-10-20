#!/usr/bin/env python3
"""Repite la racha de monitor@ohiggins del 20-oct-2026 (16:00–18:30Z) con el
feed de estaciones regenerado CON radio (fetch_dga.idw_field, radius_km=15) y la
compara con el feed original SIN radio, sobre la MISMA ventana congelada
(evidencia/monitor_ohiggins/20261020T1600: 18 pasos 10:00–18:30Z de IMERG,
GOES y DGA) y con los parámetros del config.env congelado.

Las dos corridas comparten la limitación de la ventana corta: la lluvia antes
de las 10:00Z no entra a la I-D (en vivo la ventana es de 54 h). Por eso la
corrida 'sin radio' se contrasta además con lo que vio el monitor en vivo.
El campo con radio se rehace desde el minutario archivado (evidencia/ema),
sumado por bloque de 30 min como fetch_dga.series_to_step_depths."""
import glob, json, os, re, subprocess, sys, tempfile, warnings
from datetime import datetime, timedelta
import numpy as np, rasterio

warnings.filterwarnings("ignore")
H = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(H, "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "monitor"))
from fetch_dga import idw_field, target_grid, write_step  # noqa: E402

E = os.path.join(H, "..", "evidencia", "monitor_ohiggins", "20261020T1600")
EMA = os.path.join(H, "..", "evidencia", "ema")
SUSC = os.path.join(H, "..", "evidencia", "monitor_ohiggins", "susc_0p1.tif")
BIN = os.path.expanduser("~/nowcast-monitor/bin/nowcast")
cfg = {k: v.strip('"') for k, v in (l.strip().split("=", 1) for l in open(os.path.join(E, "config.env"))
                                    if "=" in l and not l.lstrip().startswith("#"))}
nivel = float(cfg["ALERT_LEVEL"])
R0, R1 = "20261020T1600", "20261020T1830"
keys = sorted(os.path.basename(p)[7:-4] for p in glob.glob(os.path.join(E, "hazard", "hazard_*.tif")))
keys = [k for k in keys if k <= R1]
est = []
for tok in cfg["DGA_STATIONS"].split(","):
    code, lon, lat, nom = tok.split(":")
    est.append({"code": code, "lon": float(lon), "lat": float(lat), "nombre": nom})
from shapely.geometry import Point, shape  # noqa: E402
COM = [(shape(f["geometry"]), f["properties"]["COMUNA"].title())
       for f in json.load(open(os.path.join(H, "..", "evidencia", "comunas_rm_vi.geojson")))["features"]]


def comuna(lon, lat):
    p = Point(lon, lat)
    return next((n for g, n in COM if g.contains(p)), min((g.distance(p), n) for g, n in COM)[1])


lons, lats = target_grid(tuple(float(x) for x in cfg["BBOX"].split(",")))

# lámina por estación y bloque de 30 min
depth = {}
for st in est:
    d = {}
    for l in open(os.path.join(EMA, f"{st['code']}-20261020.jsonl")):
        r = json.loads(l)
        m = re.findall(r"[\d.]+", str(r.get("aguaCaidaDelMinuto")))
        if m:
            t = datetime.strptime(r["momento"], "%Y-%m-%d %H:%M")
            k = t.replace(minute=0 if t.minute < 30 else 30).strftime("%Y%m%dT%H%M")
            d[k] = d.get(k, 0.0) + float(m[0])
    depth[st["code"]] = d

with tempfile.TemporaryDirectory() as td:
    ref = glob.glob(os.path.join(E, "feeds", "goes", "step_*.tif"))[0]
    zero = os.path.join(td, "zero.tif")
    with rasterio.open(ref) as s:
        prof = s.profile; z = np.zeros((s.height, s.width), "float32")
    with rasterio.open(zero, "w", **prof) as o:
        o.write(z, 1)

    def stack(feed):
        return ",".join(p if os.path.exists(p := os.path.join(E, "feeds", feed, f"step_{k}.tif")) else zero for k in keys)

    os.makedirs(os.path.join(td, "dga_r"))
    for k in keys:
        g = idw_field([(st, depth[st["code"]].get(k, 0.0)) for st in est], lons, lats, 2.0, radius_km=15.0)
        write_step(os.path.join(td, "dga_r", f"step_{k}.tif"), g, lons, lats)
    dga_r = ",".join(os.path.join(td, "dga_r", f"step_{k}.tif") for k in keys)

    res = {}
    for nombre, dga in (("sin radio (feed original)", stack("dga")), ("radio 15 km", dga_r)):
        out = os.path.join(td, nombre[:3])
        os.makedirs(out)
        cmd = [BIN, "run", "--susc", SUSC, "--rain-rasters", stack("primary"), "--fuse-rasters", stack("goes"),
               "--fuse-rasters", dga, "--combine", cfg.get("COMBINE", "noisy-or"), "--dt-hours", cfg["DT_HOURS"],
               "--max-window", cfg["MAX_WINDOW"], "--id-a", cfg["ID_A"], "--id-b", cfg["ID_B"], "--k", cfg["K"],
               "--alert-level", cfg["ALERT_LEVEL"], "--out-dir", out, "--format", "json"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        assert p.returncode in (0, 2), p.stderr[-400:]
        tifs = sorted(glob.glob(os.path.join(out, "*.tif")))
        assert len(tifs) == len(keys), (len(tifs), len(keys))
        alerta = None; pico = 0.0
        for k, t in zip(keys, tifs):
            if R0 <= k <= R1:
                with rasterio.open(t) as s:
                    h = s.read(1)
                alerta = (h >= nivel) if alerta is None else (alerta | (h >= nivel))
                pico = max(pico, float(np.nanmax(h)))
        res[nombre] = alerta
        idx = np.argwhere(alerta)
        cel = sorted({(round(float(lons[j]), 2), round(float(lats[::-1][i]), 2), comuna(float(lons[j]), float(lats[::-1][i]))) for i, j in idx})
        print(f"{nombre:26s}: {int(alerta.sum()):3d} celdas en alerta en la racha {R0[9:]}–{R1[9:]}Z, pico {pico:.2f}")
        if alerta.sum() <= 20:
            print(f"{'':26s}  celdas: {cel}")
    print(f"\nen vivo (monitor@ohiggins, misma racha): 80 celdas (scripts/monitores.py)")
    a, b = res["sin radio (feed original)"], res["radio 15 km"]
    print(f"celdas que el radio apaga: {int((a & ~b).sum())}; que enciende: {int((b & ~a).sum())}")
