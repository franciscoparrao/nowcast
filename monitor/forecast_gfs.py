#!/usr/bin/env python3
"""Pronóstico de peligro I-D con GFS + isoterma 0°C (USO INTERNO / experimental).

La mitad "hacia adelante" del monitor: baja el GFS determinístico 0.25°
(subsetting del lado del servidor vía el grib filter de NOMADS — OpenDAP fue
retirado, SCN 25-81), particiona la precipitación en líquida/sólida con la
altura de la ISOTERMA 0°C del propio GFS contra la orografía del propio GFS
(auto-consistente: la lluvia del modelo cae sobre el terreno del modelo), y
corre el motor I-D sobre las próximas 120 h por dominio. La comparación
posterior forecast-vs-observado (replay del archive) es el objetivo: por eso
cada ciclo GFS queda guardado completo.

Física de la partición (simple a propósito, documentada para el informe):
la transición lluvia/nieve se centra ~300 m bajo la isoterma 0°C con una
banda de ±150 m — bajo iso0−450 m todo cae líquido, sobre iso0−150 m todo
sólido, lineal entremedio. En un evento de río atmosférico con isoterma alta
(los aluviones del Maipo 2013/2017) esta partición ES la diferencia entre
"nieve inocua" y "lluvia sobre 3000 m de cuenca aportante".

LIMITACIONES (leerlas antes de citar números): determinístico (sin ensemble
→ sin incertidumbre — fase B pendiente); 0.25° ≈ 25 km sub-resuelve la
cordillera (la orografía GFS suaviza cumbres: la partición hereda eso); el
I-D del pronóstico arranca "seco" (sin lluvia antecedente observada
prependeada, v0); QPF de un modelo global subestima extremos orográficos.
Para anticipar un frente sinóptico está bien; para convección chica, no.

Uso:
    python3 forecast_gfs.py --workdir ~/nowcast-monitor/forecast \
        --nowcast-bin ~/nowcast-monitor/bin/nowcast
    (dominios del evento por defecto; --domain nombre:W,S,E,N repetible)

Salidas por ciclo y dominio en <workdir>/<ciclo>/<dominio>/:
    steps/step_*.tif        lluvia LÍQUIDA horaria pronosticada (mm), grilla 0.1°
    forecast_timeline.csv   paso, hora válida UTC, max_prob, alerta
    crossings.json          cruces pronosticados (inicio/fin/pico)
    forecast.json           metadatos: ciclo, isoterma media, fracción nieve
más un symlink <workdir>/latest → el ciclo más reciente.
"""

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

from fetch_goes_qpe import target_grid

FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl"
# Región única que cubre todos los dominios (una descarga sirve a todos).
REGION = {"leftlon": -75.0, "rightlon": -69.0, "toplat": -29.0, "bottomlat": -40.5}
HOURS = range(1, 121)  # f001..f120 horario
EVENT_DOMAINS = {
    "coquimbo": (-71.05, -32.25, -69.75, -29.25),
    "rm": (-70.75, -34.25, -69.75, -33.25),
    "nuble-biobio": (-72.05, -38.25, -71.05, -36.25),
    "araucania": (-72.35, -39.95, -71.05, -38.25),
    # Barrido completo del evento: costa a cordillera, Coquimbo → Los Ríos
    # (~4600 celdas de 0.1° — panorama; los 4 dominios chicos siguen siendo
    # la vista de detalle y la observación primaria).
    "chile-centro-sur": (-74.05, -40.25, -69.75, -29.25),
}
SNOWLINE_BELOW_ISO0 = 300.0  # centro de la transición (m bajo la isoterma)
BAND_HALF = 150.0            # semiancho de la banda de transición (m)


def log(msg):
    print(f"[gfs] {msg}", flush=True)


def cycle_candidates(now):
    """Ciclos GFS de más nuevo a más viejo con ≥4.2 h de edad (publicación)."""
    out = []
    t = now - timedelta(hours=4.2)
    t = t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for i in range(4):
        c = t - timedelta(hours=6 * i)
        out.append((c.strftime("%Y%m%d"), f"{c.hour:02d}"))
    return out


def filter_url(day, cyc, fhr):
    q = {
        "file": f"gfs.t{cyc}z.pgrb2.0p25.f{fhr:03d}",
        "lev_surface": "on", "lev_0C_isotherm": "on",
        "var_APCP": "on", "var_HGT": "on",
        "subregion": "", "dir": f"/gfs.{day}/{cyc}/atmos",
        **{k: str(v) for k, v in REGION.items()},
    }
    return f"{FILTER}?{urllib.parse.urlencode(q)}"


