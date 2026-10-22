#!/usr/bin/env python3
"""Paquete de calibración con eventos Y no-eventos para el runout de swarm-abm
(y para recalibrar el umbral I-D de nowcast).

Fuentes:
  - Catálogo nacional de remociones en masa de SERNAGEOMIN (GeoJSON descargado
    del MapServer Remociones_en_Masa_Chile): eventos con DÍA, MES y AÑO.
  - agromet-scraper (data/agromet.db): precipitación diaria y horaria de las
    redes INIA, DMC, MMA-DMC y otras, desde nov-2016.

Salidas en data/calibracion_runout/:
  eventos.geojson        eventos fechados al día en RM, O'Higgins y Valparaíso
                         desde nov-2016, con fecha_ini/fecha_fin normalizadas
  estaciones.csv         estaciones con precipitación en la zona y su cobertura
  lluvia_diaria.csv      estacion_id, fecha, pp_mm (día de agromet, ver README)
  lluvia_horaria.csv     solo en ventanas de ±3 días alrededor de cada evento,
                         para estaciones a ≤ RADIO_HORARIO_KM de algún evento
  eventos_cobertura.csv  por evento: estación más cercana, distancia y si hay
                         lluvia medida en [ini−1, fin+1] (con lluvia / <1 mm /
                         sin dato / sin estación)
  dias_etiquetados.csv   un día de lluvia (≥ PP_MIN_MM) por estación, con los
                         eventos del catálogo a ≤ RADIO_KM y en [ini−1, fin+1],
                         y la etiqueta evento / no_evento
  README.md              convenciones, conteos y supuestos

Uso: python3 armar_paquete.py
"""
import csv, json, math, os, re, sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.join(ROOT, "data", "calibracion_runout")
CAT = "/home/franciscoparrao/proyectos/conformal-prospectividad/data/external/Remociones_en_Masa_Chile/catalogo_RM_nacional.geojson"
DB = "/home/franciscoparrao/proyectos/agromet-scraper/data/agromet.db"
REGIONES = ("Metropolitana", "O'Higgins", "Valparaíso")
BBOX = (-72.1, -35.1, -69.8, -32.0)
DESDE = date(2016, 11, 3)
RADIO_KM = 15.0
RADIO_HORARIO_KM = 30.0
PP_MIN_MM = 1.0
MESES = {m: i for i, m in enumerate(["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
                                      "Septiembre", "Octubre", "Noviembre", "Diciembre"], 1)}


def km(lon1, lat1, lon2, lat2):
    dx = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, (lat2 - lat1) * 111.32)


def fechas(p):
    """'28' / '30 al 31' / '29 y 30' / '29 al 2' (cruza de mes) → (ini, fin)."""
    mes = MESES.get(p["d_MES_EVEN"]); anio = p["ANO_EVENTO"]
    nums = [int(x) for x in re.findall(r"\d+", str(p["DIA_EVENTO"] or ""))]
    if not (mes and anio and nums):
        return None
    try:
        ini = date(anio, mes, nums[0])
        fin_d = nums[-1]
        if fin_d >= nums[0]:
            fin = date(anio, mes, fin_d)
        else:  # rango que cruza al mes siguiente
            fin = (date(anio, mes, 28) + timedelta(days=4)).replace(day=fin_d)
    except ValueError:
        return None
    return ini, fin


