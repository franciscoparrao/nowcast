#!/usr/bin/env python3
"""Primer cruce REAL de monitor@rm (racha desde las 16:30Z del 12-oct-2026): qué vio
cada feed en la celda que cruzó, sobre la evidencia congelada automáticamente por
freeze_evidence.py (copia de ~/nowcast-monitor-rm/evidence/20261012T1630 en
evidencia/cruce_rm_20261012/, con manifest.json y sha256 por archivo)."""
import json, os, re
import numpy as np, rasterio
E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia", "cruce_rm_20261012")
man = json.load(open(os.path.join(E, "manifest.json")))
nivel = 0.267
print(f"racha {man['racha']} | ventana congelada {man['ventana']['desde']}–{man['ventana']['hasta']} | "
      f"{man['n_ciclos']} ciclos, {len(man['files'])} archivos, commit {man['monitor_commit']}")
print("feeds:", {k: (v["pasos_congelados"], "último real " + str(v["newest_real"])) for k, v in man["feeds"].items()})
print("IMERG en relleno cero (cola de latencia) desde:", man["feeds"]["primary"]["zerofill_en_ventana"][0])

# bitácora: primer aviso y corroboraciones
log = open(os.path.join(E, "monitor_log_extracto.txt")).read().splitlines()
avisos = [l for l in log if "NOTIFY[urgent]" in l and "CRUCE NUEVO" in l]
print(f"\navisos 'CRUCE NUEVO' entregados: {len(avisos)} → " + ", ".join(l[1:21] for l in avisos))
m = re.search(r"\] NOTIFY\[urgent\] (.*?) — (\d+ paso\(s\).*?pico de la ventana [\d.]+)", avisos[0])
print("  primer aviso:", avisos[0][1:21], "|", m.group(2) if m else avisos[0][:160])
corr = [l for l in log if "corroboración:" in l]
print(f"corroboraciones registradas: {len(corr)}; primera {corr[0][1:21]} → {corr[0].split('— ')[1]}")
print("  todas sostenidas solo por DGA:", all("(dga)" in l for l in corr))

# celda que cruza y lluvia por feed en cada paso de la racha
ep0, ep1 = man["racha"]["ep_first"], man["racha"]["ep_last"]
keys = sorted(os.path.basename(p)[7:-4] for p in os.listdir(os.path.join(E, "hazard")))
racha = [k for k in keys if ep0 <= k <= ep1]
print(f"\n{'paso UTC':14s} {'peligro máx':>11s}  celda (lon,lat)        IMERG   GOES    DGA   (mm/30 min en esa celda)")
celdas = set(); acum = {"primary": 0.0, "goes": 0.0, "dga": 0.0}
for k in racha:
    with rasterio.open(os.path.join(E, "hazard", f"hazard_{k}.tif")) as d:
        h = d.read(1); r, c = np.unravel_index(np.nanargmax(h), h.shape)
        lon, lat = d.xy(r, c); n_al = int((h >= nivel).sum())
    celdas.add((round(lon, 2), round(lat, 2)))
    mm = []
    for feed in ("primary", "goes", "dga"):
        p = os.path.join(E, "feeds", feed, f"step_{k}.tif")
        if os.path.exists(p):
            with rasterio.open(p) as d:
                v = float(d.read(1)[r, c]); v = 0.0 if not np.isfinite(v) else v
        else:
            v = 0.0   # zerofill del primario: no hay archivo
        mm.append(v); acum[feed] += v
    print(f"{k:14s} {h.max():11.3f}  ({lon:.2f},{lat:.2f}) {n_al} celda(s)  " + " ".join(f"{v:6.2f}" for v in mm))
print(f"celdas en alerta en la racha: {sorted(celdas)} | acumulado en la celda, {len(racha)} pasos: "
      + ", ".join(f"{k} {v:.1f} mm" for k, v in acum.items()))

# EMAs del archivo (ventana congelada)
res = json.load(open(os.path.join(E, "ema", "resumen.json")))
horas = sorted({h for v in res.values() for h in v["horario_mm"]})
horas = [h for h in horas if "2026-10-12T14Z" <= h <= "2026-10-12T21Z"]
print(f"\nEMAs DMC (archivo archive_ema), mm por hora UTC, 14Z–21Z:")
print(f"{'estación':28s}" + "".join(f"{h[11:14]:>7s}" for h in horas) + "   total ventana")
for code, v in res.items():
    print(f"{code} {v['nombre'][:21]:21s}" + "".join(f"{v['horario_mm'].get(h, 0.0):7.1f}" for h in horas) + f"   {v['total_mm']:.1f} mm ({v['registros']} registros)")

# GLM
cnt = json.load(open(os.path.join(E, "glm", "counts.json")))
tot = sum(v.get("domain", 0) for v in cnt.values())
print(f"\nGLM: {tot} descargas en el dominio en {len(cnt)} pasos de la ventana; "
      f"máx por paso {max((v.get('domain', 0) for v in cnt.values()), default=0)}")
cic = [json.loads(l) for l in open(os.path.join(E, "ciclos.jsonl"))]
print(f"ciclos.jsonl: {len(cic)} ciclos, veredictos {[c['corroborada'] for c in cic]}, sostienen {sorted({c['sostienen'] for c in cic})}, "
      f"títulos {sorted({c['titulo'] for c in cic})}")

# ¿Habría cruzado con Caine global (a=14.82) en vez del intercepto regional a*=5.5?
# Excedencia E = I / (a·D^-0.39) sobre la lluvia horaria de Tobalaba, D = 1, 2, 3 h
tob = res["330019"]["horario_mm"]
serie = [tob.get(f"2026-10-12T{h:02d}Z", 0.0) for h in range(14, 22)]
print("\nTobalaba 14Z–21Z (mm/h):", serie)
for a in (14.82, 5.5):
    best = 0.0
    for D in (1, 2, 3):
        for i in range(len(serie) - D + 1):
            I = sum(serie[i:i + D]) / D
            best = max(best, I / (a * D ** -0.39))
    print(f"  a={a:5.2f}: excedencia máxima E={best:.2f} → {'CRUZA' if best >= 1 else 'no cruza'} (E≥1 = sobre la curva I-D)")
