#!/usr/bin/env python3
"""Curacautín, 12-oct-2026: qué decía el pronóstico comunal y qué vieron los sensores
(GLM, GOES QPE, EMAs DMC) ante la granizada convectiva que inundó 21 viviendas.

Dos modos:
  --extraer <raiz_pronostico>   lee ~/nowcast-forecast/<ciclo>/ (comuna_impact.geojson,
                                watchlist.json, rásters horarios por modelo) y escribe
                                evidencia/curacautin_20261012/pronostico/por_ciclo.json
  (sin argumentos)              lee SOLO evidencia/curacautin_20261012/ e imprime las tablas
                                del informe (pronóstico por ciclo, GLM por 10 min y distancia
                                a la ciudad, GOES QPE en la celda de la ciudad, EMAs).
"""
import glob
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import warnings

import numpy as np

warnings.filterwarnings("ignore", category=DeprecationWarning)

E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia", "curacautin_20261012")
CIUDAD = (-71.89, -38.43)          # Curacautín (plaza), lon/lat
CUT = 9203
DOMINIO = "chile-centro-sur"
NIVEL_FORECAST = 0.5               # alert_level del pronóstico comunal
CHILE = timezone(timedelta(hours=-3))


def dist_km(lon, lat, lon0=CIUDAD[0], lat0=CIUDAD[1]):
    return math.hypot((lon - lon0) * 111.32 * math.cos(math.radians(lat0)), (lat - lat0) * 110.57)


def hora_chile(utc):
    return utc.astimezone(CHILE).strftime("%a %d %H:%M")


