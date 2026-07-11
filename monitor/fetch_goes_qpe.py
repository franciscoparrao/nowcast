#!/usr/bin/env python3
"""Fetcher incremental de QPE GOES-East (ABI-L2-RRQPEF) para el nowcast-monitor.

Segunda forzante del monitor (fase A del plan EWS): tasa de lluvia satelital
geoestacionaria cada 10 minutos con latencia de MINUTOS — el complemento de
baja latencia de IMERG Early (~4-5 h). Calidad inferior a radar (Chile no tiene
radar meteorológico público operacional); se fusiona por noisy-OR, no reemplaza.

Cada ciclo:
  1. Lista en el bucket público AWS (`s3://$GOES_BUCKET/ABI-L2-RRQPEF/`, acceso
     anónimo por HTTP, sin credenciales) los gránulos full-disk de la ventana
     rodante que aún no están convertidos.
  2. Descarga solo los nuevos (~1.5 MB c/u), recorta el BBOX reproyectando
     DESDE la grilla fija geoestacionaria del ABI (fórmulas del GOES-R PUG,
     sin dependencias nuevas) y guarda un GeoTIFF de lámina (mm por 10 min)
     por gránulo en granules/. El NetCDF crudo se borra al tiro.
  3. Ensambla pasos semihorarios step_YYYYmmddTHHMM.tif (suma de los 3
     gránulos del bloque) EN LA MISMA GRILLA 0.1° que el fetcher IMERG —
     centros en el convenio IMERG — para que `nowcast run --fuse-rasters`
     acepte la fusión (chequeo same_grid del CLI). Pasos con <3 gránulos solo
     se finalizan cuando ya no van a completarse, y el déficit se cuenta.
  4. Huecos viejos → raster de ceros MARCADO (gapmark_*), como IMERG: nada se
     imputa en silencio; el conteo viaja en status.json y monitor.sh degrada.
  5. Rota la ventana y escribe status.json (mismo esquema que el feed IMERG).

Config por variables de entorno (las exporta monitor.sh desde config.env):
BBOX, WORK_DIR (raíz del feed GOES, p.ej. $MON/feeds/goes), WINDOW_HOURS,
STALE_HOURS, GOES_BUCKET (default noaa-goes19 = GOES-East vigente). Uso:
    BBOX="-70.75,-34.25,-69.75,-33.25" WORK_DIR=/tmp/goes python3 fetch_goes_qpe.py
"""

import glob
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import numpy as np

STEP = timedelta(minutes=30)
GRANULE = timedelta(minutes=10)
PRODUCT = "ABI-L2-RRQPEF"
RES = 0.1  # grilla destino, convenio IMERG (centros en …x.x5 ± múltiplos de 0.1)
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"fetch_goes_qpe: falta la variable {name}")
    return v


def step_key(dt):
    return dt.strftime("%Y%m%dT%H%M")


