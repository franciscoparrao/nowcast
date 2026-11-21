"""Series de IMERG Final V07 semihorario en un punto vía la API de Giovanni
(api.giovanni.earthdata.nasa.gov/timeseries). Verificado el 2026-11-21: en el
núcleo de Atacama devuelve exactamente los valores de data/event_atacama_2015.csv.
"""
import time

import requests

URL = "https://api.giovanni.earthdata.nasa.gov/timeseries"
DATA = "GPM_3IMERGHH_07_precipitation"
_TOK = None


def token():
    global _TOK
    if _TOK is None:
        import earthaccess
        _TOK = earthaccess.login(strategy="netrc").token["access_token"]
    return _TOK


def serie(lat, lon, t0, t1, intentos=4):
    """[(\"YYYY-mm-dd HH:MM\", mm/h), ...] entre t0 y t1 (ISO, incluidos)."""
    for k in range(intentos):
        try:
            r = requests.get(URL, params={"data": DATA, "location": f"[{lat:.2f},{lon:.2f}]",
                                          "time": f"{t0}/{t1}"},
                             headers={"Authorization": "Bearer " + token()}, timeout=600)
            r.raise_for_status()
            out = []
            for l in r.text.splitlines():
                p = l.split(",")
                if len(p) == 2 and p[0][:2] in ("19", "20") and ":" in p[0]:
                    out.append((p[0], float(p[1]) if p[1] not in ("", "NaN") else float("nan")))
            return out
        except Exception:  # noqa: BLE001
            if k == intentos - 1:
                raise
            time.sleep(10 * (k + 1))