def fetch(url, dest):
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def read_grib(path):
    """→ (tp_accum mm, iso0 m, orog m) en la grilla GFS de la región."""
    import xarray as xr

    kw = dict(engine="cfgrib", backend_kwargs={"indexpath": ""})
    sfc = xr.open_dataset(path, filter_by_keys={"typeOfLevel": "surface"}, **kw)
    iso = xr.open_dataset(path, filter_by_keys={"typeOfLevel": "isothermZero"}, **kw)
    out = (
        np.asarray(sfc["tp"].values, "float64"),
        np.asarray(iso["gh"].values, "float64"),
        np.asarray(sfc["orog"].values, "float64"),
        sfc["latitude"].values, sfc["longitude"].values,
    )
    sfc.close(); iso.close()
    return out


def liquid_fraction(iso0, orog):
    """Fracción líquida por celda: transición lineal de ±150 m centrada
    300 m bajo la isoterma 0°C."""
    snowline = iso0 - SNOWLINE_BELOW_ISO0
    return np.clip((snowline - orog + BAND_HALF) / (2 * BAND_HALF), 0.0, 1.0)


def bilinear_to(lats_g, lons_g, field, lons_t, lats_t):
    """GFS 0.25° → centros de la grilla destino 0.1° (bilineal, numpy puro).
    lats_g decreciente (norte→sur) como viene del grib; lons_g en 0-360."""
    la = lats_g[::-1]
    fi = field[::-1, :]
    lo = lons_g - 360.0 if lons_g.max() > 180 else lons_g
    yi = np.interp(lats_t, la, np.arange(la.size))
    xi = np.interp(lons_t, lo, np.arange(lo.size))
    y0 = np.clip(np.floor(yi).astype(int), 0, la.size - 2)
    x0 = np.clip(np.floor(xi).astype(int), 0, lo.size - 2)
    wy = (yi - y0)[:, None]
    wx = (xi - x0)[None, :]
    g = (fi[np.ix_(y0, x0)] * (1 - wy) * (1 - wx)
         + fi[np.ix_(y0 + 1, x0)] * wy * (1 - wx)
         + fi[np.ix_(y0, x0 + 1)] * (1 - wy) * wx
         + fi[np.ix_(y0 + 1, x0 + 1)] * wy * wx)
    return g  # (lat asc, lon asc)


