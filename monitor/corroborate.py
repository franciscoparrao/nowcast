#!/usr/bin/env python3
"""Regla de corroboración de una alerta I-D entre feeds (2026-10-12).

POR QUÉ. La fusión es noisy-OR: basta que UN feed traiga lluvia para que el
motor cruce. Medido en monitor@rm: el 10-oct GOES QPE solo (3.8 mm/30 min en
una celda, estaciones en ~0, IMERG 1 mm) produjo una alerta que ningún otro
dato respalda; y el 12-oct el primer cruce real lo sostuvo SOLO el feed de
estaciones (Tobalaba 6.4 mm/h con IMERG y GOES en 0). Las dos situaciones
"solo un feed" no valen lo mismo: el pluviómetro es la única fuente que ve el
piedemonte y el QPE geoestacionario sobreestima chubascos y subestima lluvia
orográfica. La regla distingue quién sostiene el cruce y decide si el aviso
sale como ALERTA (urgent) o como "NO corroborada" (default).

QUÉ MIRA. Sobre la racha de alerta más reciente (pasos consecutivos en alerta
hasta el último) toma las celdas que cruzan el nivel en los rásters de peligro
de esos pasos (`out/hazard/hazard_%04d.tif`, WRITE_HAZARD_TIFS=1); sin rásters
cae a TODO el dominio (criterio más laxo: entonces la celda máxima de cada feed
cuenta). Suma la lluvia de cada feed en esas celdas en las últimas
CORROB_HOURS horas hasta el último paso en alerta.

DECISIÓN. Un feed "sostiene" si su acumulado ≥ CORROB_MIN_MM y, salvo DGA
(in situ: vale por sí solo), ≥ CORROB_FRACTION × el acumulado mayor entre los
feeds: la llovizna de fondo de IMERG (1.2 mm en 3 h) no respalda una ráfaga
de 36 mm que solo trae GOES (lo midió el autotest de integración).
  corroborada  si sostiene DGA (in situ: vale por sí solo), o IMERG (primario
               calibrado del umbral), o GOES junto con rayos GLM recientes en
               el dominio (≥ CORROB_GLM_FLASHES: convección vista por otro
               sensor), o GOES junto con IMERG.
  NO corroborada si solo sostiene GOES (o ningún feed llega al mínimo, lo que
               no debería pasar si el motor cruzó).
Un feed deshabilitado o ausente de fusion.json no cuenta ni a favor ni en
contra: con solo IMERG, IMERG corrobora.

Salida (stdout, una línea): `1|0 <feeds_que_sostienen> <detalle legible>`.
Uso: corroborate.py RUN_JSON FUSION_JSON HAZARD_DIR_o_- GLM_STATUS_o_- MIN_MM HOURS GLM_FLASHES ALERT_LEVEL [FRACTION=0.5]
`--selftest` sin red ni motor.
"""

import glob
import json
import os
import sys

import numpy as np


def episodio(run):
    idx = [s["step"] for s in run["steps"] if s.get("alert")]
    if not idx:
        return []
    last = idx[-1]
    first = last
    for i in reversed(idx[:-1]):
        if i == first - 1:
            first = i
        else:
            break
    return list(range(first, last + 1))


def celdas_en_alerta(hazard_dir, pasos, nivel):
    """Máscara booleana (filas, cols) de celdas ≥ nivel en algún paso de la racha;
    None si no hay rásters (→ todo el dominio)."""
    import rasterio
    mask = None
    for i in pasos:
        p = os.path.join(hazard_dir, f"hazard_{i:04d}.tif")
        if not os.path.exists(p):
            continue
        with rasterio.open(p) as d:
            a = d.read(1)
        q = np.isfinite(a) & (a >= nivel)
        mask = q if mask is None else (mask | q)
    return mask


