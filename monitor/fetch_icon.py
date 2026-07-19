#!/usr/bin/env python3
"""Tercera fuente deterministica: DWD ICON global (13 km) via Open Data.

ICON global es la mejora gratuita incremental frente a GFS/ECMWF Open Data
0.25 deg: malla icosaedrica nativa de ~13 km, global, 4 ciclos al dia. Este
script descarga lluvia de `rain_gsp + rain_con`, reproyecta por vecino mas
cercano a la grilla interna de 0.1 deg y corre el motor I-D igual que las
fuentes existentes.

Uso:
    python3 fetch_icon.py --workdir ~/nowcast-forecast --domain atacama \
        --nowcast-bin ~/nowcast-monitor/bin/nowcast --hours 120

Salidas:
    <workdir>/latest/<dominio>/icon/steps/step_*.tif
    <workdir>/latest/<dominio>/icon/rain_liquid_total.tif
    <workdir>/latest/<dominio>/icon/icon.json
"""

import argparse
import bz2
import html.parser
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forecast_gfs import EVENT_DOMAINS  # noqa: E402
from fetch_goes_qpe import target_grid  # noqa: E402

BASE = "https://opendata.dwd.de/weather/nwp/icon/grib"
RES = 0.1
RAIN_PARAMS = ("rain_gsp", "rain_con")


def log(msg):
    print(f"[icon] {msg}", flush=True)


class LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href":
                self.hrefs.append(value)


def read_url(url, timeout=60):
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if attempt == 4:
                break
            time.sleep(min(2 ** attempt, 12))
    raise last


def list_hrefs(url):
    p = LinkParser()
    p.feed(read_url(url).decode("utf-8", "replace"))
    return [urllib.parse.unquote(h) for h in p.hrefs]


def cycle_candidates(now):
    """ICON global publica 00/06/12/18; probar de nuevo a viejo."""
    t = now - timedelta(hours=2.5)
    t = t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for i in range(8):
        yield t - timedelta(hours=6 * i)


def icon_steps(hours):
    """DWD ICON global es horario al comienzo y luego 3-horario."""
    out = list(range(1, min(hours, 78) + 1))
    if hours > 78:
        out.extend(range(81, hours + 1, 3))
    return out


def param_dir(cycle_hour, param):
    return f"{BASE}/{cycle_hour:02d}/{param}/"


def files_for_param(cycle, param):
    """Mapa forecast-hour -> URL .grib2.bz2 para un parametro/ciclo."""
    root = param_dir(cycle.hour, param)
    stamp = cycle.strftime("%Y%m%d%H")
    out = {}
    for href in list_hrefs(root):
        if not href.endswith(".grib2.bz2") or stamp not in href:
            continue
        m = re.search(r"_(\d{3})_[A-Z0-9_]+\.grib2\.bz2$", href)
        if not m:
            continue
        out[int(m.group(1))] = urllib.parse.urljoin(root, href)
    return out


def latest_time_invariant_url(cycle_hour, param):
    """URL del ultimo campo time-invariant disponible, con fallback a 00Z."""
    for hour in (cycle_hour, 0):
        root = param_dir(hour, param)
        hrefs = [h for h in list_hrefs(root)
                 if "time-invariant" in h and h.endswith(".grib2.bz2")]
        if hrefs:
            return urllib.parse.urljoin(root, sorted(hrefs)[-1])
    raise RuntimeError(f"sin campo time-invariant para {param}")


