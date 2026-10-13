#!/usr/bin/env python3
"""Feed de RAYOS GOES-East (GLM-L2-LCFA) para el nowcast-monitor: convección activa.

POR QUÉ EXISTE (2026-10-12). El aluvión de Las Condes del 8-oct-2026 lo gatilló
una celda convectiva de una hora sobre el piedemonte: Tobalaba midió 8.2 mm/h y
los dos productos de lluvia satelital que lee el monitor no vieron nada (IMERG
×34 y GOES QPE ×8 por debajo). El sensor de rayos del mismo satélite SÍ la vio:
233 descargas sobre el piedemonte, con el máximo (82 en 10 min) a las 19:30Z,
media hora ANTES del aluvión. Lo que falla es la estimación de lluvia, no el
satélite: la firma eléctrica de la celda está en el bucket público con latencia
de minutos. Este feed la trae al ciclo.

QUÉ ES Y QUÉ NO ES. Cuenta descargas (flashes) por paso semihorario en la
grilla 0.1° del monitor y, aparte, dentro de cada cicatriz post-incendio vigente
(más un buffer, porque la geolocalización del GLM tiene error de pocos km). NO
mide lluvia y no toda ráfaga tiene rayos, así que NO entra a la fusión I-D ni
toca el peligro: es un CORROBORADOR de convección y un gatillo de vigilancia
reforzada (monitor.sh avisa "CONVECCIÓN sobre <cicatriz>" cuando la cuenta de
los últimos GLM_LOOKBACK_MIN minutos supera GLM_ALERT_FLASHES). Validado en un
solo evento; hay que medirlo en más antes de confiarle algo más que un aviso.

CÓMO. Archivos de 20 s (~450 KB) en s3://$GLM_BUCKET/GLM-L2-LCFA/YYYY/DDD/HH/,
acceso anónimo por HTTP. Incremental: guarda en state.json el último archivo
procesado y baja solo los nuevos (≈90 por ciclo de 30 min, ~40 MB, en memoria,
8 descargas en paralelo, nada queda en disco). Arranque en frío: solo
GLM_BACKFILL_MIN minutos hacia atrás (default 120), no la ventana entera.

Salidas en WORK_DIR (p.ej. $MON/feeds/glm):
  steps/step_YYYYmmddTHHMM.tif  float32 flashes por celda 0.1° (misma grilla
                                que GOES QPE/IMERG), se ACUMULA entre ciclos
  counts.json                   por paso: total dominio, máximo por celda y su
                                centro, flashes por cicatriz
  status.json                   salud + resumen de los últimos GLM_LOOKBACK_MIN
                                (lo lee monitor.sh)

Config por entorno (monitor.sh las exporta desde config.env): BBOX, WORK_DIR,
WINDOW_HOURS, GLM_BUCKET (noaa-goes19), GLM_LOOKBACK_MIN (60),
GLM_BACKFILL_MIN (120), GLM_SCAR_BUFFER_KM (5), GLM_STALE_HOURS (1),
POSTFIRE_SCARS (solo autotest). `--selftest` corre sin red.
"""

import glob
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetch_goes_qpe import RES, STEP, key_time, list_bucket, step_key, target_grid  # noqa: E402
from lib_raster import purge_invalid, read_json, valid_tif, write_geotiff, write_json  # noqa: E402  (atómico, 2026-10-13)

PRODUCT = "GLM-L2-LCFA"
WORKERS = 8


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"fetch_glm: falta la variable {name}")
    return v


def file_start(key):
    """OR_GLM-L2-LCFA_G19_sYYYYJJJHHMMSSs_e..._c....nc → inicio UTC."""
    tok = os.path.basename(key).split("_s")[1][:13]
    return datetime.strptime(tok, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)


def step_of(dt):
    """Clave del paso semihorario que contiene dt."""
    return step_key(dt - timedelta(minutes=dt.minute % 30, seconds=dt.second,
                                   microseconds=dt.microsecond))


def bin_flashes(lat, lon, lons, lats):
    """Flashes → cuentas por celda 0.1° (matriz north-up, filas N→S)."""
    nrows, ncols = lats.size, lons.size
    grid = np.zeros((nrows, ncols), "float32")
    if lat.size == 0:
        return grid
    ci = np.floor((lon - (lons[0] - RES / 2)) / RES).astype("int64")
    ri = np.floor((lat - (lats[0] - RES / 2)) / RES).astype("int64")
    ok = (ci >= 0) & (ci < ncols) & (ri >= 0) & (ri < nrows)
    np.add.at(grid, (nrows - 1 - ri[ok], ci[ok]), 1.0)
    return grid


