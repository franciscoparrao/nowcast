#!/usr/bin/env python3
"""IMERG Early semihorario sobre Coelemu, 26 al 31 de julio de 2026 (UTC).

Fuente del evento para la climatología de la Discusión del paper (§6, «the
climatological reference»): la repetición del 31-jul corrió con IMERG Early,
que es lo que tenía el monitor en vivo. Uno a la vez: bajar → recortar la caja
→ borrar el HDF5 (disco al 99 %). Reanudable: un paso ya escrito se salta.

Uso (venv del monitor): python descargar_imerg.py [--out DIR]
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "monitor"))
from fetch_imerg_early import SHORT_NAME, VERSION, convert, granule_start, step_key  # noqa: E402

BBOX = (-73.0, -36.8, -72.3, -36.2)   # Coelemu y alrededores (lon0, lat0, lon1, lat1)
T0 = datetime(2026, 7, 26, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "data", "coelemu_2026", "imerg_early"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    import earthaccess
    earthaccess.login(strategy="netrc")
    gr = earthaccess.search_data(short_name=SHORT_NAME, version=VERSION,
                                 temporal=(T0.strftime("%Y-%m-%d %H:%M"), (T1 - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")),
                                 bounding_box=BBOX)
    print(f"{len(gr)} gránulos en CMR", flush=True)
    hechos = fallos = 0
    with tempfile.TemporaryDirectory(dir=a.out) as tmp:
        for g in gr:
            name = g.data_links()[0].rsplit("/", 1)[1]
            t = granule_start(name)
            if not (T0 <= t < T1):
                continue
            out = os.path.join(a.out, f"step_{step_key(t)}.tif")
            if os.path.exists(out):
                continue
            try:
                earthaccess.download([g], local_path=tmp)
                h5 = os.path.join(tmp, name)
                convert(h5, BBOX, out)
                os.remove(h5)
                hechos += 1
            except Exception as exc:  # noqa: BLE001
                fallos += 1
                print(f"ERROR {name}: {exc}", file=sys.stderr, flush=True)
            if hechos % 24 == 0 and hechos:
                print(f"{hechos} pasos", flush=True)
    n = len([f for f in os.listdir(a.out) if f.startswith("step_")])
    print(f"listo: {hechos} nuevos, {fallos} fallidos, {n} de {int((T1 - T0) / timedelta(minutes=30))} pasos en disco", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