# ------------------------------------------------------------------ extracción
def extraer(raiz):
    import rasterio
    from rasterio.features import geometry_mask
    from shapely.geometry import shape
    out = []
    for cyc in sorted(glob.glob(os.path.join(raiz, "2026101[0-2]T*z"))):
        c = os.path.basename(cyc)
        gj = os.path.join(cyc, "comuna_impact", "comuna_impact.geojson")
        if not os.path.exists(gj):
            print("sin geojson:", c, file=sys.stderr)
            continue
        feat = [f for f in json.load(open(gj))["features"] if f["properties"].get("CUT") == CUT][0]
        pr = feat["properties"]
        geom = shape(feat["geometry"])
        w = json.load(open(os.path.join(cyc, "watchlist.json")))
        wl = [s for s in w["selected"] if s.get("comuna", "").upper() == "CURACAUTÍN"]
        rec = {"ciclo": c, "nivel": pr["nivel"],
               "peak": {m: round(pr[f"peak_{m}"], 3) for m in ("gfs", "ecmwf", "icon")},
               "peak_terreno": round(pr["peak_terreno"], 3),
               "watchlist": [{"name": s["name"], "en_lista": s["en_lista"]} for s in wl],
               "modelos": {}}
        dom = os.path.join(cyc, DOMINIO)
        for m, sub, var, meta in (("gfs", "", "hazard_regional-a5.5", "forecast.json"),
                                  ("ecmwf", "ecmwf", "hazard_regional-a5.5", "ecmwf/ecmwf.json"),
                                  ("icon", "icon", "hazard_caine-global", "icon/icon.json")):
            d = os.path.join(dom, sub, var)
            if not os.path.isdir(d):
                continue
            info = json.load(open(os.path.join(dom, meta)))
            desde = datetime.strptime(info["ventana"]["desde"], "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
            sh = int(info.get("step_hours", 1))
            files = sorted(glob.glob(os.path.join(d, "hazard_*.tif")))
            serie = []
            best = (0.0, None, None)
            mask = None
            for i, p in enumerate(files):
                b = os.path.basename(p)[7:-4]
                t = (datetime.strptime(b, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
                     if "T" in b else desde + timedelta(hours=sh * i))
                with rasterio.open(p) as ds:
                    a = ds.read(1)
                    if mask is None:
                        mask = ~geometry_mask([geom], out_shape=a.shape, transform=ds.transform, all_touched=True)
                        r, col = ds.index(*CIUDAD)
                    v = float(a[r, col]) if np.isfinite(a[r, col]) else 0.0
                    am = np.where(mask & np.isfinite(a), a, -1)
                    j = int(am.argmax()); vm = float(am.flat[j])
                    if vm > best[0]:
                        rr, cc = np.unravel_index(j, a.shape)
                        best = (vm, t.strftime("%Y-%m-%dT%H:%MZ"), [round(x, 2) for x in ds.xy(rr, cc)])
                serie.append([t.strftime("%Y-%m-%dT%H:%MZ"), round(v, 3)])
            rain = os.path.join(dom, sub, "rain_liquid_total.tif")
            with rasterio.open(rain) as ds:
                lluvia = float(ds.read(1)[r, col])
            rec["modelos"][m] = {"serie_celda_ciudad": serie, "max_comuna": {"valor": round(best[0], 3), "utc": best[1], "celda": best[2]},
                                 "lluvia_120h_celda_ciudad_mm": round(lluvia, 1),
                                 "max_celda_ciudad": max(serie, key=lambda x: x[1])}
        out.append(rec)
        print(c, rec["nivel"], rec["peak"], [s["name"] for s in wl], file=sys.stderr)
    p = os.path.join(E, "pronostico", "por_ciclo.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"comuna": "CURACAUTÍN", "cut": CUT, "celda_ciudad": CIUDAD, "nivel_alerta": NIVEL_FORECAST,
               "umbral_por_modelo": {"gfs": "regional-a5.5", "ecmwf": "regional-a5.5", "icon": "caine-global"},
               "ciclos": out}, open(p, "w"), ensure_ascii=False, indent=1)
    print("escrito", p, file=sys.stderr)


# ------------------------------------------------------------------ informe
def informe():
    P = json.load(open(os.path.join(E, "pronostico", "por_ciclo.json")))
    print("PRONÓSTICO COMUNAL — Curacautín (CUT 9203), dominio chile-centro-sur, nivel ALTO ≥0.5")
    print(f"{'ciclo':13s} {'nivel':6s} {'GFS':>6s} {'ECMWF':>6s} {'ICON':>6s}  watchlist      pico ECMWF (dónde, cuándo)                 ciudad lun 12 17–21Z: GFS/ECMWF/ICON máx")
    for r in P["ciclos"]:
        e = r["modelos"].get("ecmwf", {}).get("max_comuna", {})
        lun = {}
        for m, v in r["modelos"].items():
            s = [x[1] for x in v["serie_celda_ciudad"] if "2026-10-12T17" <= x[0] <= "2026-10-12T21"]
            lun[m] = max(s) if s else float("nan")
        wl = ",".join(s["name"] + ("+exp" if "exposicion" in s["en_lista"] else "") for s in r["watchlist"]) or "-"
        print(f"{r['ciclo']:13s} {r['nivel']:6s} {r['peak']['gfs']:6.2f} {r['peak']['ecmwf']:6.2f} {r['peak']['icon']:6.2f}  {wl:14s} "
              f"{e.get('valor', 0):.2f} en {e.get('celda')} {e.get('utc', '')}   "
              + "/".join(f"{lun.get(m, float('nan')):.2f}" for m in ("gfs", "ecmwf", "icon")))
    ult = [r for r in P["ciclos"] if r["ciclo"] in ("20261012T00z", "20261012T06z")]
    for r in ult:
        print(f"  {r['ciclo']}: máx en la celda de la ciudad por modelo (toda la ventana): "
              + ", ".join(f"{m} {v['max_celda_ciudad'][1]:.2f} ({v['max_celda_ciudad'][0]})" for m, v in r["modelos"].items())
              + "; lluvia 120 h en la celda: " + ", ".join(f"{m} {v['lluvia_120h_celda_ciudad_mm']:.0f} mm" for m, v in r["modelos"].items()))
    e06 = [r for r in P["ciclos"] if r["ciclo"] == "20261012T06z"][0]["modelos"]["ecmwf"]["max_comuna"]
    print(f"  distancia del pico ECMWF del ciclo 06z a la ciudad: {dist_km(*e06['celda']):.0f} km")

    G = json.load(open(os.path.join(E, "glm", "flashes_curacautin_12oct.json")))
    bins = G["bins"]; pts = G["curacautin_pts"]
    print("\nGLM GOES-19 (barrido GLM-L2-LCFA 16Z–23Z) — descargas por 10 min UTC: Araucanía | caja ±0.4° Curacautín | ≤5 km de la ciudad")
    d5 = {}
    for lat, lon, hms in pts:
        if dist_km(lon, lat) <= 5:
            slot = hms[:4] + "0"
            d5[slot] = d5.get(slot, 0) + 1
    for slot in sorted(bins):
        a, cu = bins[slot].get("araucania", 0), bins[slot].get("curacautin", 0)
        if cu or d5.get(slot):
            print(f"  {slot}  {a:5d} | {cu:4d} | {d5.get(slot, 0):3d}")
    tot_a = sum(b.get("araucania", 0) for b in bins.values())
    print(f"  total Araucanía {tot_a}, caja {len(pts)}, ≤5 km {sum(d5.values())}; "
          f"máximo regional {max(bins.values(), key=lambda b: b.get('araucania', 0)).get('araucania')} /10 min a las "
          f"{max(bins, key=lambda s: bins[s].get('araucania', 0))}Z")
    # centroide por 10 min y primera descarga a ≤7 km
    print("  centroide de las descargas de la caja por 10 min (distancia a la ciudad):")
    cent = {}
    for lat, lon, hms in pts:
        cent.setdefault(hms[:4] + "0", []).append((lon, lat))
    for slot in sorted(cent):
        xs = np.array(cent[slot]); c = xs.mean(0)
        print(f"    {slot}  n={len(xs):3d}  centroide ({c[0]:.2f},{c[1]:.2f}) a {dist_km(*c):4.1f} km")
    pr7 = min((hms for lat, lon, hms in pts if dist_km(lon, lat) <= 7), default=None)
    pr5 = min((hms for lat, lon, hms in pts if dist_km(lon, lat) <= 5), default=None)
    print(f"  primera descarga a ≤7 km: {pr7}Z; a ≤5 km: {pr5}Z (Chile = UTC−3)")

    import rasterio
    print("\nGOES QPE (ABI-L2-RRQPEF, pasos de 30 min) — mm en la celda de la ciudad | máximo de la caja (dónde)")
    tot = 0.0
    for p in sorted(glob.glob(os.path.join(E, "goes", "steps", "step_*.tif"))):
        k = os.path.basename(p)[5:-4]
        with rasterio.open(p) as ds:
            a = ds.read(1); r, c = ds.index(*CIUDAD)
            v = float(a[r, c]) if np.isfinite(a[r, c]) else 0.0
            am = np.where(np.isfinite(a), a, 0); j = int(am.argmax()); rr, cc = np.unravel_index(j, a.shape)
            x, y = ds.xy(rr, cc)
        tot += v
        if "T1600" <= k[8:] <= "T2100":
            print(f"  {k}  {v:5.2f} | {am.max():5.2f} en ({x:.2f},{y:.2f}) a {dist_km(x, y):4.1f} km")
    print(f"  total en la celda de la ciudad, todos los pasos congelados: {tot:.1f} mm")

    print("\nEMAs DMC (archivo archive_ema, registros crudos del 12-oct) — mm por hora UTC 12Z–23Z")
    red = json.load(open(os.path.join(E, "ema", "estaciones_araucania.json")))
    for code in ("380063", "380018"):
        p = os.path.join(E, "ema", f"{code}-2026-10-12.jsonl")
        st = [s for s in red if str(s["codigoNacional"]) == code][0]
        hor = {}
        n = 0
        for l in open(p):
            d = json.loads(l); n += 1
            mm = d.get("aguaCaidaDelMinuto") or d.get("mm") or "0"
            try:
                v = float(str(mm).split()[0].replace(",", "."))
            except ValueError:
                v = 0.0
            h = (d.get("momento") or d.get("utc") or "").replace(" ", "T")[:13]
            hor[h] = hor.get(h, 0.0) + v
        hs = sorted(h for h in hor if "2026-10-12T12" <= h <= "2026-10-12T23")
        print(f"  {code} {st['nombreEstacion']:18s} ({float(st['longitud']):.2f},{float(st['latitud']):.2f}) a {dist_km(float(st['longitud']), float(st['latitud'])):3.0f} km: "
              + " ".join(f"{h[11:13]}Z:{hor[h]:.1f}" for h in hs) + f"  | {n} registros")
    print("  370067 Angol, Húsares: solo 5 registros, de julio (estación sin datos en la API)")
    cerca = sorted(((dist_km(float(s["longitud"]), float(s["latitud"])), s["nombreEstacion"], s["codigoNacional"]) for s in red))[:4]
    print("  EMAs DMC más cercanas a Curacautín: " + "; ".join(f"{n} ({c}) {d:.0f} km" for d, n, c in cerca))

    # --- retrospectiva de monitor@araucania (instanciado el 13-oct 01:46Z; ventana de 54 h
    #     hacia atrás) sobre la evidencia que congeló freeze_evidence.py en su primer ciclo
    M = os.path.join(E, "monitor_araucania")
    man = json.load(open(os.path.join(M, "manifest.json")))
    print(f"\nRETROSPECTIVA monitor@araucania (commit {man['monitor_commit']}, a*=5.5, nivel {man.get('alert_level', 0.391)}; "
          f"racha congelada desde {man['racha']['ep_first']}; IMERG Early en relleno cero desde "
          f"{man['feeds']['primary'].get('zerofill_en_ventana', ['-'])[0]} en el ciclo de la alerta)")
    nivel = 0.391
    print(f"{'paso UTC':14s} {'Chile':6s} {'celdas≥niv':>10s} {'peligro Curacautín':>18s}   IMERG  GOES   DGA  (mm/30 min en la celda de la ciudad) | vecina E (-71.75,-38.35): peligro IMERG")
    for p in sorted(glob.glob(os.path.join(M, "hazard", "hazard_*.tif"))):
        k = os.path.basename(p)[7:-4]
        with rasterio.open(p) as d:
            h = d.read(1); r, c = d.index(*CIUDAD); r2, c2 = d.index(-71.75, -38.35)
            n_al = int((np.isfinite(h) & (h >= nivel)).sum())
        mm = []
        for feed in ("primary", "goes", "dga"):
            q = os.path.join(M, "feeds", feed, f"step_{k}.tif")
            if os.path.exists(q):
                with rasterio.open(q) as d:
                    a = d.read(1); v = float(a[r, c]); v2 = float(a[r2, c2])
                mm.append(0.0 if not np.isfinite(v) else v)
                if feed == "primary":
                    im2 = 0.0 if not np.isfinite(v2) else v2
            else:
                mm.append(float("nan"))
                if feed == "primary":
                    im2 = float("nan")
        t = datetime.strptime(k, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
        print(f"{k:14s} {t.astimezone(CHILE):%H:%M}  {n_al:10d} {h[r, c]:18.2f}   " + " ".join(f"{v:5.2f}" for v in mm)
              + f"              | {h[r2, c2]:.2f} {im2:5.2f}")
    print("  (nan = IMERG aún no publicado en ese ciclo: relleno cero. Evento según prensa: ~18:10–18:50Z, 15:10–15:50 Chile)")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--extraer":
        extraer(os.path.expanduser(sys.argv[2]))
    else:
        informe()