def scar_boxes(buffer_km, bbox=None, hoy=None):
    """Cicatrices vigentes de scars.json → {nombre: (w, s, e, n)} en WGS84 con
    buffer; solo las que tocan `bbox` (un dominio no avisa por cicatrices de
    otra región). Sin registro o sin rasterio → {} (el feed sigue sin ellas)."""
    try:
        import rasterio
        from rasterio.warp import transform_bounds
        from susc_overlay import cargar_cicatrices
    except Exception as e:  # noqa: BLE001
        print(f"[glm] sin cicatrices ({e.__class__.__name__}: {e})")
        return {}
    out = {}
    for lst in cargar_cicatrices(hoy).values():
        for c in lst:
            try:
                with rasterio.open(c["mask_path"]) as m:
                    w, s, e, n = transform_bounds(m.crs, "EPSG:4326", *m.bounds)
            except Exception as e2:  # noqa: BLE001
                print(f"[glm] cicatriz {c['nombre']} ilegible: {e2}")
                continue
            dlat = buffer_km / 111.0
            dlon = buffer_km / (111.0 * max(0.2, np.cos(np.radians((s + n) / 2))))
            box = (w - dlon, s - dlat, e + dlon, n + dlat)
            if bbox and (box[2] < bbox[0] or box[0] > bbox[2] or box[3] < bbox[1] or box[1] > bbox[3]):
                continue
            out[c["nombre"]] = box
    return out


def in_box(lat, lon, box):
    w, s, e, n = box
    return int(((lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)).sum())


def read_flashes(raw):
    import netCDF4
    d = netCDF4.Dataset("mem", memory=raw)
    try:
        lat = np.asarray(d["flash_lat"][:], "float64")
        lon = np.asarray(d["flash_lon"][:], "float64")
    finally:
        d.close()
    return lat, lon


def fetch_one(bucket, key):
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    for intento in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return key, r.read(), None
        except Exception as e:  # noqa: BLE001
            err = e
    return key, None, err


def write_step(path, grid, lons, lats):
    import rasterio
    from rasterio.transform import from_origin
    if valid_tif(path):  # un paso corrupto no se acumula: se reemplaza
        with rasterio.open(path) as d:
            grid = grid + d.read(1)
    tr = from_origin(lons[0] - RES / 2, lats[-1] + RES / 2, RES, RES)
    write_geotiff(path, grid.astype("float32"), driver="GTiff", height=grid.shape[0],
                  width=grid.shape[1], count=1, dtype="float32", crs="EPSG:4326", transform=tr,
                  compress="deflate")


def resumen(counts, keys_recent, allowed=None):
    """Suma de los pasos recientes: dominio, celda máxima, cicatrices (solo las
    de `allowed` si se da: cuentas viejas de cicatrices fuera del dominio no
    deben resucitar en el resumen)."""
    dom = 0
    scars = {s: 0 for s in (allowed or [])}
    cell = (0, None)
    for k in keys_recent:
        c = counts.get(k)
        if not c:
            continue
        dom += c["domain"]
        for s, n in c.get("scars", {}).items():
            if allowed is not None and s not in allowed:
                continue
            scars[s] = scars.get(s, 0) + n
        if c["max_cell"] > cell[0]:
            cell = (c["max_cell"], c.get("max_cell_lonlat"))
    return {"domain": dom, "max_cell": cell[0], "max_cell_lonlat": cell[1], "scars": scars}


