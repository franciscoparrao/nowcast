#!/usr/bin/env python3
"""Entradas para el lote de calibración de la densidad de gotas que corre
swarm-abm con el binario de nowcast (~/nowcast-monitor/bin/runout_ondemand).

Agrupa los eventos del paquete con lluvia medida (eventos_cobertura.csv,
estado "con lluvia medida") por temporal y por cercanía (enlace simple a
≤ ENLACE_KM), y arma una ventana por grupo, centrada en el centroide y con
lado = extensión del grupo + 2·MARGEN_KM (mínimo LADO_MIN_KM).

Para cada ventana entrega DOS series horarias de 72 h que TERMINAN al final del
último día con evento (00:00 local, UTC−4 fijo, convención de agromet):
  - serie_max: la estación a ≤ RADIO_KM de algún evento con MÁS lluvia en esas
    72 h (la regla de forzante por evento de swarm-abm);
  - serie_cercana: la estación más cercana al centroide (sensibilidad).
No se usa el máximo hora a hora entre estaciones: armaría una tormenta más
intensa que cualquiera de las medidas. Si una estación solo tiene dato DIARIO
(Guayacán, la clave del Cajón del Maipo), su serie se DESAGREGA: total de cada
día × perfil horario de ese día en la estación con dato horario más cercana
(campo `origen`). Transfiere la forma de otra estación, no mide la intensidad.

Salida: data/calibracion_runout/entradas_densidad.json (lista de ventanas con
eventos, series, a*, trigger_volume_scale = 2·a y el comando base).
"""
import csv, json, math, os, sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
P = os.path.join(ROOT, "data", "calibracion_runout")
DB = "/home/franciscoparrao/proyectos/agromet-scraper/data/agromet.db"
SUSC_MAIPO = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif"
ENLACE_KM, MARGEN_KM, LADO_MIN_KM, RADIO_KM = 8.0, 4.0, 12.0, 15.0
A_STAR = 5.5
H0_CSV = os.path.join(ROOT, "data", "h0_centro_diaria.csv")
H0_SESGO_M = 250  # la serie H0 sesga +250/+450 m contra radiosonda (CLAUDE.md, nowcast-h0)


