#!/usr/bin/env python3
"""Sensibilidad de la huella del runout al gatillo externo (swarm-abm 6f07499),
con los DOS modos de siembra que pidió swarm-abm (fixed y follow) y el área
excedida y el k objetivo por hora registrados por runout_ondemand.

El Arrayán (ventana de 14 km), susceptibilidad RandomForest del Maipo, semilla
1, chanaral_enhanced, a*=5.5. Serie horaria real del 29–31 ene 2021 de La
Platina (INIA-4, 55 mm, máx 6.6 mm/h) escalada ×1, ×2 y ×4 (E máx 1.66, 3.33,
6.66), y tres densidades de gotas (0.02 es el placeholder de swarm-abm,
derivado de dominios de miles de km²).
Requiere scripts/huellas_2021/series_2021.json (lo escribe este script desde
agromet si falta)."""
import json, os, sqlite3, subprocess

H = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(H, "..", ".."))
BIN = os.path.join(ROOT, "target", "release", "examples", "runout_ondemand")
SUSC = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif"
OUT = os.path.join(ROOT, "data", "huellas_2021", "runout")
SERIES = os.path.join(ROOT, "data", "huellas_2021", "series_2021.json")

if not os.path.exists(SERIES):
    c = sqlite3.connect("file:/home/franciscoparrao/proyectos/agromet-scraper/data/agromet.db?mode=ro", uri=True)
    s = {}
    for sid in ("EXT-108", "INIA-4"):
        d = dict(c.execute("select fecha, valor from mediciones where estacion_id=? and metrica='PP-SUM' "
                           "and stack='HOUR' and fecha>='2021-01-29' and fecha<'2021-02-01'", (sid,)))
        s[sid] = [d.get(f"2021-01-{29 + h // 24:02d} {h % 24:02d}:00") or 0.0 for h in range(72)]
    json.dump(s, open(SERIES, "w"))
serie = json.load(open(SERIES))["INIA-4"]

print(f"{'siembra':8s} {'gotas/km²':>9s} {'escala':>6s} {'E máx':>6s} {'h E≥1':>6s} {'km² comp.':>9s} {'k/h':>4s} {'Σk':>5s} {'celdas':>7s}")
for seeding, densidades in (("fixed", [None]), ("follow", [0.02, 0.1, 0.5])):
    for dens in densidades:
        for k in (1, 2, 4):
            tag = f"gatillo_{seeding}_{dens}_x{k}"
            out = os.path.join(OUT, tag)
            cmd = [BIN, "--lon=-70.42", "--lat=-33.35", "--size-km", "14",
                   "--rain-mm-per-hour", ",".join(f"{v * k:.3f}" for v in serie), "--trigger-id-a", "5.5",
                   "--trigger-seeding", seeding, "--isotherm-m", "4500", "--sediment", "0.5",
                   "--susceptibility", SUSC, "--params", "chanaral_enhanced", "--seed", "1", "--out", out]
            if dens is not None:
                cmd += ["--trigger-drops-per-km2", str(dens)]
            subprocess.run(cmd, check=True, capture_output=True)
            st = json.load(open(os.path.join(out, "stats.json")))
            t = st["forcing"]["trigger"]
            kh = t["k_target_by_hour"]
            # En `fixed` el k objetivo no se usa (siembra = gotas fijas del preset).
            kmax, ksum = (f"{max(kh):4d}", f"{sum(kh):5d}") if seeding == "follow" else ("   —", "    —")
            print(f"{seeding:8s} {str(dens or '—'):>9s} {'×' + str(k):>6s} {t['e_max']:6.2f} {t['hours_e_ge_1']:6d} "
                  f"{t['gated_area_km2']:9.1f} {kmax} {ksum} {st['runout']['affected_cells']:7d}")