def main():
    os.makedirs(OUT, exist_ok=True)
    # --- eventos -------------------------------------------------------------
    feats, motivos = [], Counter()
    for f in json.load(open(CAT))["features"]:
        p = f["properties"]
        if not any(p["d_REGION"].startswith(r) for r in REGIONES):
            continue
        fe = fechas(p)
        if fe is None:
            motivos["sin día"] += 1
            continue
        if fe[0] < DESDE:
            motivos["antes de nov-2016"] += 1
            continue
        if (fe[1] - fe[0]).days > 7:
            motivos["rango > 7 días"] += 1
            continue
        feats.append({"type": "Feature", "geometry": f["geometry"], "properties": {
            "cod_rm": p["COD_RM"], "region": p["d_REGION"], "subtipo": p["d_SUBTIPO"], "nombre": p["d_NOMBRE_R"],
            "detonante": p["d_DETONANT"], "precision": p["d_PREC_COO"], "fecha_ini": fe[0].isoformat(),
            "fecha_fin": fe[1].isoformat(), "dia_original": p["DIA_EVENTO"], "lon": p["LONGITUD"],
            "lat": p["LATITUD"], "fuente": p["FUENTE_INF"], "enlace": p["ENLACE"]}})
    json.dump({"type": "FeatureCollection", "features": feats}, open(os.path.join(OUT, "eventos.geojson"), "w"),
              ensure_ascii=False)
    ev = [f["properties"] for f in feats]

    # --- estaciones y lluvia diaria -----------------------------------------
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    est = list(c.execute(
        """select e.id, e.nombre, e.institucion, e.region, e.latitud, e.longitud, e.elevacion,
                  min(m.fecha), max(m.fecha), count(*)
           from estaciones e join mediciones m on m.estacion_id = e.id
           where m.metrica='PP-SUM' and m.stack='DAY' and e.latitud between ? and ? and e.longitud between ? and ?
           group by e.id""", (BBOX[1], BBOX[3], BBOX[0], BBOX[2])))
    with open(os.path.join(OUT, "estaciones.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["estacion_id", "nombre", "institucion", "region", "lat", "lon", "elev_m", "desde", "hasta", "dias_con_dato"])
        w.writerows(est)
    pos = {e[0]: (e[5], e[4]) for e in est}
    diaria = defaultdict(dict)
    with open(os.path.join(OUT, "lluvia_diaria.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["estacion_id", "fecha", "pp_mm"])
        for sid, f_, v in c.execute(
                f"""select estacion_id, fecha, valor from mediciones where metrica='PP-SUM' and stack='DAY'
                    and estacion_id in ({','.join('?' * len(pos))}) order by estacion_id, fecha""", list(pos)):
            if v is None:
                continue
            w.writerow([sid, f_[:10], v]); diaria[sid][f_[:10]] = v

    # --- lluvia horaria en ventanas de evento --------------------------------
    ventanas = defaultdict(set)  # estacion → fechas
    for e in ev:
        for sid, (lo, la) in pos.items():
            if km(lo, la, e["lon"], e["lat"]) <= RADIO_HORARIO_KM:
                d0, d1 = date.fromisoformat(e["fecha_ini"]), date.fromisoformat(e["fecha_fin"])
                d = d0 - timedelta(days=3)
                while d <= d1 + timedelta(days=3):
                    ventanas[sid].add(d.isoformat()); d += timedelta(days=1)
    n_h = 0
    with open(os.path.join(OUT, "lluvia_horaria.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["estacion_id", "fecha_hora", "pp_mm"])
        for sid, dias in sorted(ventanas.items()):
            for f_, v in c.execute("select fecha, valor from mediciones where metrica='PP-SUM' and stack='HOUR' and estacion_id=? order by fecha", (sid,)):
                if f_[:10] in dias and v is not None:
                    w.writerow([sid, f_, v]); n_h += 1

    # --- días etiquetados ----------------------------------------------------
    cerca = defaultdict(list)  # estacion → [(evento, dist)]
    for e in ev:
        for sid, (lo, la) in pos.items():
            d = km(lo, la, e["lon"], e["lat"])
            if d <= RADIO_KM:
                cerca[sid].append((e, d))
    n_lab = Counter()
    with open(os.path.join(OUT, "dias_etiquetados.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["estacion_id", "fecha", "pp_mm", "pp_3d_mm", "n_eventos", "dist_min_km", "cod_rm", "etiqueta"])
        for sid, serie in sorted(diaria.items()):
            for f_, v in sorted(serie.items()):
                if v < PP_MIN_MM:
                    continue
                d = date.fromisoformat(f_)
                pp3 = sum(serie.get((d - timedelta(days=k)).isoformat(), 0.0) for k in range(3))
                hits = [(e, dist) for e, dist in cerca[sid]
                        if date.fromisoformat(e["fecha_ini"]) - timedelta(days=1) <= d <= date.fromisoformat(e["fecha_fin"]) + timedelta(days=1)]
                lab = "evento" if hits else "no_evento"
                n_lab[lab] += 1
                w.writerow([sid, f_, v, round(pp3, 1), len(hits), round(min((h[1] for h in hits), default=float("nan")), 1),
                            ",".join(sorted({h[0]["cod_rm"] for h in hits})), lab])

    # --- cobertura por evento: ¿hay lluvia medida cerca y en su fecha? -------
    # Un evento sin estación, sin dato o con <1 mm en la estación más cercana NO
    # entra a dias_etiquetados como 'evento': queda aquí, a la vista, para que
    # nadie lo lea como no-evento (chubascos de verano que la red no ve).
    cob = Counter()
    with open(os.path.join(OUT, "eventos_cobertura.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cod_rm", "region", "fecha_ini", "fecha_fin", "estacion_cercana", "dist_km", "pp_max_ventana_mm", "estado"])
        for e in ev:
            sid, dist = min(((s_, km(lo, la, e["lon"], e["lat"])) for s_, (lo, la) in pos.items()), key=lambda t: t[1])
            d0 = date.fromisoformat(e["fecha_ini"]) - timedelta(days=1)
            d1 = date.fromisoformat(e["fecha_fin"]) + timedelta(days=1)
            vals = [diaria[sid].get((d0 + timedelta(days=k)).isoformat()) for k in range((d1 - d0).days + 1)]
            vals = [v for v in vals if v is not None]
            if dist > RADIO_KM:
                estado = "sin estación a 15 km"
            elif not vals:
                estado = "sin dato"
            elif max(vals) < PP_MIN_MM:
                estado = "lluvia < 1 mm"
            else:
                estado = "con lluvia medida"
            cob[estado] += 1
            w.writerow([e["cod_rm"], e["region"], e["fecha_ini"], e["fecha_fin"], sid, round(dist, 1),
                        max(vals) if vals else "", estado])

    # --- resumen ---------------------------------------------------------------
    por_evento = Counter((e["region"], e["fecha_ini"][:7]) for e in ev)
    con_est = sum(1 for e in ev if any(km(lo, la, e["lon"], e["lat"]) <= RADIO_KM for lo, la in pos.values()))
    res = {"eventos": len(ev), "descartados": dict(motivos), "eventos_con_estacion_a_15km": con_est,
           "cobertura_eventos": dict(cob),
           "estaciones": len(est), "filas_horarias": n_h, "dias_etiquetados": dict(n_lab),
           "eventos_por_region_y_mes": {f"{r} {m}": n for (r, m), n in sorted(por_evento.items())},
           "detonantes": dict(Counter(e["detonante"] for e in ev))}
    json.dump(res, open(os.path.join(OUT, "resumen.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