def download_bz2(url, dest):
    """Descarga y descomprime .bz2 a GRIB2 crudo. Reusa cache si existe."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    bz_dest = dest + ".bz2"
    if not os.path.exists(bz_dest) or os.path.getsize(bz_dest) == 0:
        tmp_bz = bz_dest + ".tmp"
        last = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=180) as r, open(tmp_bz, "wb") as f:
                    shutil.copyfileobj(r, f)
                os.replace(tmp_bz, bz_dest)
                break
            except Exception as exc:
                last = exc
                try:
                    os.remove(tmp_bz)
                except FileNotFoundError:
                    pass
                if attempt == 4:
                    raise last
                time.sleep(min(2 ** attempt, 20))
    tmp = dest + ".tmp"
    with bz2.open(bz_dest, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, dest)
    return dest


def read_grib_values(path):
    import xarray as xr

    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    names = list(ds.data_vars)
    if not names:
        ds.close()
        raise RuntimeError(f"sin variables GRIB en {path}")
    name = names[0]
    da = ds[name]
    values = np.asarray(da.values, "float64").squeeze().reshape(-1)
    attrs = dict(da.attrs)
    lats = np.asarray(ds["latitude"].values, "float64").squeeze().reshape(-1) \
        if "latitude" in ds else None
    lons = np.asarray(ds["longitude"].values, "float64").squeeze().reshape(-1) \
        if "longitude" in ds else None
    ds.close()
    return values, lats, lons, attrs


def to_degrees_if_needed(values):
    values = np.asarray(values, "float64")
    finite = values[np.isfinite(values)]
    if finite.size and np.nanmax(np.abs(finite)) <= math.pi + 0.1:
        return np.degrees(values)
    return values


def normalise_lons(lons):
    return ((np.asarray(lons, "float64") + 180.0) % 360.0) - 180.0


def unit_to_mm(values, attrs):
    unit = str(attrs.get("GRIB_units") or attrs.get("units") or "").lower()
    if unit in {"m", "m of water equivalent"}:
        return values * 1000.0
    # ICON precipitation accumulations are normally kg m**-2, numerically mm.
    return values


def load_coordinates(raw_dir, cycle_hour):
    paths = {}
    for param in ("clat", "clon"):
        url = latest_time_invariant_url(cycle_hour, param)
        paths[param] = download_bz2(url, os.path.join(raw_dir, f"static_{param}.grib2"))
    clat, _, _, _ = read_grib_values(paths["clat"])
    clon, _, _, _ = read_grib_values(paths["clon"])
    return to_degrees_if_needed(clat), normalise_lons(to_degrees_if_needed(clon))


def build_nearest_map(src_lats, src_lons, lats_t, lons_t, bbox):
    w, s, e, n = bbox
    margin = 0.5
    mask = (
        (src_lats >= s - margin) & (src_lats <= n + margin) &
        (src_lons >= w - margin) & (src_lons <= e + margin)
    )
    src_idx = np.flatnonzero(mask)
    if src_idx.size == 0:
        raise RuntimeError(f"ICON no tiene puntos fuente dentro de {bbox}")
    slat = src_lats[src_idx]
    slon = src_lons[src_idx]
    lon_grid, lat_grid = np.meshgrid(lons_t, lats_t)
    tlat = lat_grid.reshape(-1)
    tlon = lon_grid.reshape(-1)
    scale = math.cos(math.radians(float(np.mean(tlat))))
    nearest = np.empty(tlat.size, dtype=np.int64)
    for start in range(0, tlat.size, 512):
        end = min(start + 512, tlat.size)
        dlat = tlat[start:end, None] - slat[None, :]
        dlon = (tlon[start:end, None] - slon[None, :]) * scale
        nearest[start:end] = src_idx[np.argmin(dlat * dlat + dlon * dlon, axis=1)]
    return nearest.reshape(lats_t.size, lons_t.size)


def write_step(out_tif, grid_latasc, lons_t, lats_t):
    with rasterio.open(
        out_tif, "w", driver="GTiff", height=lats_t.size, width=lons_t.size,
        count=1, dtype="float32", crs="EPSG:4326",
        transform=from_origin(lons_t[0] - RES / 2, lats_t[-1] + RES / 2, RES, RES),
    ) as dst:
        dst.write(grid_latasc[::-1, :].astype("float32"), 1)


def choose_cycle(now, hours):
    wanted = icon_steps(hours)
    for cycle in cycle_candidates(now):
        try:
            files = {p: files_for_param(cycle, p) for p in RAIN_PARAMS}
        except Exception as exc:
            log(f"{cycle:%Y%m%dT%H}z no listo: {exc}")
            continue
        ok = all(1 in files[p] for p in RAIN_PARAMS)
        if ok:
            available = sorted(set.intersection(*(set(files[p]) for p in RAIN_PARAMS)))
            usable = [h for h in wanted if h in available]
            if usable:
                return cycle, files, usable
    return None, None, None


def latest_root(workdir, cycle_id):
    latest = os.path.join(workdir, "latest")
    if os.path.exists(latest):
        return os.path.realpath(latest)
    root = os.path.join(workdir, cycle_id)
    os.makedirs(root, exist_ok=True)
    try:
        os.symlink(cycle_id, latest)
    except FileExistsError:
        pass
    return root


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--nowcast-bin", default="nowcast")
    ap.add_argument("--domain", action="append", help="repetible; default: EVENT_DOMAINS")
    ap.add_argument("--hours", type=int, default=120, help="horizonte ICON deseado")
    ap.add_argument("--id-a", type=float, default=14.82)
    ap.add_argument("--id-b", type=float, default=0.39)
    ap.add_argument("--k", type=float, default=4.0)
    ap.add_argument("--alert-level", type=float, default=0.5)
    ap.add_argument("--check-only", action="store_true",
                    help="solo verifica ciclo/archivos disponibles; no descarga campos")
    a = ap.parse_args()

    domains = {d: EVENT_DOMAINS[d] for d in a.domain} if a.domain else EVENT_DOMAINS
    now = datetime.now(timezone.utc)
    cycle, file_maps, steps = choose_cycle(now, a.hours)
    if cycle is None:
        sys.exit("[icon] ningun ciclo ICON con lluvia disponible")
    cycle_id = f"{cycle:%Y%m%dT%H}z_icon"
    log(f"ciclo {cycle_id}; {len(steps)} pasos disponibles hasta f{max(steps):03d}")
    if a.check_only:
        return

    root = latest_root(a.workdir, cycle_id)
    raw_dir = os.path.join(a.workdir, cycle_id, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    src_lats, src_lons = load_coordinates(raw_dir, cycle.hour)

    # Precalcular reproyeccion por dominio. ICON es icosaedrico; esta primera
    # ingesta usa vecino mas cercano para no introducir una dependencia CDO/ESMF.
    maps = {}
    for name, bbox in domains.items():
        lons_t, lats_t = target_grid(bbox)
        maps[name] = (lons_t, lats_t, build_nearest_map(src_lats, src_lons, lats_t, lons_t, bbox))

    prev_fhr = 0
    prev_acc = None
    hourly = {}
    failed = 0
    step_types = set()
    for fhr in steps:
        try:
            parts = []
            attrs_seen = []
            for param in RAIN_PARAMS:
                url = file_maps[param][fhr]
                path = download_bz2(url, os.path.join(raw_dir, f"f{fhr:03d}_{param}.grib2"))
                values, lats_inline, lons_inline, attrs = read_grib_values(path)
                if lats_inline is not None and lons_inline is not None:
                    if values.size != src_lats.size:
                        src_lats = to_degrees_if_needed(lats_inline)
                        src_lons = normalise_lons(to_degrees_if_needed(lons_inline))
                parts.append(unit_to_mm(values, attrs))
                attrs_seen.append(attrs)
            acc = np.clip(sum(parts), 0.0, None)
            step_types.update(str(x.get("GRIB_stepType", "")) for x in attrs_seen)
            if prev_acc is None:
                inc = acc
            else:
                inc = acc - prev_acc if "accum" in step_types else acc
            inc = np.clip(inc, 0.0, None)
            span = max(1, fhr - prev_fhr)
            for hh in range(prev_fhr + 1, fhr + 1):
                hourly[hh] = inc / span
            prev_fhr = fhr
            prev_acc = acc
        except Exception as exc:
            failed += 1
            log(f"ERROR f{fhr:03d}: {exc}")
    if not hourly:
        sys.exit("[icon] no se pudo leer ningun paso")
    log(f"{len(hourly)} pasos horarios reconstruidos ({failed} pasos ICON fallidos)")

    for name, bbox in domains.items():
        lons_t, lats_t, nearest = maps[name]
        ddir = os.path.join(root, name, "icon")
        sdir = os.path.join(ddir, "steps")
        os.makedirs(sdir, exist_ok=True)
        keys, total = [], None
        for hour in sorted(hourly):
            grid = hourly[hour][nearest]
            valid = cycle + timedelta(hours=hour)
            key = valid.strftime("%Y%m%dT%H%M")
            keys.append(key)
            write_step(os.path.join(sdir, f"step_{key}.tif"), grid, lons_t, lats_t)
            total = grid if total is None else total + grid
        write_step(os.path.join(ddir, "rain_liquid_total.tif"), total, lons_t, lats_t)

        rasters = ",".join(os.path.join(sdir, f"step_{k}.tif") for k in keys)
        variants = {}
        for label, id_a in [("caine-global", a.id_a), ("regional-a5.5", 5.5)]:
            hdir = os.path.join(ddir, f"hazard_{label}")
            os.makedirs(hdir, exist_ok=True)
            cmd = [a.nowcast_bin, "run", "--uniform-susc", "1.0",
                   "--rain-rasters", rasters, "--dt-hours", "1", "--max-window", "48",
                   "--id-a", str(id_a), "--id-b", str(a.id_b), "--k", str(a.k),
                   "--alert-level", str(a.alert_level), "--out-dir", hdir,
                   "--format", "json"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode not in (0, 2):
                log(f"{name}/{label}: MOTOR FALLO rc={proc.returncode}: {proc.stderr[:300]}")
                continue
            run = json.loads(proc.stdout)
            peak = max((s["max_probability"] for s in run["steps"]), default=0.0)
            variants[label] = {
                "id_a": id_a,
                "n_alert_steps": run["n_alerts"],
                "peak_prob": round(peak, 3),
            }

            hmax = None
            profile = None
            for i, key in enumerate(keys):
                src_tif = os.path.join(hdir, f"hazard_{i:04}.tif")
                if not os.path.exists(src_tif):
                    continue
                with rasterio.open(src_tif) as src:
                    hz = src.read(1)
                    profile = src.profile
                hmax = hz if hmax is None else np.maximum(hmax, hz)
                os.replace(src_tif, os.path.join(hdir, f"hazard_{key}.tif"))
            if hmax is not None and profile is not None:
                profile.update(dtype="float32", nodata=float("nan"))
                suffix = "" if label == "caine-global" else f"_{label}"
                with rasterio.open(os.path.join(ddir, f"hazard_max{suffix}.tif"),
                                   "w", **profile) as dst:
                    dst.write(hmax.astype("float32"), 1)

        meta = {
            "cycle": cycle.strftime("%Y%m%dT%Hz"),
            "generated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
            "source": "DWD ICON global, native icosahedral grid, ~13 km",
            "license": "DWD Open Data / CC BY 4.0",
            "fields": list(RAIN_PARAMS),
            "reprojection": "nearest-neighbour ICON cell centre -> 0.1 deg target grid",
            "n_steps": len(keys),
            "failed_icon_steps": failed,
            "step_hours": 1,
            "native_step_types": sorted(x for x in step_types if x),
            "params": {"id_b": a.id_b, "k": a.k, "alert_level": a.alert_level},
            "variants": variants,
        }
        gfs_meta = os.path.join(root, name, "forecast.json")
        ecmwf_meta = os.path.join(root, name, "ecmwf", "ecmwf.json")
        if os.path.exists(gfs_meta):
            gfs = json.load(open(gfs_meta))
            meta["gfs_comparison"] = {
                "gfs_cycle": gfs.get("cycle"),
                "gfs_regional_peak": gfs.get("variants", {}).get("regional-a5.5", {}).get("peak_prob"),
                "icon_regional_peak": variants.get("regional-a5.5", {}).get("peak_prob"),
            }
        if os.path.exists(ecmwf_meta):
            ecmwf = json.load(open(ecmwf_meta))
            meta["ecmwf_comparison"] = {
                "ecmwf_cycle": ecmwf.get("cycle"),
                "ecmwf_regional_peak": ecmwf.get("variants", {}).get("regional-a5.5", {}).get("peak_prob"),
                "icon_regional_peak": variants.get("regional-a5.5", {}).get("peak_prob"),
            }
        with open(os.path.join(ddir, "icon.json"), "w") as f:
            json.dump(meta, f, indent=1)
        log(f"{name}: ICON regional peak {variants.get('regional-a5.5', {}).get('peak_prob')}")

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()