def acumulado(lista, pasos, mask):
    """Suma por celda de los rásters `lista[i]` para i en pasos → máximo sobre
    la máscara (o sobre el dominio si mask es None)."""
    import rasterio
    tot = None
    for i in pasos:
        if i < 0 or i >= len(lista) or not lista[i]:
            continue
        try:
            with rasterio.open(lista[i]) as d:
                a = d.read(1)
        except Exception:
            continue
        a = np.where(np.isfinite(a), a, 0.0)
        tot = a if tot is None else tot + a
    if tot is None:
        return 0.0
    if mask is not None and mask.shape == tot.shape and mask.any():
        return float(tot[mask].max())
    return float(tot.max())


def decidir(sostienen, glm_flashes, glm_min):
    """sostienen: set de feeds con acumulado ≥ mínimo (nombres: imerg, goes, dga)."""
    if "dga" in sostienen or "imerg" in sostienen:
        return True
    if "goes" in sostienen and glm_flashes >= glm_min:
        return True
    return False


def evaluar(run, fusion, hazard_dir, glm_status, min_mm, hours, glm_min, nivel, fraction=0.5):
    pasos = episodio(run)
    if not pasos:
        return 1, set(), "sin pasos en alerta"
    last = pasos[-1]
    n_back = max(1, int(round(hours * 2)))
    ventana = list(range(last - n_back + 1, last + 1))
    mask = celdas_en_alerta(hazard_dir, pasos, nivel) if hazard_dir and os.path.isdir(hazard_dir) else None
    feeds = fusion.get("feeds", {})
    nombres = {"primary": "imerg", "goes": "goes", "dga": "dga"}
    mm = {}
    for k, nombre in nombres.items():
        if k not in feeds or not feeds[k].get("list"):
            continue
        mm[nombre] = acumulado(feeds[k]["list"].split(","), ventana, mask)
    top = max(mm.values()) if mm else 0.0
    sostienen = {n for n, v in mm.items()
                 if v >= min_mm and (n == "dga" or v >= fraction * top)}
    flashes = 0
    if glm_status and os.path.exists(glm_status):
        try:
            flashes = int(json.load(open(glm_status)).get("recent", {}).get("domain", 0))
        except Exception:
            flashes = 0
    ok = decidir(sostienen, flashes, glm_min)
    donde = "celdas en alerta" if mask is not None else "todo el dominio (sin rásters de peligro)"
    det = (f"lluvia {hours:g} h en {donde}: "
           + ", ".join(f"{n} {v:.1f} mm" for n, v in mm.items())
           + f"; rayos GLM {flashes}; sostienen: {', '.join(sorted(sostienen)) or 'ninguno'}")
    return (1 if ok else 0), sostienen, det


