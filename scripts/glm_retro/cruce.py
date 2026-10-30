#!/usr/bin/env python3
"""Cruce GLM × catálogo de eventos según PROTOCOLO.md (no cambiar reglas aquí
sin anotarlo allá).

Uso: python cruce.py [--n 25] [--glm DIR] [--catalogo CSV] [--out DIR]
Corre con ~/nowcast-monitor/venv-forecast (scipy + geopandas).
"""
import argparse
import csv
import glob
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy import ndimage

LON0, LAT0, RES, NX, NY = -74.0, -40.0, 0.1, 60, 150   # mismo recorte que descargar.py
BIN = timedelta(minutes=10)
CHILE = timedelta(hours=-3)                              # hora de Chile desde el 6-sep-2026
GPKG = os.path.expanduser("~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")
RADIO_KM, PRE, POST = 15.0, timedelta(hours=1), timedelta(hours=3)


def mascaras():
    """Celdas 0.1° que tocan tierra chilena y que tocan un límite urbano."""
    import geopandas as gpd
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    tr = from_origin(LON0, LAT0 + NY * RES, RES, RES)
    out = {}
    for capa in ("Comunal_CPV24", "Limite_Urbano_CPV24"):
        g = gpd.read_file(GPKG, layer=capa, bbox=(LON0, LAT0, LON0 + NX * RES, LAT0 + NY * RES))
        g = g.to_crs(4326)
        m = rasterize(((geom, 1) for geom in g.geometry if geom is not None), out_shape=(NY, NX),
                      transform=tr, all_touched=True, dtype="uint8")
        out[capa] = m[::-1].astype(bool)          # fila 0 = sur, como iy en descargar.py
    return out["Comunal_CPV24"], out["Limite_Urbano_CPV24"]


