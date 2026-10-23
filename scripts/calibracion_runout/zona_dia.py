#!/usr/bin/env python3
"""Tabla zona-día para calibrar el umbral de inicio con la MISMA unidad y el
MISMO agregado en eventos y no-eventos (objeción de nowcast al protocolo de
swarm-abm, 22-oct-2026).

Zonas: celdas de la grilla de 0.1° del monitor (centros en x.x5) sobre la zona
del paquete, que tengan al menos una estación con precipitación a ≤ RADIO_KM de
su centro. Las zonas NO se eligen por tener eventos (eso condicionaría la
muestra a la clase positiva) y se solapan en estaciones: no son independientes.

Por zona y día con lluvia (máx ≥ PP_MIN_MM), con los mismos estadísticos en las
dos clases:
  pp_max_mm      máximo diario entre las estaciones de la zona (agregado declarado)
  pp_media_mm    media diaria entre las estaciones con dato
  pp_cercana_mm  la estación más cercana al centro con dato ese día
  pp3_max_mm     máximo entre estaciones de la suma de 3 días (d−2..d)
  pph_max_mm     máximo horario del día entre las estaciones con dato horario
                 (vacío si ninguna lo tiene; Guayacán solo tiene diario)
  n_est, n_est_h estaciones con dato diario / horario ese día
Etiqueta: `evento` si algún evento de eventos.geojson cae DENTRO de la celda y
el día está en [ini−1, fin+1]; si no, `no_evento` (= sin flujo reportado).

Salida: data/calibracion_runout/zona_dia.csv y zona_dia_resumen.json.
"""
import csv, json, math, os, sqlite3
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
P = os.path.join(ROOT, "data", "calibracion_runout")
DB = "/home/franciscoparrao/proyectos/agromet-scraper/data/agromet.db"
BBOX = (-72.1, -35.1, -69.8, -32.0)
RADIO_KM, PP_MIN_MM = 15.0, 1.0


def km(lon1, lat1, lon2, lat2):
    return math.hypot((lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * 111.32)


def main():
    est = {r["estacion_id"]: (float(r["lon"]), float(r["lat"]))
           for r in csv.DictReader(open(os.path.join(P, "estaciones.csv")))}
    ev = [f["properties"] for f in json.load(open(os.path.join(P, "eventos.geojson")))["features"]]

    # celdas de 0.1° con centros en x.x5
    def centro(v):
        return math.floor(v * 10) / 10 + 0.05
    lons = [round(BBOX[0] + 0.05 + 0.1 * i, 2) for i in range(int((BBOX[2] - BBOX[0]) / 0.1))]
    lats = [round(BBOX[1] + 0.05 + 0.1 * j, 2) for j in range(int((BBOX[3] - BBOX[1]) / 0.1))]
    zonas = {}
    for lo in lons:
        for la in lats:
            cerca = sorted((km(lo, la, *est[s]), s) for s in est if km(lo, la, *est[s]) <= RADIO_KM)
            if cerca:
                zonas[(lo, la)] = cerca

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    diaria = defaultdict(dict)
    for sid, f, v in db.execute("select estacion_id, substr(fecha,1,10), valor from mediciones "
                                "where metrica='PP-SUM' and stack='DAY' and valor is not null"):
        if sid in est:
            diaria[sid][f] = v
    horaria = defaultdict(dict)  # máximo horario por estación y día
    for sid, f, v in db.execute("select estacion_id, substr(fecha,1,10), max(valor) from mediciones "
                                "where metrica='PP-SUM' and stack='HOUR' and valor is not null "
                                "group by estacion_id, substr(fecha,1,10)"):
        if sid in est:
            horaria[sid][f] = v

    ev_celda = defaultdict(list)
    for e in ev:
        ev_celda[(round(centro(e["lon"]), 2), round(centro(e["lat"]), 2))].append(e)
    sin_zona = sum(len(v) for k, v in ev_celda.items() if k not in zonas)

    n = defaultdict(int)
    with open(os.path.join(P, "zona_dia.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["zona_lon", "zona_lat", "fecha", "pp_max_mm", "pp_media_mm", "pp_cercana_mm", "pp3_max_mm",
                    "pph_max_mm", "n_est", "n_est_h", "n_eventos", "cod_rm", "etiqueta"])
        for (lo, la), cerca in sorted(zonas.items()):
            sids = [s for _, s in cerca]
            fechas = sorted({f for s in sids for f in diaria[s]})
            for f in fechas:
                vals = [(s, diaria[s][f]) for s in sids if f in diaria[s]]
                pmax = max(v for _, v in vals)
                if pmax < PP_MIN_MM:
                    continue
                d = date.fromisoformat(f)
                tres = [(d - timedelta(days=k)).isoformat() for k in range(3)]
                pp3 = max(sum(diaria[s].get(t, 0.0) for t in tres) for s, _ in vals)
                hv = [horaria[s][f] for s in sids if f in horaria[s]]
                cer = next(v for s, v in [(s, diaria[s].get(f)) for s in sids] if v is not None)
                hits = [e for e in ev_celda.get((lo, la), [])
                        if date.fromisoformat(e["fecha_ini"]) - timedelta(days=1) <= d
                        <= date.fromisoformat(e["fecha_fin"]) + timedelta(days=1)]
                lab = "evento" if hits else "no_evento"
                n[lab] += 1
                w.writerow([lo, la, f, round(pmax, 1), round(sum(v for _, v in vals) / len(vals), 1), round(cer, 1),
                            round(pp3, 1), round(max(hv), 1) if hv else "", len(vals), len(hv), len(hits),
                            ",".join(sorted({e["cod_rm"] for e in hits})), lab])
    res = {"zonas": len(zonas), "zona_dias": dict(n), "eventos": len(ev),
           "eventos_en_celdas_sin_estacion_a_15km": sin_zona,
           "eventos_en_alguna_zona_dia_evento": None, "agregado_declarado": "máximo entre estaciones (pp_max_mm)"}
    # eventos efectivamente representados (≥1 zona-día evento con lluvia)
    rep = set()
    for r in csv.DictReader(open(os.path.join(P, "zona_dia.csv"))):
        if r["etiqueta"] == "evento":
            rep |= set(r["cod_rm"].split(","))
    res["eventos_en_alguna_zona_dia_evento"] = len(rep)
    json.dump(res, open(os.path.join(P, "zona_dia_resumen.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
