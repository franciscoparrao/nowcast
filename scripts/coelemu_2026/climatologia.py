#!/usr/bin/env python3
"""¿Qué tan raro fue el evento de Coelemu (31-jul-2026) según la ventana con que
se mida? Recalcula las cifras de la Discusión del paper (§6, «the
climatological reference») y GUARDA la salida, que faltaba (2026-11-10).

Evento: IMERG Early semihorario (data/coelemu_2026/imerg_early), media sobre
las celdas 0.1° cuyo centro cae en la comuna. Máxima acumulación de 24 h y de
5 días, y la fracción del total de 5 días que cayó en sus 2 días más lluviosos.

Climatología: CR2MET v2.5 diario 0.05°, media comunal (centros en la comuna),
inviernos mayo-septiembre 1991-2020. Totales diarios para la ventana de 24 h y
sumas móviles de 5 días dentro de cada invierno para la de 5 días.

Percentil = % de valores climatológicos estrictamente menores que el evento;
"igualado" = n.º de valores ≥ evento.

Uso: python climatologia.py   (python del sistema o venv-forecast)
"""
import glob
import json
import os

import geopandas as gpd
import netCDF4
import numpy as np
import rasterio
from shapely.geometry import Point

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "..", "data", "coelemu_2026")
GPKG = os.path.expanduser("~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")
CR2 = "/mnt/kingston/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"
ANIOS, MESES = range(1991, 2021), (5, 6, 7, 8, 9)


def comuna():
    g = gpd.read_file(GPKG, layer="Comunal_CPV24", where="COMUNA = 'COELEMU'").to_crs(4326)
    assert len(g) == 1, len(g)
    return g.geometry.iloc[0]


def evento(poly, carpeta=None, esperados=288):
    fs = sorted(glob.glob(os.path.join(carpeta or os.path.join(DATA, "imerg_early"), "step_*.tif")))
    serie, claves = [], []
    for f in fs:
        with rasterio.open(f) as s:
            a = s.read(1)
            if not serie:
                rows, cols = np.mgrid[0:a.shape[0], 0:a.shape[1]]
                x, y = s.transform * (cols + .5, rows + .5)
                dentro = np.array([[poly.contains(Point(xx, yy)) for xx, yy in zip(rx, ry)] for rx, ry in zip(x, y)])
                n_celdas = int(dentro.sum())
            serie.append(float(a[dentro].mean()))
        claves.append(os.path.basename(f)[5:-4])
    serie = np.array(serie)
    if esperados and len(serie) != esperados:
        raise SystemExit(f"faltan pasos IMERG: {len(serie)} de {esperados}; un hueco subestima el evento")
    def max_ventana(n):
        c = np.concatenate([[0], np.cumsum(serie)])
        s = c[n:] - c[:-n]
        i = int(np.argmax(s))
        return float(s[i]), i
    v24, i24 = max_ventana(48)
    v5d, i5 = max_ventana(min(240, len(serie)))
    tramo = serie[i5:i5 + 240]
    c = np.concatenate([[0], np.cumsum(tramo)])
    n2 = min(96, len(tramo))   # la serie del monitor en vivo tiene menos de dos días
    dos = float((c[n2:] - c[:-n2]).max())
    # los 5 días (240 pasos) que TERMINAN con el máximo de 24 h
    fin = i24 + 48
    t5 = serie[max(0, fin - 240):fin]
    c5 = np.concatenate([[0], np.cumsum(t5)])
    hasta24 = dict(total_mm=round(float(t5.sum()), 2), pasos=len(t5),
                   frac_2dias=round(float((c5[96:] - c5[:-96]).max() / t5.sum()), 3) if len(t5) > 96 else 1.0)
    return dict(pasos=len(serie), esperados=esperados, celdas_imerg=n_celdas,
                cinco_dias_hasta_max_24h=hasta24,
                max_24h_mm=round(v24, 2), fin_24h=claves[i24 + 47],
                max_5d_mm=round(v5d, 2), inicio_5d=claves[i5], fin_5d=claves[min(i5 + 239, len(claves) - 1)],
                frac_2dias_en_5d=round(dos / v5d, 3) if v5d else None)


def climatologia(poly):
    diarios, sumas5 = [], []
    mask = None
    for y in ANIOS:
        invierno = []
        for m in MESES:
            d = netCDF4.Dataset(os.path.join(CR2, f"CR2MET_pr_v2.5_day_{y}_{m:02d}_005deg.nc"))
            if mask is None:
                lat, lon = d["lat"][:], d["lon"][:]
                ilat = np.where((lat > poly.bounds[1] - .1) & (lat < poly.bounds[3] + .1))[0]
                ilon = np.where((lon > poly.bounds[0] - .1) & (lon < poly.bounds[2] + .1))[0]
                mask = np.array([[poly.contains(Point(float(lon[j]), float(lat[i]))) for j in ilon] for i in ilat])
            pr = np.asarray(d["pr"][:, ilat[0]:ilat[-1] + 1, ilon[0]:ilon[-1] + 1], "float64")
            d.close()
            invierno += list(pr[:, mask].mean(axis=1))
        invierno = np.array(invierno)
        diarios += list(invierno)
        c = np.concatenate([[0], np.cumsum(invierno)])
        sumas5 += list(c[5:] - c[:-5])
    return np.array(diarios), np.array(sumas5), int(mask.sum())


def rareza(valor, clim):
    return dict(percentil=round(100 * float((clim < valor).mean()), 2), igualado=int((clim >= valor).sum()),
                n=len(clim))


def main():
    poly = comuna()
    ev = evento(poly)
    dia, s5, n_cr2 = climatologia(poly)
    out = dict(evento=ev, celdas_cr2met=n_cr2, inviernos=f"{ANIOS[0]}-{ANIOS[-1]} MJJAS",
               ventana_24h=rareza(ev["max_24h_mm"], dia), ventana_5d_max=rareza(ev["max_5d_mm"], s5),
               ventana_5d_hasta_max_24h=rareza(ev["cinco_dias_hasta_max_24h"]["total_mm"], s5))
    # Variante "monitor en vivo": los pasos que tenía monitor@cs el 31-jul (≈46 h
    # con 14 huecos rellenos con cero). Reproduce las cifras de 24 h del paper
    # anterior; sirve para documentar de dónde salieron, no como evento.
    mon = os.path.expanduser("~/nowcast-monitor-cs/steps")
    if os.path.isdir(mon):
        ev_m = evento(poly, mon, esperados=None)
        out["monitor_en_vivo"] = dict(evento=ev_m, ventana_24h=rareza(ev_m["max_24h_mm"], dia),
                                      ventana_total_como_5d=rareza(ev_m["max_5d_mm"], s5))
    for k in ("ventana_24h", "ventana_5d_max", "ventana_5d_hasta_max_24h"):
        out[k]["veces_por_invierno"] = round(out[k]["igualado"] / len(ANIOS), 2)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    json.dump(out, open(os.path.join(DATA, "climatologia.json"), "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
