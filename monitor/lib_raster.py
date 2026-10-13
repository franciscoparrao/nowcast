#!/usr/bin/env python3
"""Escritura ATÓMICA y validación de los archivos de ventana del monitor
(2026-10-13).

POR QUÉ. El 9-oct-2026 a las 22:15 Chile, con el disco al 99 %, los fetchers de
monitor@atacama dejaron cinco GeoTIFF de 0 bytes (un gránulo y un paso de GOES
en la ventana, tres copias en archive/). `rasterio.open(path, "w")` crea el
archivo antes de escribir: si el disco se llena a medio camino, queda un archivo
con nombre válido y sin firma TIFF. Desde ese momento `fetch_goes_qpe` moría al
abrir el gránulo vacío y el motor al leer el paso vacío ("TIFF signature not
found"), en CADA ciclo, durante 73 horas: el vigía lo avisaba cada 6 h y nadie
lo leyó como «dominio caído». Un archivo corrupto no debe bloquear ningún ciclo
siguiente: se escribe en un temporal del mismo directorio y se renombra al final
(`os.replace` es atómico en POSIX), y lo que ya está en la ventana se VALIDA
antes de reutilizarlo; lo inválido se borra con aviso y el fetcher lo vuelve a
bajar si la fuente aún lo sirve (o lo rellena como hueco MARCADO si no).

Uso: `write_geotiff`, `write_json`, `valid_tif`, `purge_invalid`.
`python3 lib_raster.py --selftest` (sin red).
"""
import glob
import json
import os
import sys

PART = ".part"


def _tmp_name(path):
    return f"{path}{PART}-{os.getpid()}"


def write_geotiff(path, array, **profile):
    """Escribe `array` (2-D) como GeoTIFF de una banda en `path` de forma
    atómica: temporal en el mismo directorio → `os.replace`. Si la escritura
    falla (disco lleno, señal), no queda NADA con el nombre final."""
    import rasterio
    tmp = _tmp_name(path)
    try:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(array, 1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj, **kw):
    """json.dump atómico (mismo contrato que write_geotiff)."""
    tmp = _tmp_name(path)
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, **kw)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default, tag="[lib_raster]"):
    """json.load tolerante: archivo ausente, vacío o corrupto → `default` con
    aviso (un state.json de 0 bytes no debe tumbar el ciclo)."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        print(f"{tag} AVISO: {path} ilegible ({exc}); se parte de cero", file=sys.stderr)
        return default


def valid_tif(path):
    """True si el archivo existe, no está vacío y rasterio lo abre y lee la
    banda 1 (los rásters del monitor son chicos: leerlos cuesta menos que un
    ciclo caído)."""
    try:
        if os.path.getsize(path) == 0:
            return False
        import rasterio
        with rasterio.open(path) as d:
            d.read(1)
        return True
    except Exception:  # noqa: BLE001  (RasterioIOError, OSError, …)
        return False


def purge_invalid(dir_, pattern="*.tif", tag="[lib_raster]"):
    """Borra de `dir_` los GeoTIFF que no pasan `valid_tif` y los temporales
    `*.part-*` huérfanos de corridas interrumpidas. Devuelve las rutas borradas.
    Nunca lanza: un error borrando se reporta y se sigue."""
    removed = []
    if not os.path.isdir(dir_):
        return removed
    for p in sorted(glob.glob(os.path.join(dir_, f"*{PART}-*"))):
        try:
            os.remove(p)
            removed.append(p)
            print(f"{tag} temporal huérfano borrado: {p}", file=sys.stderr)
        except OSError as exc:
            print(f"{tag} AVISO: no se pudo borrar {p}: {exc}", file=sys.stderr)
    for p in sorted(glob.glob(os.path.join(dir_, pattern))):
        if valid_tif(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        try:
            os.remove(p)
            removed.append(p)
            print(f"{tag} AVISO: {p} corrupto ({size} bytes, sin firma TIFF legible): "
                  f"borrado; se vuelve a bajar o queda como hueco marcado", file=sys.stderr)
        except OSError as exc:
            print(f"{tag} AVISO: no se pudo borrar {p}: {exc}", file=sys.stderr)
    return removed


def selftest():
    import tempfile
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    fallos = []
    prof = dict(driver="GTiff", height=3, width=4, count=1, dtype="float32", crs="EPSG:4326",
                transform=from_origin(-71.0, -38.0, 0.1, 0.1))
    with tempfile.TemporaryDirectory() as td:
        # (a) escritura normal → válido, sin temporal residual
        p = os.path.join(td, "step_a.tif")
        write_geotiff(p, np.ones((3, 4), "float32"), **prof)
        ok = valid_tif(p) and not glob.glob(os.path.join(td, "*.part-*"))
        print(f"  {'ok  ' if ok else 'FALLA'} escritura atómica: válido y sin temporal")
        fallos += [] if ok else ["escritura"]
        # (b) escritura que falla DESPUÉS de crear el archivo (rasterio abre y
        #     luego no puede escribir un array de texto) → no queda final ni temporal
        p2 = os.path.join(td, "step_b.tif")
        try:
            write_geotiff(p2, np.full((3, 4), "x"), **prof)
            raised = False
        except Exception:
            raised = True
        ok = raised and not os.path.exists(p2) and not glob.glob(os.path.join(td, "*.part-*"))
        print(f"  {'ok  ' if ok else 'FALLA'} escritura fallida: lanza y no deja nada")
        fallos += [] if ok else ["fallida"]
        # (c) sobreescritura atómica: el archivo viejo sigue válido hasta el replace
        write_geotiff(p, np.full((3, 4), 2.0, "float32"), **prof)
        with rasterio.open(p) as d:
            ok = float(d.read(1).max()) == 2.0
        print(f"  {'ok  ' if ok else 'FALLA'} sobreescritura: contenido nuevo")
        fallos += [] if ok else ["sobreescritura"]
        # (d) purge: 0 bytes, basura y temporal huérfano se van; el válido queda
        z = os.path.join(td, "step_zero.tif"); open(z, "w").close()
        g = os.path.join(td, "step_garbage.tif"); open(g, "wb").write(b"no soy un tiff" * 10)
        h = os.path.join(td, "step_h.tif.part-999"); open(h, "w").write("x")
        gone = purge_invalid(td, tag="[selftest]")
        ok = (set(map(os.path.basename, gone)) == {"step_zero.tif", "step_garbage.tif", "step_h.tif.part-999"}
              and os.path.exists(p) and not os.path.exists(z) and not os.path.exists(g))
        print(f"  {'ok  ' if ok else 'FALLA'} purge: borra 0 bytes, basura y temporal; conserva el válido")
        fallos += [] if ok else ["purge"]
        # (e) json atómico + lectura tolerante
        j = os.path.join(td, "state.json")
        write_json(j, {"a": 1})
        open(os.path.join(td, "vacio.json"), "w").close()
        ok = (read_json(j, {}) == {"a": 1} and read_json(os.path.join(td, "vacio.json"), {"d": 0}) == {"d": 0}
              and read_json(os.path.join(td, "nada.json"), 7) == 7)
        print(f"  {'ok  ' if ok else 'FALLA'} json: escritura atómica y lectura tolerante a vacío/ausente")
        fallos += [] if ok else ["json"]
    print(f"\n[selftest lib_raster] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else 0)
