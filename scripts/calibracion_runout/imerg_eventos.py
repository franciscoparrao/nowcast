#!/usr/bin/env python3
"""Forzante grillada IMERG Final (GPM_3IMERGHH v07, semihoraria, 0.1°) para los
eventos del paquete de calibración: recupera los 124 eventos sin estación y
desacopla El Arrayán 2021 de El Colorado (decisión 23-oct-2026).

Fase A (descarga): para cada día necesario —[ini−2, fin+1] de cada evento, para
que la ventana I-D de hasta 48 h tenga historia— baja los 48 gránulos globales
UNO A UNO por listado de GES DISC (sin CMR, que cayó el 20-oct), recorta la
región y BORRA el HDF5. Guarda un cubo por día en
data/calibracion_runout/imerg/dia_YYYYMMDD.npz (48 × lat × lon, mm por media
hora). Reanudable; se detiene si quedan < MIN_LIBRE_GB en disco.

Fase B (extracción): por evento, la celda IMERG que contiene el punto, con su
serie semihoraria en [ini−2, fin+1] (UTC); en la ventana [ini−1, fin+1]: total,
máximo diario (día UTC), máximo horario y la excedencia I-D máxima con el núcleo
del motor (peor ventana de hasta 48 h) para a*=5.5 y Caine. Salida
data/calibracion_runout/eventos_imerg.csv.

SESGO DECLARADO: IMERG pierde los chubascos convectivos de piedemonte (Las
Condes 8-oct-2026: ×34 bajo el pluviómetro) y sobreestima medias areales en
lluvia frontal (Huasco 18-jul-2026). Días en UTC; el catálogo usa día civil
local: la ventana ±1 día lo absorbe.

Uso: python3 imerg_eventos.py descargar | extraer | todo
"""
import csv, glob, json, os, re, shutil, sys
from datetime import date, datetime, timedelta

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
P = os.path.join(ROOT, "data", "calibracion_runout")
CUBOS = os.path.join(P, "imerg")
RAW = os.path.join(CUBOS, "raw")
BBOX = (-72.1, -35.1, -69.8, -32.0)
DIR = "https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGHH.07/{y}/{doy:03d}/"
PAT = re.compile(r"3B-HHR\.MS\.MRG\.3IMERG\.\d{8}-S\d{6}-E\d{6}\.\d{4}\.V[0-9A-Z]+\.HDF5")
MIN_LIBRE_GB = 6.0  # margen para que la operación (monitores, pronóstico) nunca quede sin disco
CURVAS = {"a5.5": 5.5, "caine": 14.82}
B = 0.39


def eventos():
    return [f["properties"] for f in json.load(open(os.path.join(P, "eventos.geojson")))["features"]]


def dias_necesarios(evs):
    d = set()
    for e in evs:
        x = date.fromisoformat(e["fecha_ini"]) - timedelta(days=2)
        while x <= date.fromisoformat(e["fecha_fin"]) + timedelta(days=1):
            d.add(x); x += timedelta(days=1)
    return sorted(d)


def libre_gb():
    return shutil.disk_usage(P).free / 1e9


def descargar():
    import earthaccess
    import xarray as xr
    os.makedirs(RAW, exist_ok=True)
    earthaccess.login(strategy="netrc")
    ses = earthaccess.get_requests_https_session()
    dias = dias_necesarios(eventos())
    hechos = sum(os.path.exists(os.path.join(CUBOS, f"dia_{d:%Y%m%d}.npz")) for d in dias)
    print(f"[imerg] {len(dias)} días necesarios, {hechos} ya hechos", flush=True)
    for d in dias:
        out = os.path.join(CUBOS, f"dia_{d:%Y%m%d}.npz")
        if os.path.exists(out):
            continue
        if libre_gb() < MIN_LIBRE_GB:
            sys.exit(f"[imerg] ALTO: quedan {libre_gb():.1f} GB libres (< {MIN_LIBRE_GB}); reanudar tras liberar disco")
        url = DIR.format(y=d.year, doy=d.timetuple().tm_yday)
        r = ses.get(url, timeout=120); r.raise_for_status()
        nombres = {}
        for n in set(PAT.findall(r.text)):  # una versión por paso (la más alta)
            k = n.split(".3IMERG.")[1][:16]
            nombres[k] = max(nombres.get(k, ""), n)
        nombres = [nombres[k] for k in sorted(nombres)]
        if len(nombres) != 48:
            print(f"[imerg] {d}: {len(nombres)} gránulos (se esperaban 48)", flush=True)
        cubo, tiempos, lons, lats = [], [], None, None
        for n in nombres:
            tmp = os.path.join(RAW, n)
            with ses.get(url + n, stream=True, timeout=300) as g:
                g.raise_for_status()
                with open(tmp + ".part", "wb") as fh:
                    for ch in g.iter_content(1 << 20):
                        fh.write(ch)
            os.replace(tmp + ".part", tmp)
            ds = xr.open_dataset(tmp, group="Grid")
            var = "precipitation" if "precipitation" in ds else "precipitationCal"
            da = ds[var].sel(lon=slice(BBOX[0], BBOX[2]), lat=slice(BBOX[1], BBOX[3])).squeeze("time")
            a = np.clip(np.nan_to_num(np.asarray(da.values, "float32"), nan=0.0), 0.0, None) * 0.5  # mm/h → mm/30 min
            if lons is None:
                lons, lats = da["lon"].values, da["lat"].values
            ds.close()
            os.remove(tmp)
            cubo.append(a)  # dims (lon, lat), layout lon-major de IMERG
            tok = n.split(".3IMERG.")[1]
            tiempos.append(tok[:8] + tok[10:14])
        np.savez_compressed(out + ".tmp.npz", mm=np.stack(cubo), lons=lons, lats=lats, tiempos=np.array(tiempos))
        os.replace(out + ".tmp.npz", out)
        print(f"[imerg] {d}: {len(cubo)} pasos → {os.path.basename(out)} · {libre_gb():.1f} GB libres", flush=True)