def write_step(out_tif, grid_latasc, lons_t, lats_t):
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
        out_tif, "w", driver="GTiff", height=lats_t.size, width=lons_t.size,
        count=1, dtype="float32", crs="EPSG:4326",
        transform=from_origin(lons_t[0] - 0.05, lats_t[-1] + 0.05, 0.1, 0.1),
    ) as dst:
        dst.write(grid_latasc[::-1, :].astype("float32"), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--nowcast-bin", default="nowcast")
    ap.add_argument("--domain", action="append", metavar="nombre:W,S,E,N",
                    help="repetible; default: los 3 dominios del evento")
    ap.add_argument("--id-a", type=float, default=14.82)
    ap.add_argument("--id-b", type=float, default=0.39)
    ap.add_argument("--k", type=float, default=4.0)
    ap.add_argument("--alert-level", type=float, default=0.5)
    a = ap.parse_args()

    domains = EVENT_DOMAINS
    if a.domain:
        domains = {}
        for spec in a.domain:
            name, bb = spec.split(":", 1)
            domains[name] = tuple(float(x) for x in bb.split(","))

    now = datetime.now(timezone.utc)
    day = cyc = None
    for d, c in cycle_candidates(now):
        try:
            urllib.request.urlopen(filter_url(d, c, HOURS[-1]), timeout=60).read(200)
            day, cyc = d, c
            break
        except Exception:
            continue
    if day is None:
        sys.exit("[gfs] ningún ciclo GFS disponible (¿NOMADS caído?)")
    cycle_id = f"{day}T{cyc}z"
    base = datetime.strptime(day + cyc, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    log(f"ciclo {cycle_id} (edad {(now - base).total_seconds()/3600:.1f} h)")

    cdir = os.path.join(a.workdir, cycle_id)
    raw = os.path.join(cdir, "raw")
    os.makedirs(raw, exist_ok=True)

    # --- descarga región completa (~5 KB × 120) -------------------------------
    tp_prev, hourly = None, {}   # fhr -> (depth mm, iso0, orog)
    failed = 0
    for fhr in HOURS:
        g = os.path.join(raw, f"f{fhr:03d}.grib2")
        try:
            if not os.path.exists(g) or os.path.getsize(g) < 500:
                fetch(filter_url(day, cyc, fhr), g)
            tp, iso0, orog, lats_g, lons_g = read_grib(g)
        except Exception as exc:
            failed += 1
            log(f"ERROR f{fhr:03d}: {exc}")
            tp_prev = None  # rompe el balde: el siguiente usa su acumulado propio
            continue
        # APCP viene acumulado en baldes de 6 h: f001,f007,… traen 1 h sola.
        depth = tp if (fhr - 1) % 6 == 0 or tp_prev is None else tp - tp_prev
        tp_prev = None if fhr % 6 == 0 else tp
        hourly[fhr] = (np.clip(depth, 0.0, None), iso0, orog)
    if not hourly:
        sys.exit("[gfs] no se pudo leer ningún paso del ciclo")
    log(f"{len(hourly)} pasos horarios leídos ({failed} fallidos)")

    # --- por dominio: partición, remuestreo, motor ----------------------------
    summary = {}
    for name, bbox in domains.items():
        lons_t, lats_t = target_grid(bbox)
        ddir = os.path.join(cdir, name)
        sdir = os.path.join(ddir, "steps")
        os.makedirs(sdir, exist_ok=True)
        keys, snowfrac_acc, iso_acc = [], [], []
        tot_liquid = None
        for fhr in sorted(hourly):
            depth, iso0, orog = hourly[fhr]
            frac = liquid_fraction(iso0, orog)
            liquid = np.clip(bilinear_to(lats_g, lons_g, depth * frac, lons_t, lats_t), 0, None)
            total = bilinear_to(lats_g, lons_g, depth, lons_t, lats_t)
            valid = base + timedelta(hours=fhr)
            key = valid.strftime("%Y%m%dT%H%M")
            keys.append(key)
            write_step(os.path.join(sdir, f"step_{key}.tif"), liquid, lons_t, lats_t)
            tot_liquid = liquid if tot_liquid is None else tot_liquid + liquid
            tot = float(total.sum())
            snowfrac_acc.append(0.0 if tot <= 0 else 1.0 - float(liquid.sum()) / tot)
            iso_acc.append(float(bilinear_to(lats_g, lons_g, iso0, lons_t, lats_t).mean()))
        # Ráster GIS: lluvia líquida total del horizonte (mm) — el mapa de
        # "cuánta agua efectiva pronostica el GFS" independiente del umbral.
        write_step(os.path.join(ddir, "rain_liquid_total.tif"), tot_liquid, lons_t, lats_t)

        rasters = ",".join(os.path.join(sdir, f"step_{k}.tif") for k in keys)
        # Dos umbrales por diseño: Caine GLOBAL (a de --id-a) y el intercepto
        # REGIONAL a*≈5.5 validado en el backtest Maipo/CR2MET (split-sample
        # años pares, POD 0.5) — el régimen correcto para forzante gruesa:
        # un QPF de 25 km diluye la intensidad igual que CR2MET a 5 km, y la
        # curva global queda estructuralmente alta sobre él.
        variants = [("caine-global", a.id_a), ("regional-a5.5", 5.5)]
        meta = {"cycle": cycle_id, "generated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
                "n_steps": len(keys), "failed_hours": failed,
                "iso0_mean_m": round(float(np.mean(iso_acc)), 0),
                "snow_fraction_mean": round(float(np.mean(snowfrac_acc)), 3),
                "params": {"id_b": a.id_b, "k": a.k, "alert_level": a.alert_level,
                           "susc": "uniform 1.0",
                           "antecedent": "NONE (arranque seco, v0)"},
                "variants": {}}
        parts = []
        for label, id_a in variants:
            # --out-dir: el motor escribe el campo de peligro por paso como
            # GeoTIFF (georef del stack de lluvia) — el insumo del bloque GIS.
            hdir = os.path.join(ddir, f"hazard_{label}")
            os.makedirs(hdir, exist_ok=True)
            cmd = [a.nowcast_bin, "run", "--uniform-susc", "1.0",
                   "--rain-rasters", rasters, "--dt-hours", "1", "--max-window", "48",
                   "--id-a", str(id_a), "--id-b", str(a.id_b), "--k", str(a.k),
                   "--alert-level", str(a.alert_level), "--out-dir", hdir,
                   "--format", "json"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode not in (0, 2):
                log(f"{name}/{label}: MOTOR FALLÓ rc={proc.returncode}: {proc.stderr[:300]}")
                continue
            run = json.loads(proc.stdout)

            rows, crossings, cur = [], [], None
            for s in run["steps"]:
                k = keys[s["step"]]
                al = s.get("alert")
                utc = f"{k[:4]}-{k[4:6]}-{k[6:8]}T{k[9:11]}:{k[11:]}Z"
                rows.append({"step_key": k, "utc": utc,
                             "max_prob": s["max_probability"], "alert": bool(al),
                             "n_cells": al["n_cells"] if al else 0})
                if al and cur is None:
                    cur = {"start": utc, "peak_prob": s["max_probability"],
                           "peak_cells": al["n_cells"]}
                elif al:
                    cur["peak_prob"] = max(cur["peak_prob"], s["max_probability"])
                    cur["peak_cells"] = max(cur["peak_cells"], al["n_cells"])
                elif cur:
                    cur["end"] = utc
                    crossings.append(cur)
                    cur = None
            if cur:
                cur["end"] = rows[-1]["utc"]
                crossings.append(cur)

            suffix = "" if label == "caine-global" else f"_{label}"
            with open(os.path.join(ddir, f"forecast_timeline{suffix}.csv"), "w",
                      newline="") as f:
                w = csv.DictWriter(f, fieldnames=rows[0].keys())
                w.writeheader(); w.writerows(rows)
            with open(os.path.join(ddir, f"crossings{suffix}.json"), "w") as f:
                json.dump(crossings, f, indent=1)
            peak = max((r["max_prob"] for r in rows), default=0.0)
            meta["variants"][label] = {
                "id_a": id_a, "n_alert_steps": run["n_alerts"],
                "n_crossings": len(crossings), "peak_prob": round(peak, 3),
                "first_crossing": crossings[0]["start"] if crossings else None,
            }

            # --- Salidas GIS agregadas por variante ---------------------------
            # hazard_max: peligro máximo por celda en las 120 h (el "mapa del
            # pronóstico"); hour_of_max: hora de pronóstico (f-hour) del máximo;
            # first_crossing_hour: f-hour del primer cruce del alert-level por
            # celda (NaN = nunca cruza). El stack horario queda en hazard_<label>/
            # renombrado a la hora VÁLIDA para el control temporal de QGIS.
            import rasterio

            hmax = None
            hourmax = None
            firstx = None
            fhrs = sorted(hourly)
            for i, fhr in enumerate(fhrs):
                src_tif = os.path.join(hdir, f"hazard_{i:04}.tif")
                if not os.path.exists(src_tif):
                    continue
                with rasterio.open(src_tif) as src:
                    hz = src.read(1)
                    profile = src.profile
                if hmax is None:
                    hmax = np.full(hz.shape, -1.0, "float32")
                    hourmax = np.zeros(hz.shape, "float32")
                    firstx = np.full(hz.shape, np.nan, "float32")
                newmax = hz > hmax
                hourmax[newmax] = fhr
                hmax = np.maximum(hmax, hz)
                crossed = (hz >= a.alert_level) & np.isnan(firstx)
                firstx[crossed] = fhr
                # renombrar al timestamp válido (QGIS temporal: yyyyMMddThhmm)
                key = keys[i]
                os.replace(src_tif, os.path.join(hdir, f"hazard_{key}.tif"))
            if hmax is not None:
                profile.update(dtype="float32", nodata=float("nan"))
                for fname, grid in [
                    (f"hazard_max{suffix}.tif", hmax),
                    (f"hazard_hour_of_max{suffix}.tif", hourmax),
                    (f"first_crossing_fhour{suffix}.tif", firstx),
                ]:
                    with rasterio.open(os.path.join(ddir, fname), "w", **profile) as dst:
                        dst.write(grid, 1)
            parts.append(f"{label}: {len(crossings)} cruce(s), pico {peak:.2f}"
                         + (f", 1º {crossings[0]['start']}" if crossings else ""))
        with open(os.path.join(ddir, "forecast.json"), "w") as f:
            json.dump(meta, f, indent=1)
        summary[name] = meta
        log(f"{name}: {' | '.join(parts)} | iso0 {meta['iso0_mean_m']:.0f} m, "
            f"nieve {meta['snow_fraction_mean']*100:.0f}%")

    latest = os.path.join(a.workdir, "latest")
    if os.path.islink(latest):
        os.remove(latest)
    os.symlink(cycle_id, latest)
    log(f"listo: {cdir}")
    # eccodes tiene un segfault conocido al destruir el intérprete tras cfgrib;
    # el trabajo ya está en disco y flushed — salida dura para no romper cron.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