def cargar_glm(d):
    dias = sorted(glob.glob(os.path.join(d, "glm_*.csv.gz")))
    t0 = datetime.strptime(os.path.basename(dias[0])[4:12], "%Y%m%d").replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(os.path.basename(dias[-1])[4:12], "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    nt = int((t1 - t0) / BIN)
    A = np.zeros((nt, NY, NX), "int32")
    presentes = np.zeros(nt, bool)
    for f in dias:
        dd = datetime.strptime(os.path.basename(f)[4:12], "%Y%m%d").replace(tzinfo=timezone.utc)
        i0 = int((dd - t0) / BIN)
        presentes[i0:i0 + 144] = True
        for r in csv.DictReader(gzip.open(f, "rt")):
            t = datetime.strptime(r["t10"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            ix = int(round((float(r["lon"]) - LON0) / RES - .5))
            iy = int(round((float(r["lat"]) - LAT0) / RES - .5))
            A[int((t - t0) / BIN), iy, ix] += int(r["n"])
    return t0, A, presentes


def episodios(A, tierra, n_umbral):
    S60 = ndimage.uniform_filter1d(A.astype("float32"), 6, axis=0, origin=2) * 6   # 60 min hacia atrás
    S = ndimage.uniform_filter(S60, size=(1, 3, 3), mode="constant") * 9            # vecindario 3×3
    aviso = (np.rint(S) >= n_umbral) & tierra[None]
    # huecos ≤30 min unen: se dilata 3 bloques hacia adelante solo para etiquetar
    puente = ndimage.binary_dilation(aviso, structure=np.ones((4, 1, 1), bool), origin=(-2, 0, 0))  # t → t..t+3 (verificado)
    lab, n = ndimage.label(puente, structure=np.ones((3, 3, 3), bool))
    lab = np.where(aviso, lab, 0)
    return lab, n


def hav_km(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(a))


def leer_catalogo(p):
    ev = []
    for r in csv.DictReader(open(p, encoding="utf-8")):
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except ValueError:
            continue
        hora = (r.get("hora_local") or "").strip()[:5]
        t = None
        if hora and r.get("hora_confianza", "").strip().lower() in ("exacta", "aprox"):
            t = datetime.strptime(f"{r['fecha']} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) - CHILE
        ev.append(dict(r, lat=lat, lon=lon, t_utc=t))
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--glm", default=os.path.expanduser("~/nowcast-forecast/glm_retro"))
    ap.add_argument("--catalogo", default=os.path.join(os.path.dirname(__file__), "..", "..", "data",
                                                       "catalogo_eventos_2026-10.csv"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    tierra, urbano = mascaras()
    t0, A, presentes = cargar_glm(a.glm)
    lab, n = episodios(A, tierra, a.n)
    yy, xx = np.mgrid[0:NY, 0:NX]
    clat, clon = LAT0 + (yy + .5) * RES, LON0 + (xx + .5) * RES
    objs = ndimage.find_objects(lab)
    eps = []
    for k, sl in enumerate(objs, 1):
        if sl is None:
            continue
        m = lab[sl] == k
        ti, yi, xi = np.nonzero(m)
        ti, yi, xi = ti + sl[0].start, yi + sl[1].start, xi + sl[2].start
        eps.append(dict(id=k, t_ini=t0 + ti.min() * BIN, t_fin=t0 + (ti.max() + 1) * BIN,
                        celdas=sorted(set(zip(yi.tolist(), xi.tolist()))),
                        urbano=bool(urbano[yi, xi].any()), descargas=int(A[ti, yi, xi].sum()),
                        primera={(y, x): t0 + t * BIN for t, y, x in sorted(zip(ti, yi, xi), reverse=True)}))
    ev = leer_catalogo(a.catalogo)
    for e in ev:
        e["episodios"], e["adelanto_min"] = [], None
        d_ev = datetime.strptime(e["fecha"], "%Y-%m-%d").date()
        for ep in eps:
            cerca = [(y, x) for y, x in ep["celdas"] if hav_km(e["lat"], e["lon"], clat[y, x], clon[y, x]) <= RADIO_KM]
            if not cerca:
                continue
            if e["t_utc"] is not None:
                ok = ep["t_ini"] - PRE <= e["t_utc"] <= ep["t_fin"] + POST
            else:
                ok = (ep["t_ini"] + CHILE).date() <= d_ev <= (ep["t_fin"] + CHILE).date()
            if ok:
                e["episodios"].append(ep["id"])
                ep.setdefault("eventos", []).append(e["localidad"] or e["comuna"])
                if e["t_utc"] is not None:
                    t_av = min(ep["primera"][c] for c in cerca)
                    lead = (e["t_utc"] - t_av).total_seconds() / 60
                    e["adelanto_min"] = lead if e["adelanto_min"] is None else max(e["adelanto_min"], lead)
    con = [ep for ep in eps if ep.get("eventos")]
    res = dict(n_umbral=a.n, bloques_sin_dato=int((~presentes).sum()), episodios=len(eps),
               episodios_con_evento=len(con), episodios_urbanos=sum(ep["urbano"] for ep in eps),
               episodios_urbanos_con_evento=sum(ep["urbano"] for ep in con), eventos=len(ev),
               eventos_con_episodio=sum(bool(e["episodios"]) for e in ev),
               por_mecanismo={}, adelantos=[])
    for e in ev:
        mec = "convectivo" if any(w in (e.get("mecanismo", "") + e.get("tipo", "")).lower()
                                  for w in ("convect", "tormenta", "granizo", "eléctric")) else "frontal/sin dato"
        d = res["por_mecanismo"].setdefault(mec, [0, 0])
        d[0] += 1
        d[1] += bool(e["episodios"])
        if e["adelanto_min"] is not None:
            res["adelantos"].append((e["fecha"], e["localidad"] or e["comuna"], round(e["adelanto_min"])))
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, f"episodios_n{a.n}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "t_ini_utc", "t_fin_utc", "n_celdas", "descargas", "urbano", "lat_c", "lon_c", "eventos"])
            for ep in eps:
                ys, xs = zip(*ep["celdas"])
                w.writerow([ep["id"], f"{ep['t_ini']:%Y-%m-%dT%H:%MZ}", f"{ep['t_fin']:%Y-%m-%dT%H:%MZ}",
                            len(ep["celdas"]), ep["descargas"], int(ep["urbano"]),
                            f"{np.mean(clat[ys, xs]):.2f}", f"{np.mean(clon[ys, xs]):.2f}",
                            "; ".join(ep.get("eventos", []))])
        with open(os.path.join(a.out, f"eventos_n{a.n}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["fecha", "localidad", "comuna", "tipo", "mecanismo", "episodios", "adelanto_min"])
            for e in ev:
                w.writerow([e["fecha"], e["localidad"], e["comuna"], e["tipo"], e.get("mecanismo", ""),
                            " ".join(map(str, e["episodios"])), "" if e["adelanto_min"] is None else round(e["adelanto_min"])])
        json.dump(res, open(os.path.join(a.out, f"resumen_n{a.n}.json"), "w"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