def km(lon1, lat1, lon2, lat2):
    return math.hypot((lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * 111.32)


def main():
    ev = {f["properties"]["cod_rm"]: f["properties"]
          for f in json.load(open(os.path.join(P, "eventos.geojson")))["features"]}
    con = [r["cod_rm"] for r in csv.DictReader(open(os.path.join(P, "eventos_cobertura.csv")))
           if r["estado"] == "con lluvia medida"]
    est = {r["estacion_id"]: r for r in csv.DictReader(open(os.path.join(P, "estaciones.csv")))}

    # grupos: mismo temporal (mes de inicio) + enlace simple espacial
    por_temporal = defaultdict(list)
    for c in con:
        por_temporal[ev[c]["fecha_ini"][:7]].append(c)
    grupos = []
    for t, cods in sorted(por_temporal.items()):
        libres = sorted(cods)  # orden fijo: ids de ventana reproducibles
        while libres:
            g = {libres.pop(0)}
            crece = True
            while crece:
                crece = False
                for c in list(libres):
                    if any(km(ev[c]["lon"], ev[c]["lat"], ev[o]["lon"], ev[o]["lat"]) <= ENLACE_KM for o in g):
                        g.add(c); libres.remove(c); crece = True
            grupos.append((t, sorted(g)))

    h0 = {r["fecha"]: float(r["h0_m"]) for r in csv.DictReader(open(H0_CSV))}
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    salida = []
    for i, (t, cods) in enumerate(grupos, 1):
        lons = [ev[c]["lon"] for c in cods]; lats = [ev[c]["lat"] for c in cods]
        clon, clat = sum(lons) / len(lons), sum(lats) / len(lats)
        ext = max([km(clon, clat, lo, la) for lo, la in zip(lons, lats)] + [0.0]) * 2
        lado = max(LADO_MIN_KM, round(ext + 2 * MARGEN_KM, 1))
        d0 = max(date.fromisoformat(ev[c]["fecha_fin"]) for c in cods) - timedelta(days=2)
        horas = [(datetime.combine(d0, datetime.min.time()) + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
                 for h in range(72)]
        cand = [s for s, r in est.items()
                if min(km(float(r["lon"]), float(r["lat"]), ev[c]["lon"], ev[c]["lat"]) for c in cods) <= RADIO_KM]

        def serie(sid):
            d = dict(db.execute("select fecha, valor from mediciones where estacion_id=? and metrica='PP-SUM' "
                                "and stack='HOUR' and fecha between ? and ?", (sid, horas[0], horas[-1])))
            vals = [d.get(h) for h in horas]
            return vals, sum(v is not None for v in vals)

        series = {s: serie(s) for s in cand}
        con_dato = {s: v for s, v in series.items() if v[1] >= 60}  # ≥ 60 de 72 horas con dato
        origen = {s: "horaria medida" for s in con_dato}
        # Desagregación de las candidatas que solo tienen dato diario.
        dias = [(d0 + timedelta(days=k)).isoformat() for k in range(3)]
        donantes = sorted((km(clon, clat, float(r["lon"]), float(r["lat"])), sid) for sid, r in est.items()
                          if sid not in cand or sid in con_dato)
        for sid in cand:
            if sid in con_dato:
                continue
            diario = dict(db.execute("select substr(fecha,1,10), valor from mediciones where estacion_id=? and "
                                     "metrica='PP-SUM' and stack='DAY' and substr(fecha,1,10) in (?,?,?)", (sid, *dias)))
            if len(diario) < 3 or sum(v or 0 for v in diario.values()) <= 0:
                continue
            for dist, don in donantes:
                if dist > 50:
                    break
                vals, n = serie(don)
                if n < 60:
                    continue
                out = []
                for k, dia in enumerate(dias):
                    bloque = [x or 0.0 for x in vals[24 * k:24 * k + 24]]
                    tot = sum(bloque)
                    perfil = [x / tot for x in bloque] if tot > 0 else [1 / 24] * 24
                    out += [(diario.get(dia) or 0.0) * f for f in perfil]
                con_dato[sid] = (out, 72)
                origen[sid] = f"desagregada: total diario × perfil horario de {est[don]['nombre']} ({dist:.0f} km del centro)"
                break
        if not con_dato:
            salida.append({"id": f"v{i:02d}", "temporal": t, "eventos": cods, "sin_serie_horaria": True})
            continue
        smax = max(con_dato, key=lambda s: sum(x or 0 for x in con_dato[s][0]))
        scer = min(con_dato, key=lambda s: km(clon, clat, float(est[s]["lon"]), float(est[s]["lat"])))
        fmt = lambda s: {"estacion_id": s, "nombre": est[s]["nombre"],
                         "dist_centro_km": round(km(clon, clat, float(est[s]["lon"]), float(est[s]["lat"])), 1),
                         "origen": origen[s],
                         "horas_con_dato": con_dato[s][1], "total_mm": round(sum(x or 0 for x in con_dato[s][0]), 1),
                         "mm_por_hora": [round(x or 0.0, 2) for x in con_dato[s][0]],
                         # Cota inferior para las desagregadas: total del día
                         # repartido parejo en 24 h (la intensidad horaria del
                         # perfil transferido NO es medida).
                         "mm_por_hora_uniforme_diaria": ([round(sum((x or 0.0) for x in con_dato[s][0][24 * k:24 * k + 24]) / 24, 3)
                                                          for k in range(3) for _ in range(24)]
                                                         if origen[s].startswith("desagregada") else None)}
        salida.append({
            "id": f"v{i:02d}", "temporal": t, "centro_lon": round(clon, 4), "centro_lat": round(clat, 4),
            "lado_km": lado, "inicio_local_utc_menos_4": horas[0],
            "eventos": [{"cod_rm": c, "lon": ev[c]["lon"], "lat": ev[c]["lat"], "subtipo": ev[c]["subtipo"]} for c in cods],
            "serie_max": fmt(smax), "serie_cercana": fmt(scer),
            "isoterma_m": round(sum(h0[d] for d in dias) / 3 - H0_SESGO_M, -1),
            "isoterma_nota": f"media de H0 (serie 30-36 °S) en {dias[0]}..{dias[-1]} menos {H0_SESGO_M} m de sesgo",
            "id_a": A_STAR, "trigger_volume_scale": 2 * A_STAR, "susceptibilidad": SUSC_MAIPO,
            "comando_base": ["~/nowcast-monitor/bin/runout_ondemand", f"--lon={round(clon, 4)}", f"--lat={round(clat, 4)}",
                             "--size-km", str(lado), "--rain-mm-per-hour", "<serie>", "--trigger-id-a", str(A_STAR),
                             "--trigger-volume-scale", str(2 * A_STAR), "--trigger-seeding", "follow",
                             "--trigger-drops-per-km2", "<densidad>", "--isotherm-m", "<isoterma_m>",
                             "--sediment", "0.5", "--susceptibility", SUSC_MAIPO, "--params", "chanaral_enhanced",
                             "--seed", "<semilla>", "--out", "<dir>"]})
    json.dump(salida, open(os.path.join(P, "entradas_densidad.json"), "w"), indent=1, ensure_ascii=False)
    for v in salida:
        if v.get("sin_serie_horaria"):
            print(f"{v['id']} {v['temporal']} {len(v['eventos'])} eventos — SIN serie horaria con ≥60 h de dato")
            continue
        print(f"{v['id']} {v['temporal']} {len(v['eventos']):3d} eventos · lado {v['lado_km']:5.1f} km · "
              f"máx {v['serie_max']['nombre'][:16]} {v['serie_max']['total_mm']} mm ({v['serie_max']['origen'][:11]}) · "
              f"cercana {v['serie_cercana']['nombre'][:16]} {v['serie_cercana']['total_mm']} mm ({v['serie_cercana']['origen'][:11]})")


if __name__ == "__main__":
    main()
