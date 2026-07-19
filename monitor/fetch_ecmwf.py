#!/usr/bin/env python3
"""Segunda fuente de pronóstico: ECMWF HRES (IFS determinístico) vía Open Data.

Complementa forecast_gfs.py con un modelo INDEPENDIENTE (mismo patrón que la
fusión de feeds observacionales por noisy-OR: si dos fuentes coinciden, más
confianza; si divergen, hay que ser cautos). Motivado por el hallazgo del
2026-07-14: un reporte meteorológico citaba "GFS y ECMWF" con montos de
lluvia bastante mayores a los que nuestra ingesta de SOLO GFS mostraba — sin
ECMWF propio no podíamos saber si la brecha era un error nuestro o una
diferencia real entre modelos.

Fuente: ECMWF Open Data (gratuito, sin registro), bucket público AWS S3,
mismo patrón de acceso que Copernicus/GOES. Ciclo HRES determinístico
(stream=oper), 0.25°, publicado 2x/día (00Z, 12Z — no 4x como GFS), pasos
cada 3 h hasta 144 h. Cada archivo .grib2 trae TODOS los parámetros de un
paso (~140 MB); se descarga solo el rango de bytes de los 2 campos que
importan (tp, sf) vía el .index que acompaña a cada archivo — unos cientos
de KB por paso, no el archivo completo.

Lluvia líquida = tp − sf (ambos en metros de lámina de agua equivalente en
el GRIB de ECMWF; sf ya viene en equivalente líquido, no en profundidad de
nieve) — más directo y preciso que la aproximación de forecast_gfs.py
(isoterma del propio modelo × elevación): acá el propio ECMWF ya hizo la
partición de fase internamente.

OJO ACUMULACIÓN: a diferencia del APCP de GFS (que NOMADS entrega en
"buckets" que se resetean cada 6h), el tp/sf de ECMWF stream=oper es
ACUMULADO DESDE t=0 del ciclo (verificado empíricamente: crece monótono en
cada paso, sin reseteo). Hay que DIFERENCIAR pasos consecutivos para obtener
la lámina de cada ventana de 3h — usar el valor acumulado directo como si
fuera la lluvia del paso sobre-cuenta por un factor ~10-40x hacia el final
del horizonte (exactamente lo que pasó en el primer intento: peak_prob=1.0
saturado incluso en la curva Caine global, que en el backtest real casi
nunca gatilla).

Uso:
    python3 fetch_ecmwf.py --workdir ~/nowcast-forecast --domain atacama \
        --nowcast-bin ../target/release/nowcast [--hours 120]
Salidas en <workdir>/<ciclo>/<dominio>/ecmwf/: steps de lluvia líquida,
hazard_max_ecmwf_<variante>.tif, ecmwf.json (metadatos + comparación con GFS
si el forecast.json de esa corrida ya existe).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

from window_meta import ventana_desde_keys
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forecast_gfs import EVENT_DOMAINS, bilinear_to  # noqa: E402
from fetch_goes_qpe import target_grid  # noqa: E402

BUCKET = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
STEP_H = 3  # cadencia de HRES hasta 144h
RES = 0.1  # misma grilla destino 0.1° que forecast_gfs.py


def log(msg):
    print(f"[ecmwf] {msg}", flush=True)


def urlopen_read(req, timeout, retries=5):
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if attempt == retries - 1:
                break
            time.sleep(min(2 ** attempt, 20))
    raise last


def latest_cycle(now, max_age_h=8.0):
    """Ciclos ECMWF (00Z/12Z) de más nuevo a más viejo con publicación ya
    cumplida — HRES suele publicarse ~6-8 h después de la hora de ciclo."""
    for back in range(6):
        t = now - timedelta(hours=12 * back)
        cyc = t.replace(hour=12 if t.hour >= 12 else 0, minute=0, second=0, microsecond=0)
        age = (now - cyc).total_seconds() / 3600
        if age >= max_age_h / 2:  # margen; se valida con un HEAD real después
            yield cyc


def index_url(cycle, step):
    d = cycle.strftime("%Y%m%d")
    hz = f"{cycle.hour:02d}z"
    stamp = cycle.strftime("%Y%m%d%H0000")
    return f"{BUCKET}/{d}/{hz}/ifs/0p25/oper/{stamp}-{step}h-oper-fc.index"


def grib_url(cycle, step):
    return index_url(cycle, step).replace(".index", ".grib2")


def fetch_field(cycle, step, param, cache_dir):
    """Lee un campo (tp o sf) de un paso vía byte-range del .index; cachea el
    mensaje GRIB crudo (chico) en disco para no re-pedir en corridas repetidas."""
    cache = os.path.join(cache_dir, f"{cycle:%Y%m%d%H}_{step:03d}h_{param}.grib2")
    if not os.path.exists(cache):
        lines = urlopen_read(index_url(cycle, step), timeout=60).decode().strip().split("\n")
        rec = None
        for line in lines:
            d = json.loads(line)
            if d.get("param") == param:
                rec = d
                break
        if rec is None:
            raise RuntimeError(f"campo {param} no encontrado en el índice de f{step:03d}")
        off, length = rec["_offset"], rec["_length"]
        req = urllib.request.Request(
            grib_url(cycle, step), headers={"Range": f"bytes={off}-{off + length - 1}"}
        )
        tmp = cache + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(urlopen_read(req, timeout=90))
            os.replace(tmp, cache)
        except Exception:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            raise
    import xarray as xr

    ds = xr.open_dataset(cache, engine="cfgrib", backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    lats = ds.latitude.values
    lons = ds.longitude.values
    data = np.asarray(ds[var].values, "float64")
    ds.close()
    return data, lats, lons


def read_step(cycle, step, cache_dir):
    """(tp, sf, lats, lons) en METROS de lámina de agua (convención ECMWF)."""
    tp, lats, lons = fetch_field(cycle, step, "tp", cache_dir)
    sf, _, _ = fetch_field(cycle, step, "sf", cache_dir)
    return tp, sf, lats, lons


def write_step(out_tif, grid_latasc, lons_t, lats_t):
    with rasterio.open(
        out_tif, "w", driver="GTiff", height=lats_t.size, width=lons_t.size,
        count=1, dtype="float32", crs="EPSG:4326",
        transform=from_origin(lons_t[0] - RES / 2, lats_t[-1] + RES / 2, RES, RES),
    ) as dst:
        dst.write(grid_latasc.astype("float32"), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--nowcast-bin", default="nowcast")
    ap.add_argument("--domain", action="append", help="repetible; default: EVENT_DOMAINS")
    ap.add_argument("--hours", type=int, default=120, help="horizonte (múltiplo de 3, <=144)")
    ap.add_argument("--id-a", type=float, default=14.82)
    ap.add_argument("--id-b", type=float, default=0.39)
    ap.add_argument("--k", type=float, default=4.0)
    ap.add_argument("--alert-level", type=float, default=0.5)
    a = ap.parse_args()

    domains = {d: EVENT_DOMAINS[d] for d in a.domain} if a.domain else EVENT_DOMAINS
    now = datetime.now(timezone.utc)

    cycle = None
    for cand in latest_cycle(now):
        try:
            urllib.request.urlopen(index_url(cand, 0), timeout=30).read(200)
            cycle = cand
            break
        except Exception:
            continue
    if cycle is None:
        sys.exit("[ecmwf] ningún ciclo HRES disponible aún")
    cycle_id = f"{cycle:%Y%m%dT%H}z"
    log(f"ciclo {cycle_id} (edad {(now - cycle).total_seconds()/3600:.1f} h)")

    cdir = os.path.join(a.workdir, f"{now:%Y%m%dT%H%M}_ecmwf")
    cache_dir = os.path.join(cdir, "raw")
    os.makedirs(cache_dir, exist_ok=True)

    steps = list(range(0, a.hours + 1, STEP_H))
    cumulative = {}  # step -> (tp_acum_m, sf_acum_m) desde t=0 del ciclo
    failed = 0
    for step in steps:
        try:
            tp, sf, lats_g, lons_g = read_step(cycle, step, cache_dir)
            cumulative[step] = (tp, sf)
        except Exception as exc:
            failed += 1
            log(f"ERROR f{step:03d}h: {exc}")
    if not cumulative:
        sys.exit("[ecmwf] no se pudo leer ningún paso")
    log(f"{len(cumulative)} pasos de 3h leídos ({failed} fallidos)")

    # Diferenciar consecutivos: tp/sf son acumulados desde t=0, no buckets.
    # f000 es la referencia (típicamente ~0); si faltó, el primer paso
    # disponible pasa a ser la referencia (su incremento se descarta, no se
    # inventa un valor previo).
    ordered = sorted(cumulative)
    hourly = {}
    prev_step, prev_tp, prev_sf = None, None, None
    for step in ordered:
        tp, sf = cumulative[step]
        if prev_tp is not None:
            liquid = np.clip((tp - prev_tp) - (sf - prev_sf), 0.0, None) * 1000.0  # m->mm
            hourly[step] = liquid
        prev_step, prev_tp, prev_sf = step, tp, sf

    for name, bbox in domains.items():
        lons_t, lats_t = target_grid(bbox)
        ddir = os.path.join(a.workdir, "latest", name, "ecmwf")
        sdir = os.path.join(ddir, "steps")
        os.makedirs(sdir, exist_ok=True)
        keys, tot = [], None
        for step in sorted(hourly):
            liquid = bilinear_to(lats_g, lons_g, hourly[step], lons_t, lats_t)
            liquid = np.clip(liquid, 0.0, None)
            valid = cycle + timedelta(hours=step)
            key = valid.strftime("%Y%m%dT%H%M")
            keys.append(key)
            write_step(os.path.join(sdir, f"step_{key}.tif"), liquid, lons_t, lats_t)
            tot = liquid if tot is None else tot + liquid
        write_step(os.path.join(ddir, "rain_liquid_total.tif"), tot, lons_t, lats_t)

        rasters = ",".join(os.path.join(sdir, f"step_{k}.tif") for k in keys)
        n_steps_window = max(1, round(48 / STEP_H))  # ventana I-D de 48h en pasos de 3h
        variants_out = {}
        for label, id_a in [("caine-global", a.id_a), ("regional-a5.5", 5.5)]:
            cmd = [a.nowcast_bin, "run", "--uniform-susc", "1.0",
                   "--rain-rasters", rasters, "--dt-hours", str(STEP_H),
                   "--max-window", str(n_steps_window),
                   "--id-a", str(id_a), "--id-b", str(a.id_b), "--k", str(a.k),
                   "--alert-level", str(a.alert_level), "--out-dir",
                   os.path.join(ddir, f"hazard_{label}"), "--format", "json"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode not in (0, 2):
                log(f"{name}/{label}: MOTOR FALLÓ: {proc.stderr[:300]}")
                continue
            run = json.loads(proc.stdout)
            peak = max((s["max_probability"] for s in run["steps"]), default=0.0)
            crossings = sum(1 for s in run["steps"] if s.get("alert"))
            variants_out[label] = {"id_a": id_a, "peak_prob": round(peak, 3),
                                   "n_alert_steps": crossings}
            # hazard_max agregado por variante
            hmax = None
            for i, k in enumerate(keys):
                htif = os.path.join(ddir, f"hazard_{label}", f"hazard_{i:04}.tif")
                if not os.path.exists(htif):
                    continue
                with rasterio.open(htif) as src:
                    hz = src.read(1)
                    profile = src.profile
                hmax = hz if hmax is None else np.maximum(hmax, hz)
            if hmax is not None:
                profile.update(dtype="float32", nodata=float("nan"))
                suffix = "" if label == "caine-global" else f"_{label}"
                with rasterio.open(os.path.join(ddir, f"hazard_max{suffix}.tif"), "w",
                                   **profile) as dst:
                    dst.write(hmax, 1)

        meta = {"cycle": cycle_id, "generated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
                "source": "ECMWF HRES (oper, 0.25°) via Open Data", "n_steps": len(keys),
                "failed_steps": failed, "step_hours": STEP_H,
                "ventana": ventana_desde_keys(keys, step_hours=STEP_H),
                "variants": variants_out}
        # Comparación directa con GFS si su forecast.json del mismo ciclo existe.
        gfs_meta_p = os.path.join(a.workdir, "latest", name, "forecast.json")
        if os.path.exists(gfs_meta_p):
            gfs = json.load(open(gfs_meta_p))
            meta["gfs_comparison"] = {
                "gfs_cycle": gfs["cycle"],
                "gfs_regional_peak": gfs["variants"]["regional-a5.5"]["peak_prob"],
                "ecmwf_regional_peak": variants_out.get("regional-a5.5", {}).get("peak_prob"),
            }
        with open(os.path.join(ddir, "ecmwf.json"), "w") as f:
            json.dump(meta, f, indent=1)

        cmp_str = ""
        if "gfs_comparison" in meta:
            c = meta["gfs_comparison"]
            cmp_str = f" | GFS({c['gfs_cycle']}) regional peak {c['gfs_regional_peak']}"
        log(f"{name}: ECMWF regional peak {variants_out.get('regional-a5.5',{}).get('peak_prob')}"
            f"{cmp_str}")

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