def key_time(key):
    return datetime.strptime(key, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def granule_start(name):
    """Inicio desde OR_ABI-L2-RRQPEF-M6_G19_sYYYYJJJHHMMSSs_e..._c....nc"""
    tok = name.split("_s")[1][:13]  # YYYYJJJHHMMSS
    return datetime.strptime(tok, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)


def target_grid(bbox):
    """Centros de la grilla 0.1° destino, convenio IMERG (idéntico al feed
    IMERG: centros de la grilla global GPM dentro del bbox), para que ambos
    feeds compartan geotransform exacto y el CLI acepte fusionarlos."""
    w, s, e, n = bbox

    def centers(lo, hi):
        first = math.floor(lo / RES) * RES + RES / 2
        if first < lo - 1e-9:
            first += RES
        out = []
        c = first
        while c <= hi + 1e-9:
            out.append(round(c, 6))
            c += RES
        return np.array(out)

    return centers(w, e), centers(s, n)  # lons (W→E), lats (S→N)


def list_bucket(bucket, prefix):
    """Claves bajo un prefijo del bucket público (paginado, anónimo)."""
    keys, token = [], None
    while True:
        url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&prefix={prefix}"
        if token:
            token_q = urllib.parse.quote(token, safe="")
            url += f"&continuation-token={token_q}"
        with urllib.request.urlopen(url, timeout=60) as r:
            tree = ElementTree.parse(r).getroot()
        keys += [k.text for k in tree.iter(f"{S3_NS}Key")]
        nxt = tree.find(f"{S3_NS}NextContinuationToken")
        if nxt is None:
            return keys
        token = nxt.text


# --- Proyección geoestacionaria ABI (GOES-R PUG vol. 3, 5.1.2.8) -------------
# Sin pyproj: fórmulas cerradas del PUG, validadas en el selftest con el punto
# subsatelital (x=y=0 → lat 0, lon lon_0) y contra granulos reales.


def geos_forward(lat_deg, lon_deg, p):
    """(lat, lon) → (x, y) en radianes de la grilla fija (sweep x)."""
    req, rpol, H, lam0 = p["req"], p["rpol"], p["H"], p["lam0"]
    e2 = (req**2 - rpol**2) / req**2
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    latc = np.arctan((rpol**2 / req**2) * np.tan(lat))
    rc = rpol / np.sqrt(1.0 - e2 * np.cos(latc) ** 2)
    sx = H - rc * np.cos(latc) * np.cos(lon - lam0)
    sy = -rc * np.cos(latc) * np.sin(lon - lam0)
    sz = rc * np.sin(latc)
    x = np.arcsin(-sy / np.sqrt(sx**2 + sy**2 + sz**2))
    y = np.arctan(sz / sx)
    return x, y


def geos_inverse(x, y, p):
    """(x, y) radianes → (lat, lon) grados; NaN fuera del disco."""
    req, rpol, H, lam0 = p["req"], p["rpol"], p["H"], p["lam0"]
    with np.errstate(invalid="ignore"):
        a = np.sin(x) ** 2 + np.cos(x) ** 2 * (
            np.cos(y) ** 2 + (req**2 / rpol**2) * np.sin(y) ** 2
        )
        b = -2.0 * H * np.cos(x) * np.cos(y)
        c = H**2 - req**2
        disc = b**2 - 4.0 * a * c
        rs = (-b - np.sqrt(disc)) / (2.0 * a)
        sx = rs * np.cos(x) * np.cos(y)
        sy = -rs * np.sin(x)
        sz = rs * np.cos(x) * np.sin(y)
        lat = np.degrees(np.arctan((req**2 / rpol**2) * sz / np.sqrt((H - sx) ** 2 + sy**2)))
        lon = np.degrees(lam0 - np.arctan(sy / (H - sx)))
    return lat, lon


def convert(nc_path, bbox, out_tif):
    """NetCDF full disk → GeoTIFF del bbox en mm por gránulo (10 min)."""
    import rasterio
    import xarray as xr
    from rasterio.transform import from_origin

    lons, lats = target_grid(bbox)
    ds = xr.open_dataset(nc_path)
    proj = ds["goes_imager_projection"].attrs
    p = {
        "req": float(proj["semi_major_axis"]),
        "rpol": float(proj["semi_minor_axis"]),
        "H": float(proj["perspective_point_height"]) + float(proj["semi_major_axis"]),
        "lam0": math.radians(float(proj["longitude_of_projection_origin"])),
    }
    # Recorte en la grilla fija: proyectar el bbox (con margen) a (x, y).
    corners_lat = np.array([bbox[1], bbox[1], bbox[3], bbox[3]])
    corners_lon = np.array([bbox[0], bbox[2], bbox[0], bbox[2]])
    cx, cy = geos_forward(corners_lat, corners_lon, p)
    pad = 0.0005  # ~9 px de 56 µrad: margen para el binning del borde
    xv, yv = ds["x"].values, ds["y"].values
    xi = np.where((xv >= cx.min() - pad) & (xv <= cx.max() + pad))[0]
    yi = np.where((yv >= cy.min() - pad) & (yv <= cy.max() + pad))[0]
    if xi.size == 0 or yi.size == 0:
        ds.close()
        raise RuntimeError("el bbox cae fuera del disco GOES")
    sub = ds["RRQPE"].isel(x=slice(xi[0], xi[-1] + 1), y=slice(yi[0], yi[-1] + 1))
    rate = np.asarray(sub.values, "float64")  # mm/h, NaN = fill/fuera de disco
    dqf = np.asarray(
        ds["DQF"].isel(x=slice(xi[0], xi[-1] + 1), y=slice(yi[0], yi[-1] + 1)).values
    )
    xs, ys = np.meshgrid(xv[xi[0] : xi[-1] + 1], yv[yi[0] : yi[-1] + 1])
    ds.close()

    lat, lon = geos_inverse(xs, ys, p)
    # DQF 0 = good; el resto (incl. fuera de disco) no aporta. NaN/negativo → 0.
    good = (dqf == 0) & np.isfinite(rate)
    rate = np.where(good, np.clip(rate, 0.0, None), np.nan)

    # Binning por promedio a la grilla 0.1° (≈25 px ABI de 2 km por celda).
    w_edge = lons[0] - RES / 2
    s_edge = lats[0] - RES / 2
    ncols, nrows = lons.size, lats.size
    ci = np.floor((lon - w_edge) / RES).astype("int64")
    ri = np.floor((lat - s_edge) / RES).astype("int64")
    inside = (ci >= 0) & (ci < ncols) & (ri >= 0) & (ri < nrows) & np.isfinite(rate)
    flat = ri[inside] * ncols + ci[inside]
    sums = np.bincount(flat, weights=rate[inside], minlength=nrows * ncols)
    counts = np.bincount(flat, minlength=nrows * ncols)
    mean_rate = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    coverage = float((counts > 0).sum()) / (nrows * ncols)

    depth = (mean_rate * (GRANULE.total_seconds() / 3600.0)).reshape(nrows, ncols)
    grid = depth[::-1, :].astype("float32")  # S→N a north-up
    transform = from_origin(w_edge, lats[-1] + RES / 2, RES, RES)
    with rasterio.open(
        out_tif, "w", driver="GTiff", height=nrows, width=ncols, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(grid, 1)
    return (nrows, ncols), coverage


def zero_raster_like(ref_tif, out_tif):
    import rasterio

    with rasterio.open(ref_tif) as src:
        profile = src.profile
        shape = (src.height, src.width)
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(np.zeros(shape, dtype="float32"), 1)


def main():
    bbox = tuple(float(x) for x in env("BBOX").split(","))
    work = env("WORK_DIR")
    window_h = float(env("WINDOW_HOURS", "54"))
    stale_h = float(env("STALE_HOURS", "1"))  # GOES publica en minutos
    bucket = env("GOES_BUCKET", "noaa-goes19")

    steps_dir = os.path.join(work, "steps")
    gran_dir = os.path.join(work, "granules")
    raw_dir = os.path.join(work, "raw")
    for d in (steps_dir, gran_dir, raw_dir):
        os.makedirs(d, exist_ok=True)

    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=window_h)
    first = (t0 + (STEP - timedelta(minutes=t0.minute % 30, seconds=t0.second,
                                    microseconds=t0.microsecond)) % STEP)

    have_gran = {
        os.path.basename(p)[len("gran_"):-len(".tif")]
        for p in glob.glob(os.path.join(gran_dir, "gran_*.tif"))
    }
    done_steps = {
        os.path.basename(p)[len("step_"):-len(".tif")]
        for p in glob.glob(os.path.join(steps_dir, "step_*.tif"))
    }

    # --- 1-2. listar por hora y bajar solo gránulos nuevos --------------------
    fetched, failed = 0, 0
    low_coverage = 0
    if os.environ.get("SKIP_FETCH", "0") != "1":
        hours, t = [], first.replace(minute=0)
        while t <= now:
            hours.append(t)
            t += timedelta(hours=1)
        for hour in hours:
            prefix = f"{PRODUCT}/{hour.year}/{hour.strftime('%j')}/{hour.strftime('%H')}/"
            try:
                keys = list_bucket(bucket, prefix)
            except Exception as exc:
                failed += 1
                print(f"[goes] ERROR listando {prefix}: {exc}", file=sys.stderr)
                continue
            for key in keys:
                name = os.path.basename(key)
                try:
                    start = granule_start(name)
                except Exception:
                    continue
                gkey = start.strftime("%Y%m%dT%H%M")
                skey = step_key(start - timedelta(minutes=start.minute % 30))
                if gkey in have_gran or skey in done_steps or start < first:
                    continue
                nc = os.path.join(raw_dir, name)
                gt = os.path.join(gran_dir, f"gran_{gkey}.tif")
                try:
                    urllib.request.urlretrieve(
                        f"https://{bucket}.s3.amazonaws.com/{key}", nc
                    )
                    shape, coverage = convert(nc, bbox, gt)
                    fetched += 1
                    if coverage < 0.99:
                        low_coverage += 1
                    print(f"[goes] {gkey} → {gt} {shape} cobertura={coverage:.2f}")
                except Exception as exc:
                    failed += 1
                    print(f"[goes] ERROR con {name}: {exc}", file=sys.stderr)
                finally:
                    try:
                        os.remove(nc)
                    except OSError:
                        pass

    # --- 3. ensamblar pasos semihorarios (3 gránulos = 1 paso) ----------------
    import rasterio

    grans = sorted(glob.glob(os.path.join(gran_dir, "gran_*.tif")))
    by_step = {}
    for g in grans:
        gt = key_time(os.path.basename(g)[len("gran_"):-len(".tif")])
        by_step.setdefault(step_key(gt - timedelta(minutes=gt.minute % 30)), []).append(g)
    partial_steps = 0
    for skey, files in sorted(by_step.items()):
        st = key_time(skey)
        complete = len(files) >= 3
        # Un paso incompleto solo se finaliza cuando ya no puede completarse
        # (más viejo que el paso siguiente + margen de publicación).
        expired = now - st > STEP + timedelta(minutes=30)
        if not complete and not expired:
            continue
        out = os.path.join(steps_dir, f"step_{skey}.tif")
        if skey not in done_steps:
            total = None
            profile = None
            for f in files:
                with rasterio.open(f) as src:
                    a = src.read(1)
                    profile = profile or src.profile
                total = a if total is None else total + a
            if not complete:
                # Déficit honesto: escalar el promedio disponible al bloque
                # completo sería inventar lluvia; sumar lo que hay la subestima.
                # Se elige subestimar (conservador para I-D) y CONTAR.
                partial_steps += 1
                print(f"[goes] paso {skey} finalizado PARCIAL ({len(files)}/3 gránulos)")
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(total.astype("float32"), 1)
            done_steps.add(skey)
        for f in files:
            os.remove(f)

    # --- 4. huecos viejos → cero MARCADO (política idéntica a IMERG) ----------
    have = {
        os.path.basename(p)[len("step_"):-len(".tif")]
        for p in glob.glob(os.path.join(steps_dir, "step_*.tif"))
    }
    real = {k for k in have if not os.path.exists(os.path.join(steps_dir, f"gapmark_{k}"))}
    newest_real = max((key_time(k) for k in real), default=None)
    gaps = []
    if newest_real is not None:
        t = first
        while t <= newest_real:
            k = step_key(t)
            if k not in have:
                gaps.append(k)
            t += STEP
        ref = sorted(glob.glob(os.path.join(steps_dir, "step_*.tif")))
        for k in gaps:
            if not ref:
                break
            out = os.path.join(steps_dir, f"step_{k}.tif")
            zero_raster_like(ref[0], out)
            open(os.path.join(steps_dir, f"gapmark_{k}"), "w").close()
            print(f"[goes] hueco {k} rellenado con 0 mm (MARCADO)")

    # --- 5. rotación + status -------------------------------------------------
    for p in glob.glob(os.path.join(steps_dir, "step_*.tif")):
        k = os.path.basename(p)[len("step_"):-len(".tif")]
        if key_time(k) < first:
            os.remove(p)
            mark = os.path.join(steps_dir, f"gapmark_{k}")
            if os.path.exists(mark):
                os.remove(mark)

    n_steps = len(glob.glob(os.path.join(steps_dir, "step_*.tif")))
    n_gap_marks = len(glob.glob(os.path.join(steps_dir, "gapmark_*")))
    stale_min = (now - newest_real).total_seconds() / 60 if newest_real else None
    status = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed": f"goes-qpe:{bucket}",
        "newest_step": step_key(newest_real) if newest_real else None,
        "stale_minutes": stale_min,
        "n_steps": n_steps,
        "n_gaps": n_gap_marks,
        "gap_fraction": (n_gap_marks / n_steps) if n_steps else 1.0,
        "fetched_this_cycle": fetched,
        "convert_failures": failed,
        "partial_steps": partial_steps,
        "low_coverage_granules": low_coverage,
        "stale_hours_threshold": stale_h,
    }
    with open(os.path.join(work, "status.json"), "w") as f:
        json.dump(status, f, indent=1)
    print(f"[goes] status: {json.dumps(status)}")


if __name__ == "__main__":
    main()