def selftest():
    import tempfile
    import rasterio
    from rasterio.transform import from_origin
    fallos = []
    with tempfile.TemporaryDirectory() as td:
        tr = from_origin(-70.85, -33.05, 0.1, 0.1)
        prof = dict(driver="GTiff", height=6, width=6, count=1, dtype="float32", crs="EPSG:4326", transform=tr)

        seq = [0]

        def ras(name, val_cell=None, mm=0.0, bg=0.0):
            # nombre único por llamada: los casos se construyen ANTES del bucle
            # y dos rásters con el mismo nombre se pisarían (pasó en la 1ª corrida)
            seq[0] += 1
            p = os.path.join(td, f"{seq[0]:03d}_{name}")
            a = np.full((6, 6), bg, "float32")
            if val_cell:
                a[val_cell] = mm
            with rasterio.open(p, "w", **prof) as d:
                d.write(a, 1)
            return p

        zero = ras("zero.tif")
        n = 8
        run = {"steps": [{"step": i, "max_probability": 0.1, "alert": None} for i in range(n)]}
        for i in (6, 7):
            run["steps"][i]["alert"] = {"n_cells": 1}
        hz = os.path.join(td, "hazard"); os.makedirs(hz)
        for i in range(n):
            src = ras(f"hz_{i}.tif", (2, 3) if i >= 6 else None, 0.8, 0.05)
            os.replace(src, os.path.join(hz, f"hazard_{i:04d}.tif"))

        def fusion(imerg=0.0, goes=0.0, dga=0.0, cell=(2, 3), con_dga=True):
            f = {"feeds": {}}
            for name, v in (("primary", imerg), ("goes", goes), ("dga", dga)):
                if name == "dga" and not con_dga:
                    continue
                lst = [zero] * n
                for i in (6, 7):
                    lst[i] = ras(f"{name}_{i}.tif", cell, v) if v else zero
                f["feeds"][name] = {"list": ",".join(lst)}
            return f

        def glm(flashes):
            seq[0] += 1
            p = os.path.join(td, f"glm_{seq[0]:03d}.json")
            json.dump({"recent": {"domain": flashes}}, open(p, "w"))
            return p

        casos = [
            ("solo GOES, sin rayos → NO", fusion(goes=3.0), glm(0), 0),
            ("solo GOES + rayos → sí", fusion(goes=3.0), glm(5), 1),
            ("solo DGA → sí", fusion(dga=3.0), glm(0), 1),
            ("solo IMERG → sí", fusion(imerg=3.0), glm(0), 1),
            ("GOES + IMERG → sí", fusion(goes=3.0, imerg=1.5), glm(0), 1),
            ("GOES en OTRA celda que la alerta → NO (no sostiene)", fusion(goes=3.0, cell=(0, 0)), glm(0), 0),
            ("GOES bajo el mínimo + DGA sobre → sí", fusion(goes=0.4, dga=1.2), glm(0), 1),
            ("GOES 36 mm + llovizna IMERG 1.2 mm → NO (fracción)", fusion(goes=18.0, imerg=0.6), glm(0), 0),
            ("GOES 36 mm + DGA 1.2 mm → sí (in situ no lleva fracción)", fusion(goes=18.0, dga=0.6), glm(0), 1),
            ("sin feed DGA, solo GOES → NO", fusion(goes=3.0, con_dga=False), glm(0), 0),
        ]
        for nombre, fz, g, esp in casos:
            ok, sost, det = evaluar(run, fz, hz, g, 1.0, 3.0, 3, 0.5)
            res = ok == esp
            print(f"  {'ok  ' if res else 'FALLA'} {nombre:52s} → {ok} ({det})")
            if not res:
                fallos.append(nombre)
        # sin rásters de peligro: GOES en otra celda SÍ sostiene (criterio laxo, todo el dominio)
        ok, _, det = evaluar(run, fusion(goes=3.0, cell=(0, 0)), None, glm(5), 1.0, 3.0, 3, 0.5)
        res = ok == 1
        print(f"  {'ok  ' if res else 'FALLA'} {'sin rásters: GOES en otra celda + rayos → sí':52s} → {ok} ({det})")
        if not res:
            fallos.append("sin rasters")
        # racha: solo cuenta la última racha (paso 2 en alerta aislado no la alarga)
        run2 = json.loads(json.dumps(run)); run2["steps"][2]["alert"] = {"n_cells": 1}
        res = episodio(run2) == [6, 7]
        print(f"  {'ok  ' if res else 'FALLA'} {'racha más reciente = [6, 7]':52s} → {episodio(run2)}")
        if not res:
            fallos.append("racha")
    print(f"\n[selftest corroborate] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    run_p, fusion_p, hazard_dir, glm_status, min_mm, hours, glm_min, nivel = sys.argv[1:9]
    fraction = float(sys.argv[9]) if len(sys.argv) > 9 else 0.5
    run = json.load(open(run_p))
    fusion = json.load(open(fusion_p))
    ok, sost, det = evaluar(run, fusion,
                            None if hazard_dir == "-" else hazard_dir,
                            None if glm_status == "-" else glm_status,
                            float(min_mm), float(hours), int(glm_min), float(nivel), fraction)
    print(ok, ",".join(sorted(sost)) or "ninguno", det)
    return 0


if __name__ == "__main__":
    sys.exit(main())
