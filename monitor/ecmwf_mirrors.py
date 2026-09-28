"""Descarga desde ECMWF Open Data con varios espejos y respaldo entre ellos.

Por qué existe (2026-09-28): el bucket de AWS
(ecmwf-forecasts.s3.eu-central-1) empezó a responder `503 Slow Down` a ~90% de
los pedidos, incluso espaciados y de a uno. fetch_ecmwf.py lo usaba como ÚNICA
fuente y, peor, trataba cualquier error del chequeo de disponibilidad como "ciclo
aún no publicado": retrocedía a ciclos de 25-56 h o se rendía con "ningún ciclo
HRES disponible". Desde el 26-sep ningún ciclo tuvo ECMWF utilizable. Medido ese
día con 6 pedidos por espejo: AWS 1/6, data.ecmwf.int 6/6, Google Cloud 6/6
(con byte-range 206), Azure 409 (descartado).

Los espejos publican los MISMOS archivos con la misma ruta relativa
(`YYYYMMDD/HHz/ifs/0p25/<stream>/...`). `ECMWF_MIRRORS` (coma) cambia el orden.
"""

import os
import time
import urllib.error
import urllib.request

MIRRORS = [m.strip().rstrip("/") for m in os.environ.get(
    "ECMWF_MIRRORS",
    "https://storage.googleapis.com/ecmwf-open-data,"
    "https://data.ecmwf.int/forecasts,"
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
).split(",") if m.strip()]


class NoPublicado(Exception):
    """Todos los espejos respondieron 404/403: el archivo no existe (aún)."""


def fetch(rel_path, headers=None, timeout=60, retries=3):
    """Bytes de `rel_path` probando cada espejo en orden.

    Un 404/403 pasa al siguiente espejo sin reintentar (ahí no está); un 5xx,
    timeout o corte se reintenta con backoff y luego pasa al siguiente. Si TODOS
    dicen 404/403 → NoPublicado; si alguno falló por otra causa → el último error
    (así "servidor saturado" nunca se confunde con "no publicado").
    """
    no_esta, ultimo = 0, None
    for base in MIRRORS:
        req = urllib.request.Request(f"{base}/{rel_path}", headers=headers or {})
        for intento in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                ultimo = exc
                if exc.code in (403, 404):
                    no_esta += 1
                    break
            except Exception as exc:  # timeout, conexión, etc.
                ultimo = exc
            if intento < retries - 1:
                time.sleep(min(2 ** intento, 8))
    if no_esta == len(MIRRORS):
        raise NoPublicado(rel_path)
    raise ultimo