def extraer():
    filas = []
    for e in eventos():
        d0 = date.fromisoformat(e["fecha_ini"]) - timedelta(days=2)
        d1 = date.fromisoformat(e["fecha_fin"]) + timedelta(days=1)
        serie, tiempos, falta = [], [], False
        x = d0
        while x <= d1:
            f = os.path.join(CUBOS, f"dia_{x:%Y%m%d}.npz")
            if not os.path.exists(f):
                falta = True; break
            z = np.load(f)
            i = int(np.argmin(np.abs(z["lons"] - e["lon"]))); j = int(np.argmin(np.abs(z["lats"] - e["lat"])))
            serie += list(z["mm"][:, i, j]); tiempos += list(z["tiempos"])
            x += timedelta(days=1)
        if falta:
            filas.append({"cod_rm": e["cod_rm"], "estado": "falta día IMERG"}); continue
        s = np.array(serie, float)
        ini_v = 48  # la ventana de evaluación arranca en ini−1 (el día 0 es ini−2)
        v = s[ini_v:]
        dias = v.reshape(-1, 48).sum(1)
        hor = v.reshape(-1, 2).sum(1)
        fila = {"cod_rm": e["cod_rm"], "region": e["region"], "fecha_ini": e["fecha_ini"], "fecha_fin": e["fecha_fin"],
                "lon": e["lon"], "lat": e["lat"], "celda_lon": round(float(z["lons"][i]), 2),
                "celda_lat": round(float(z["lats"][j]), 2), "total_mm": round(float(v.sum()), 1),
                "max_diario_mm": round(float(dias.max()), 1), "max_horario_mm": round(float(hor.max()), 2),
                "estado": "ok"}
        for k, a in CURVAS.items():
            # la historia de ini−2 entra a las ventanas, pero solo se evalúan los pasos de la ventana
            fila[f"e_max_{k}"] = round(worst_e_ventana(s, a, ini_v), 3)
        filas.append(fila)
    campos = ["cod_rm", "region", "fecha_ini", "fecha_fin", "lon", "lat", "celda_lon", "celda_lat", "total_mm",
              "max_diario_mm", "max_horario_mm", "e_max_a5.5", "e_max_caine", "estado"]
    with open(os.path.join(P, "eventos_imerg.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos); w.writeheader()
        for f in filas:
            w.writerow({c: f.get(c, "") for c in campos})
    ok = [f for f in filas if f["estado"] == "ok"]
    print(f"[imerg] {len(ok)} de {len(filas)} eventos con serie IMERG completa")
    for k in CURVAS:
        print(f"  E máx ≥ 1 con {k}: {sum(f[f'e_max_{k}'] >= 1 for f in ok)} de {len(ok)}")


def worst_e_ventana(s, a, desde, dt=0.5, max_h=48):
    """Peor E sobre los pasos t ≥ desde, con ventanas que pueden mirar hacia atrás
    hasta 48 h (incluida la historia previa a `desde`)."""
    pre = np.concatenate([[0.0], np.cumsum(np.maximum(s, 0))])
    best, mmax = 0.0, int(max_h / dt)
    for t in range(desde, len(s)):
        for m in range(1, min(mmax, t + 1) + 1):
            dur = m * dt
            best = max(best, ((pre[t + 1] - pre[t + 1 - m]) / dur) * dur ** B / a)
    return best


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "todo"
    if modo in ("descargar", "todo"):
        descargar()
    if modo in ("extraer", "todo"):
        extraer()