def selftest():
    """Sin red: binning, paso, cicatriz con buffer y resumen de ventana."""
    lons, lats = target_grid((-70.85, -34.25, -69.75, -33.05))
    fallos = []
    # (a) 3 flashes en la celda de (-70.475,-33.409) y 1 fuera del bbox
    lat = np.array([-33.41, -33.40, -33.42, -20.0]); lon = np.array([-70.47, -70.48, -70.46, -70.5])
    g = bin_flashes(lat, lon, lons, lats)
    r = int(np.where(np.isclose(lats, -33.45))[0][0]); c = int(np.where(np.isclose(lons, -70.45))[0][0])
    ok = g.sum() == 3 and g[lats.size - 1 - r, c] == 3
    print(f"[selftest glm] binning: total {g.sum():.0f} (esp. 3), celda (-70.45,-33.45) = {g[lats.size-1-r, c]:.0f} {'OK' if ok else 'FALLA'}")
    ok or fallos.append("binning")
    # (b) paso semihorario y nombre de archivo
    t = file_start("OR_GLM-L2-LCFA_G19_s20262811930200_e20262811930400_c20262811930423.nc")
    ok = step_of(t) == "20261008T1930" and t.minute == 30 and t.second == 20
    print(f"[selftest glm] archivo s20262811930200 → {t:%Y-%m-%d %H:%M:%S}Z paso {step_of(t)} {'OK' if ok else 'FALLA'}")
    ok or fallos.append("paso")
    # (c) cicatriz con buffer: caja de 0.1° y 5 km de buffer; flash a 3 km del borde entra, a 8 km no
    box = (-70.5 - 5 / 92.6, -33.45 - 5 / 111, -70.4 + 5 / 92.6, -33.35 + 5 / 111)
    ok = in_box(np.array([-33.35 + 3 / 111, -33.35 + 8 / 111]), np.array([-70.45, -70.45]), box) == 1
    print(f"[selftest glm] buffer de cicatriz 5 km: dentro a 3 km, fuera a 8 km {'OK' if ok else 'FALLA'}")
    ok or fallos.append("buffer")
    # (d) resumen de ventana
    counts = {"20261008T1900": {"domain": 10, "max_cell": 4, "max_cell_lonlat": [-70.45, -33.45], "scars": {"x": 2}},
              "20261008T1930": {"domain": 100, "max_cell": 40, "max_cell_lonlat": [-70.45, -33.35], "scars": {"x": 70}},
              "20261008T1800": {"domain": 999, "max_cell": 999, "scars": {"x": 999}}}
    s = resumen(counts, ["20261008T1900", "20261008T1930"])
    s2 = resumen(counts, ["20261008T1900", "20261008T1930"], allowed={"y"})
    ok = s["domain"] == 110 and s["max_cell"] == 40 and s["scars"]["x"] == 72 and s2["scars"] == {"y": 0}
    print(f"[selftest glm] resumen 60 min: dominio {s['domain']} (esp. 110), máx celda {s['max_cell']} (40), cicatriz {s['scars']['x']} (72) {'OK' if ok else 'FALLA'}")
    ok or fallos.append("resumen")
    print(f"\n[selftest glm] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main():
    bbox = tuple(float(x) for x in env("BBOX").split(","))
    work = env("WORK_DIR")
    window_h = float(env("WINDOW_HOURS", "54"))
    bucket = env("GLM_BUCKET", "noaa-goes19")
    lookback = int(float(env("GLM_LOOKBACK_MIN", "60")))
    backfill = int(float(env("GLM_BACKFILL_MIN", "120")))
    buffer_km = float(env("GLM_SCAR_BUFFER_KM", "5"))
    stale_h = float(env("GLM_STALE_HOURS", "1"))

    steps_dir = os.path.join(work, "steps")
    os.makedirs(steps_dir, exist_ok=True)
    purge_invalid(steps_dir, "step_*.tif", tag="[glm]")
    state_p = os.path.join(work, "state.json")
    counts_p = os.path.join(work, "counts.json")
    # state/counts de 0 bytes (disco lleno) no deben tumbar el feed: se parte de cero con aviso
    state = read_json(state_p, {}, tag="[glm]")
    counts = read_json(counts_p, {}, tag="[glm]")
    lons, lats = target_grid(bbox)
    boxes = scar_boxes(buffer_km, bbox)
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=window_h)

    fetched = errors = 0
    newest_file = datetime.fromisoformat(state["newest_file"]) if state.get("newest_file") else None
    if os.environ.get("SKIP_FETCH", "0") != "1":
        desde = max(newest_file or datetime.min.replace(tzinfo=timezone.utc),
                    now - timedelta(minutes=backfill))
        # listar las horas desde `desde` hasta ahora
        keys = []
        h = desde.replace(minute=0, second=0, microsecond=0)
        while h <= now:
            try:
                keys += list_bucket(bucket, f"{PRODUCT}/{h:%Y/%j/%H}/")
            except Exception as e:  # noqa: BLE001
                print(f"[glm] listado {h:%Y/%j/%H} falló: {e}")
                errors += 1
            h += timedelta(hours=1)
        keys = sorted(k for k in keys if k.endswith(".nc") and file_start(k) > desde)
        print(f"[glm] {len(keys)} archivos nuevos desde {desde:%Y-%m-%dT%H:%M:%S}Z")
        por_paso = {}
        with ThreadPoolExecutor(WORKERS) as ex:
            for key, raw, err in ex.map(lambda k: fetch_one(bucket, k), keys):
                if raw is None:
                    print(f"[glm] descarga falló {os.path.basename(key)}: {err}")
                    errors += 1
                    continue
                try:
                    lat, lon = read_flashes(raw)
                except Exception as e:  # noqa: BLE001
                    print(f"[glm] archivo ilegible {os.path.basename(key)}: {e}")
                    errors += 1
                    continue
                fetched += 1
                t = file_start(key)
                if newest_file is None or t > newest_file:
                    newest_file = t
                k = step_of(t)
                acc = por_paso.setdefault(k, {"grid": np.zeros((lats.size, lons.size), "float32"),
                                              "scars": {s: 0 for s in boxes}})
                acc["grid"] += bin_flashes(lat, lon, lons, lats)
                for s, box in boxes.items():
                    acc["scars"][s] += in_box(lat, lon, box)
        for k, acc in sorted(por_paso.items()):
            write_step(os.path.join(steps_dir, f"step_{k}.tif"), acc["grid"], lons, lats)
            prev = counts.get(k, {"domain": 0, "max_cell": 0, "max_cell_lonlat": None, "scars": {}})
            # el máximo por celda se recalcula sobre el paso acumulado
            import rasterio
            with rasterio.open(os.path.join(steps_dir, f"step_{k}.tif")) as d:
                g = d.read(1)
            r, c = np.unravel_index(int(g.argmax()), g.shape)
            counts[k] = {
                "domain": int(prev["domain"] + acc["grid"].sum()),
                "max_cell": int(g.max()),
                "max_cell_lonlat": [float(lons[c]), float(lats[lats.size - 1 - r])] if g.max() > 0 else None,
                "scars": {s: int(prev["scars"].get(s, 0) + n) for s, n in acc["scars"].items()},
            }
            print(f"[glm] paso {k}: +{acc['grid'].sum():.0f} flashes (total {counts[k]['domain']}), "
                  f"máx celda {counts[k]['max_cell']}, cicatrices {counts[k]['scars']}")

    # rotación
    for p in glob.glob(os.path.join(steps_dir, "step_*.tif")):
        k = os.path.basename(p)[5:-4]
        if key_time(k) < first:
            os.remove(p)
            counts.pop(k, None)
    counts = {k: v for k, v in counts.items() if key_time(k) >= first}
    recent_keys = [k for k in counts if key_time(k) >= now - timedelta(minutes=lookback + 30)]
    rec = resumen(counts, recent_keys, set(boxes))
    rec["minutes"] = lookback
    top_scar = max(rec["scars"].items(), key=lambda kv: kv[1]) if rec["scars"] else (None, 0)
    rec["top_scar"] = {"nombre": top_scar[0], "flashes": top_scar[1]}
    stale_min = (now - newest_file).total_seconds() / 60 if newest_file else None
    status = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed": f"glm:{bucket}",
        "newest_step": step_of(newest_file) if newest_file else None,
        "newest_file": newest_file.isoformat() if newest_file else None,
        "stale_minutes": stale_min,
        "stale_hours_threshold": stale_h,
        "n_steps": len(counts),
        "n_gaps": 0,
        "gap_fraction": 0.0,
        "fetched_this_cycle": fetched,
        "fetch_errors": errors,
        "scars": sorted(boxes),
        "recent": rec,
    }
    write_json(os.path.join(work, "status.json"), status, indent=1)
    write_json(counts_p, counts, indent=1)
    write_json(state_p, {"newest_file": status["newest_file"]})
    print(f"[glm] status: {json.dumps(status)}")
    return 0 if errors == 0 or fetched > 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    rc = main()
    # Salida DURA, como fetch_goes_qpe: este proceso mezcla netCDF4 (HDF5) y
    # rasterio (GDAL) y el teardown normal del intérprete puede terminar en
    # segfault con todo ya escrito (contrato de salida, selftest --exit-contract;
    # faltaba aquí desde que nació el feed, 2026-10-12).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
