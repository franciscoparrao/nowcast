#!/usr/bin/env python3
"""Fetcher incremental de IMERG Early NRT para el nowcast-monitor.

Cada ciclo:
  1. Busca en CMR los gránulos GPM_3IMERGHHE v07 (Early, ~4 h de latencia) de
     la ventana rodante [ahora - WINDOW_HOURS, ahora] que aún no están
     convertidos localmente.
  2. Descarga solo los nuevos (Earthdata en ~/.netrc, app GES DISC autorizada),
     recorta el BBOX y escribe un GeoTIFF por paso (mm por media hora) en
     $WORK_DIR/steps/step_YYYYmmddTHHMM.tif. El HDF5 crudo se borra al tiro:
     en disco solo viven recortes de KB.
  3. Rellena huecos VIEJOS (pasos faltantes ya fuera de la latencia esperada)
     con rasters de cero lluvia, MARCÁNDOLOS en status.json — el motor exige un
     eje temporal contiguo y rechaza NaN por diseño; imputar cero en silencio
     sería el pecado que las auditorías pasaron 3 rondas matando, así que el
     conteo de huecos viaja al reporte y monitor.sh degrada la corrida si pasa
     de MAX_GAP_FRACTION.
  4. Rota: borra pasos más viejos que la ventana y escribe status.json
     (paso más nuevo, staleness, huecos, conteos) para monitor.sh.

Reusa la lógica de lectura probada de scripts/extract_event_imerg.py (xarray
sobre group="Grid", layout lon-major, tasa mm/h × 0.5 = lámina por paso).

Config por variables de entorno (las exporta monitor.sh desde config.env):
BBOX, WORK_DIR, WINDOW_HOURS, STALE_HOURS. Uso directo:
    BBOX="-70.75,-34.25,-69.75,-33.25" WORK_DIR=/tmp/mon python3 fetch_imerg_early.py
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

STEP = timedelta(minutes=30)
SHORT_NAME = "GPM_3IMERGHHE"  # IMERG Early half-hourly (NRT)
VERSION = "07"


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"fetch_imerg_early: falta la variable {name}")
    return v


def step_key(dt):
    return dt.strftime("%Y%m%dT%H%M")


def key_time(key):
    return datetime.strptime(key, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def granule_start(path):
    """Hora de inicio desde el nombre 3B-HHR-E...3IMERG.YYYYMMDD-S HHMMSS-..."""
    tok = os.path.basename(path).split(".3IMERG.")[1]
    return datetime.strptime(
        tok.split("-")[0] + tok.split("-S")[1][:6], "%Y%m%d%H%M%S"
    ).replace(tzinfo=timezone.utc)


def convert(hdf5_path, bbox, out_tif):
    """HDF5 global → GeoTIFF del bbox en mm por paso (media hora), north-up.

    La grilla destino se construye ANALÍTICAMENTE (target_grid, el mismo
    convenio que los fetchers GOES y DGA) y las celdas IMERG se seleccionan
    por centro más cercano. Derivarla de las coordenadas del archivo (slice)
    rompía la fusión: los lon/lat vienen en float32 y un borde de bbox no
    representable exacto (p.ej. -72.05) pierde la columna del borde y corre
    el origen ~1e-6 — same_grid del CLI lo rechaza (correctamente). Con la
    grilla analítica los tres feeds son bit-idénticos en georef por
    construcción, para cualquier bbox.
    """
    import rasterio
    import xarray as xr
    from rasterio.transform import from_origin

    from fetch_goes_qpe import target_grid

    lons_t, lats_t = target_grid(bbox)
    ds = xr.open_dataset(hdf5_path, group="Grid")
    var = "precipitation" if "precipitation" in ds else "precipitationCal"
    da = (
        ds[var]
        .sel(lon=xr.DataArray(lons_t, dims="lon"), lat=xr.DataArray(lats_t, dims="lat"),
             method="nearest")
        .squeeze("time")
    )
    # (lon, lat), tasa mm/h → lámina mm por media hora; NaN/negativos → 0
    # (política idéntica al extractor probado y al bridge de nowcast-surtgis).
    a = np.clip(np.nan_to_num(np.asarray(da.values, "float64"), nan=0.0), 0.0, None) * 0.5
    ds.close()
    # A (lat, lon) con filas norte→sur para un GeoTIFF north-up estándar.
    grid = a.T[::-1, :].astype("float32")
    res = 0.1
    transform = from_origin(lons_t[0] - res / 2, lats_t[-1] + res / 2, res, res)
    with rasterio.open(
        out_tif, "w", driver="GTiff", height=grid.shape[0], width=grid.shape[1],
        count=1, dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(grid, 1)
    return grid.shape


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
    stale_h = float(env("STALE_HOURS", "6"))

    steps_dir = os.path.join(work, "steps")
    raw_dir = os.path.join(work, "raw")
    os.makedirs(steps_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=window_h)
    # Primer paso alineado a la media hora dentro de la ventana.
    first = (t0 + (STEP - timedelta(minutes=t0.minute % 30, seconds=t0.second,
                                    microseconds=t0.microsecond)) % STEP)

    have = {
        os.path.basename(p)[len("step_"):-len(".tif")]
        for p in glob.glob(os.path.join(steps_dir, "step_*.tif"))
    }

    # --- 1-2. buscar y bajar solo lo nuevo -----------------------------------
    fetched, failed = 0, 0
    if os.environ.get("SKIP_FETCH", "0") != "1":
        import earthaccess

        earthaccess.login(strategy="netrc")
        granules = earthaccess.search_data(
            short_name=SHORT_NAME, version=VERSION,
            temporal=(t0.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M")),
            bounding_box=bbox,
        )
        # earthaccess nombra por el link de descarga; filtramos por hora S.
        new = []
        for g in granules:
            try:
                name = g.data_links()[0].rsplit("/", 1)[1]
                start = granule_start(name)
            except Exception:
                new.append(g)  # nombre inesperado: bajar y decidir después
                continue
            if step_key(start) not in have and start >= first:
                new.append(g)
        if new:
            print(f"[fetch] {len(new)} gránulo(s) nuevo(s) de {len(granules)} en ventana")
        # UNO A LA VEZ (bajar → convertir → borrar): un arranque en frío del
        # lote completo son ~800 MB simultáneos en disco, letal en un nodo
        # chico como sentinel (57 GB al 96 %). Así el pico transitorio queda
        # acotado a UN gránulo (~10 MB) sin importar el tamaño de la ventana.
        for g in new:
            try:
                earthaccess.download([g], local_path=raw_dir)
            except Exception as exc:
                failed += 1
                print(f"[fetch] ERROR bajando gránulo: {exc}", file=sys.stderr)
                continue
            for hdf5 in sorted(glob.glob(os.path.join(raw_dir, "*.HDF5"))):
                try:
                    key = step_key(granule_start(hdf5))
                    out = os.path.join(steps_dir, f"step_{key}.tif")
                    if not os.path.exists(out):
                        shape = convert(hdf5, bbox, out)
                        fetched += 1
                        print(f"[fetch] {key} → {out} {shape}")
                except Exception as exc:  # un gránulo corrupto no tumba el ciclo
                    failed += 1
                    print(f"[fetch] ERROR convirtiendo {hdf5}: {exc}", file=sys.stderr)
                finally:
                    try:
                        os.remove(hdf5)
                    except OSError:
                        pass

    # --- 3. huecos: rellenar con cero SOLO pasos viejos, y contarlos ---------
    have = {
        os.path.basename(p)[len("step_"):-len(".tif")]
        for p in glob.glob(os.path.join(steps_dir, "step_*.tif"))
    }
    real = {k for k in have if not os.path.exists(os.path.join(steps_dir, f"gapmark_{k}"))}
    newest_real = max((key_time(k) for k in real), default=None)

    expected, gaps = [], []
    t = first
    horizon = newest_real if newest_real is not None else now - timedelta(hours=stale_h)
    while t <= horizon:
        k = step_key(t)
        expected.append(k)
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
        print(f"[fetch] hueco {k} rellenado con 0 mm (MARCADO)")

    # --- 4. rotación + status -------------------------------------------------
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
        "newest_step": step_key(newest_real) if newest_real else None,
        "stale_minutes": stale_min,
        "n_steps": n_steps,
        "n_gaps": n_gap_marks,
        "gap_fraction": (n_gap_marks / n_steps) if n_steps else 1.0,
        "fetched_this_cycle": fetched,
        "convert_failures": failed,
    }
    with open(os.path.join(work, "status.json"), "w") as f:
        json.dump(status, f, indent=1)
    print(f"[fetch] status: {json.dumps(status)}")


if __name__ == "__main__":
    main()
