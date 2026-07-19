#!/usr/bin/env python3
"""Mapa de riesgo por localidad: peligro físico × exposición urbana, a 30 m.

Cruza `hazard_x_susc_30m_<cuenca>.tif` (gatillo climático × susceptibilidad
real de terreno, ya generado por `susc_overlay.py`) con el footprint del ABM
de remociones en masa (`runout_localidad.py`, cuando existe y es plausible) y
el límite urbano real (Censo 2024, `Limite_Urbano_CPV24`) para dar contexto
de exposición, recortado a una ventana angosta alrededor del pueblo.

EXPOSICIÓN (2026-07-17): se calcula de verdad, no solo se anota población
como contexto — el footprint del ABM se cruza con el polígono urbano real
(rasterizado a la misma grilla) para dar la FRACCIÓN de área urbana cubierta
y una población expuesta estimada (fracción × población urbana censada,
ponderación areal uniforme — sigue siendo una cota gruesa, igual que
`comuna_impact.py`, no un conteo de edificios).

VULNERABILIDAD: se trae el índice social ya calculado por
`comuna_vulnerability.py` (Censo 2024) y se muestra APARTE — sigue la
convención ya establecida (peligro y vulnerabilidad SEPARADOS, nunca
multiplicados: la vulnerabilidad es demográfica, no física, y fusionarla en
un solo índice oculta cuál de las dos está pesando). El lector arma el
"riesgo real" con las tres piezas (peligro, exposición, vulnerabilidad) él
mismo, no un índice compuesto inventado por el sistema.

Uso:
    python3 risk_map_localidad.py --name vallenar --lon -70.76517 --lat -28.5774 \
        --domain atacama --comuna VALLENAR
Salidas en ~/nowcast-forecast/risk_maps/<name>/mapa_riesgo.png (+ .tif del
recorte hazard×susc para uso en GIS).
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from pyproj import Transformer

from susc_overlay import DOMAIN_BASINS, raster_coverage_warning
from runout_localidad import pick_susceptibility, point_value

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CENSUS_GPKG = os.path.expanduser(
    "~/proyectos/censo-2024/Cartografia_censo2024_Pais/Cartografia_censo2024_Pais.gpkg")


def latest_vulnerability(comuna):
    """Índice de vulnerabilidad social (Censo 2024) para `comuna` — busca en
    cualquier ciclo disponible (el dato es censal, no depende del ciclo de
    pronóstico) y toma el más reciente que lo tenga."""
    paths = sorted(glob.glob(os.path.expanduser(
        "~/nowcast-forecast/*/comuna_impact/comuna_vulnerability.json")), reverse=True)
    if not paths:
        return None
    import geopandas as gpd
    gdf = gpd.read_file(CENSUS_GPKG, layer="Comunal_CPV24", columns=["CUT", "COMUNA"])
    row = gdf[gdf["COMUNA"].str.upper() == comuna.upper()]
    if row.empty:
        return None
    cut = str(int(row.iloc[0]["CUT"]))
    for p in paths:
        v = json.load(open(p)).get("por_comuna", {}).get(cut)
        if v:
            v["_source"] = p
            return v
    return None


def rasterize_urban(urb_geom, out_shape, win_transform):
    from rasterio.features import rasterize
    return rasterize([(urb_geom, 1)], out_shape=out_shape, transform=win_transform,
                     fill=0, dtype="uint8").astype(bool)


def exposure_from_footprint(fp, urban_mask, pob):
    """Fracción del polígono urbano cubierta por el footprint del runout +
    población expuesta estimada (ponderación areal uniforme sobre `pob`,
    cota gruesa — no es conteo de edificios ni de personas reales)."""
    n_urban = int(urban_mask.sum())
    if n_urban == 0:
        return None
    frac = float((urban_mask & (fp >= 1)).sum()) / n_urban
    return {
        "urban_area_footprint_frac": round(frac, 3),
        "poblacion_expuesta_estimada": round(frac * pob) if pob else None,
        "nota": "ponderación areal uniforme sobre el polígono urbano — cota gruesa, no conteo real",
    }


def id_exceedance_at_point(fdir, lon, lat, id_a, id_b, dt_hours=1.0, max_window_h=72.0):
    """Máxima excedencia I-D sobre ventanas rodantes en el punto: E = I / (a·D^-b).

    Es la MISMA cantidad que el motor calcula internamente (threshold.rs:
    `exceedance`), recomputada acá sobre la serie horaria de la forzante en el
    punto exacto. E=1 significa justo en el umbral; E=3, tres veces por sobre.
    No satura: es la razón física cruda.

    Por qué se necesita (hallazgo 2026-07-18): el factor de gatillo que usa
    `hazard_max` es la transformada LOGÍSTICA de esta excedencia
    (trigger.rs, k=4), y por diseño llega a T≈0.98 ya en E=2 — o sea que a
    partir de dos veces el umbral deja de discriminar. En un evento extremo
    eso vuelve el índice compuesto insensible a la intensidad: Vallenar, con
    194 mm en 3 días, y una localidad con un tercio de esa lluvia quedaban con
    el mismo factor ≈1.0, y el ranking pasaba a estar decidido solo por la
    susceptibilidad estática. Usar E cruda restaura la discriminación.

    Se recorre a la resolución NATIVA de la forzante (horaria en GFS), no
    agregada a días: agregar a diario baja la intensidad máxima resoluble a
    total/24h y suprime estructuralmente el disparo I-D — el mismo techo de
    resolución ya documentado para CR2MET en `resolution_headtohead.rs`.
    """
    import glob as _glob
    files = sorted(_glob.glob(os.path.join(fdir, "steps", "step_*.tif")))
    if not files:
        return None
    serie = [max(0.0, point_value(f, lon, lat)) for f in files]
    n = len(serie)
    pre = [0.0] * (n + 1)
    for i, v in enumerate(serie):
        pre[i + 1] = pre[i] + v
    max_m = max(1, int(round(max_window_h / dt_hours)))
    best = {"exceedancia": 0.0, "duracion_h": None, "intensidad_mm_h": None, "profundidad_mm": None}
    for end in range(1, n + 1):
        for m in range(1, min(max_m, end) + 1):
            depth = pre[end] - pre[end - m]
            if depth <= 0:
                continue
            dur = m * dt_hours
            inten = depth / dur
            crit = id_a * (dur ** (-id_b))
            if crit <= 0:
                continue
            e = inten / crit
            if e > best["exceedancia"]:
                best = {"exceedancia": round(e, 3), "duracion_h": dur,
                        "intensidad_mm_h": round(inten, 2), "profundidad_mm": round(depth, 1)}
    return best if best["duracion_h"] is not None else None


def model_agreement(forecast_dir, domain, lon, lat):
    """Acuerdo entre modelos sobre la lluvia en el punto.

    Por qué hace falta: el peligro que reporta este sistema sale de
    `hazard_max_worst_available`, el PEOR de las fuentes disponibles. Eso es
    defendible por precaución, pero borra la distinción que más importa para
    decidir qué hacer: si el número lo sostienen dos modelos o uno solo.
    Hallazgo 2026-07-18: en este evento GFS y ECMWF discrepan por un factor
    50 en dónde cae la tormenta — en Alto del Carmen GFS da 208 mm y ECMWF
    4 mm, mientras que en Limarí (Juntas, Combarbalá) los dos coinciden y
    ECMWF es incluso más húmedo. Ambos casos salían con el mismo aspecto en
    el reporte.

    Se compara la LLUVIA ACUMULADA, no la excedencia I-D: ECMWF se lee a 3 h
    y GFS a 1 h, y el umbral I-D castiga estructuralmente la resolución
    temporal gruesa (la intensidad máxima resoluble es profundidad/3h contra
    profundidad/1h), así que comparar excedencias estaría sesgado contra
    ECMWF. Los totales acumulados no tienen ese sesgo.

    ICON se reporta pero NO entra en la etiqueta: su acumulado no es
    comparable sin verificar antes la reconstrucción horaria desde sus pasos
    nativos (en el ciclo 20260718T18z declara 21 pasos nativos y escribe 120,
    y da medias de dominio muy por debajo de las otras dos fuentes). Queda
    pendiente resolverlo en vez de mezclarlo a ciegas.
    """
    out = {"gfs_mm": None, "ecmwf_mm": None, "icon_mm": None,
           "razon_min_max": None, "confianza": None, "nota": None}
    base = os.path.join(forecast_dir, domain)
    for clave, sub in (("gfs_mm", ""), ("ecmwf_mm", "ecmwf"), ("icon_mm", "icon")):
        p = os.path.join(base, sub, "rain_liquid_total.tif")
        if os.path.exists(p):
            v = point_value(p, lon, lat)
            out[clave] = round(float(v), 1) if np.isfinite(v) else None

    g, e = out["gfs_mm"], out["ecmwf_mm"]
    if g is None or e is None:
        out["confianza"] = "indeterminada"
        out["nota"] = "falta una de las dos fuentes deterministas para comparar"
        return out

    mx, mn = max(g, e), min(g, e)
    out["razon_min_max"] = round(mn / mx, 2) if mx > 0 else None
    if mx < 50:
        out["confianza"] = "sin señal relevante"
        out["nota"] = f"ningún modelo supera 50 mm acumulados (GFS {g}, ECMWF {e})"
    elif out["razon_min_max"] >= 0.5:
        out["confianza"] = "ALTA"
        # OJO con la redacción: razón 0.5 significa que un modelo ve la MITAD
        # que el otro. Eso es acuerdo sobre QUE llueve fuerte acá, no sobre
        # cuánto — decir "coinciden en magnitud" sería sobreafirmar.
        out["nota"] = (f"ambos modelos ven lluvia en este punto (GFS {g} mm, "
                       f"ECMWF {e} mm; el menor es {out['razon_min_max']:.0%} del mayor). "
                       f"La etiqueta mide ACUERDO, no magnitud: mirar los mm para saber "
                       f"si la lluvia además es grande")
    elif out["razon_min_max"] >= 0.2:
        out["confianza"] = "MEDIA"
        out["nota"] = (f"ambos ven lluvia pero difieren fuerte en magnitud (GFS {g} mm, "
                       f"ECMWF {e} mm; el menor es {out['razon_min_max']:.0%} del mayor)")
    else:
        cual = "GFS" if g > e else "ECMWF"
        out["confianza"] = "BAJA"
        out["nota"] = (f"la señal la sostiene SOLO {cual} (GFS {g} mm, ECMWF {e} mm; "
                       f"razón {out['razon_min_max']}) — el otro modelo no reproduce "
                       f"esta lluvia, tratar el peligro como una de dos hipótesis")
    return out


def point_susceptibility(susc_path, lon, lat):
    """Susceptibilidad estática cruda en el punto, en [0,1]."""
    if not susc_path or not os.path.exists(susc_path):
        return None
    v = point_value(susc_path, lon, lat)
    return round(float(v), 3) if np.isfinite(v) else None


def composite_risk(hazard, exposure_frac, vulnerability_idx):
    """Índice compuesto = media geométrica de peligro×exposición×vulnerabilidad.
    Media geométrica, no producto simple: un producto directo castiga demasiado
    fuerte si cualquiera de las tres está cerca de 0 (ej. exposición baja porque
    el footprint apenas roza el borde urbano) y esconde que las otras dos son
    altas.

    El término de PELIGRO es `susceptibilidad × excedencia I-D` (ver
    `id_exceedance_at_point`), NO el factor de gatillo saturado. Por eso el
    índice ya no está acotado a [0,1]: un valor >1 en el término de peligro
    significa terreno susceptible bajo una tormenta varias veces sobre el
    umbral, y eso debe poder distinguirse de "justo en el umbral". Exposición y
    vulnerabilidad sí siguen en [0,1].

    NO reemplaza el reporte separado de las tres capas — es una CUARTA pieza
    para ranking relativo entre localidades, no un índice oficial. Hereda un
    descalce de escala real: peligro/exposición son por footprint/punto,
    vulnerabilidad es a nivel comuna (no varía dentro de la comuna).
    """
    vals = [v for v in (hazard, exposure_frac, vulnerability_idx) if v is not None]
    if len(vals) < 3:
        return None
    return round(float(np.prod(vals)) ** (1 / 3), 3)


def basin_hazard_tif(forecast_dir, domain, lon, lat):
    """El hazard_x_susc_30m_<basin>.tif del dominio con dato válido en el
    punto, MÁS CERCANO al centro de su propio rectángulo — mismo criterio
    (y misma corrección) que `pick_susceptibility` en runout_localidad.py y
    select_watchlist.py: rectángulos de cuenca vecinos se solapan, y "la
    primera con dato" elegía por orden de lista en vez de por pertenencia
    real (hallazgo 2026-07-17, Vallenar → Copiapó en vez de Huasco)."""
    ddir = os.path.join(forecast_dir, domain)
    candidates = []
    for rel in DOMAIN_BASINS.get(domain, []):
        stem = os.path.splitext(os.path.basename(rel))[0]
        basin = rel.split("/")[0]
        if stem not in ("susceptibility_XGBoost", "susceptibility_RandomForest", "susceptibility_Ridge"):
            basin = f"{basin}_{stem.split('_')[-1]}"
        p = os.path.join(ddir, f"hazard_x_susc_30m_{basin}.tif")
        if not os.path.exists(p):
            continue
        with rasterio.open(p) as src:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = t.transform(lon, lat)
            b = src.bounds
            if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                continue
            v = list(src.sample([(x, y)]))[0][0]
            if not np.isfinite(v):
                continue
            cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
            candidates.append((((x - cx) ** 2 + (y - cy) ** 2) ** 0.5, p))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--domain", required=True, choices=list(DOMAIN_BASINS))
    ap.add_argument("--comuna", required=True, help="nombre COMUNA (Censo 2024) para el limite urbano")
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--window-km", type=float, default=6.0)
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/risk_maps"))
    # Umbral I-D para la excedencia del término de peligro. Los defaults son la
    # variante `regional-a5.5` (a=5.5, b=0.39), que es la que susc_overlay usa
    # para GFS/ECMWF; para ICON la variante del proyecto es `caine-global`
    # (a=14.82). Ver HAZARD_VARIANT_BY_SOURCE en susc_overlay.py.
    ap.add_argument("--id-a", type=float, default=5.5)
    ap.add_argument("--id-b", type=float, default=0.39)
    ap.add_argument("--dt-hours", type=float, default=1.0,
                    help="paso de la forzante (GFS horario = 1.0)")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    hz_path = basin_hazard_tif(forecast_dir, a.domain, a.lon, a.lat)
    if hz_path is None:
        sys.exit(f"[{a.name}] sin hazard_x_susc_30m con dato valido en el punto — "
                  f"correr susc_overlay.py primero")

    susc_path = pick_susceptibility(a.domain, a.lon, a.lat)
    cobertura = (raster_coverage_warning(susc_path, a.lon, a.lat) if susc_path
                 else {"sospechoso": True, "std_local": None, "distancia_borde_km": None,
                       "nota": "sin raster de susceptibilidad crudo resoluble para el chequeo"})
    if cobertura["sospechoso"]:
        print(f"[{a.name}] AVISO QA cobertura de raster: {cobertura['nota']}")

    out = os.path.join(a.out_root, a.name)
    os.makedirs(out, exist_ok=True)

    with rasterio.open(hz_path) as src:
        t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x0, y0 = t.transform(a.lon, a.lat)
        half = a.window_km * 1000 / 2
        win = from_bounds(x0 - half, y0 - half, x0 + half, y0 + half, src.transform)
        hz = src.read(1, window=win)
        win_transform = src.window_transform(win)
        crs = src.crs
        profile = src.profile.copy()
    profile.update(height=hz.shape[0], width=hz.shape[1], transform=win_transform,
                    dtype="float32", nodata=np.nan)
    out_tif = os.path.join(out, "hazard_x_susc_recorte.tif")
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(hz.astype("float32"), 1)

    fp_path = os.path.join(a.runouts, a.name, "runout_footprint.tif")
    fp = None
    if os.path.exists(fp_path):
        with rasterio.open(fp_path) as src:
            from rasterio.warp import reproject, Resampling
            fp_arr = np.zeros(hz.shape, dtype="float32")
            reproject(source=rasterio.band(src, 1), destination=fp_arr,
                      dst_transform=win_transform, dst_crs=crs,
                      resampling=Resampling.nearest)
            fp = fp_arr

    urb = None
    try:
        import geopandas as gpd
        urb_all = gpd.read_file(CENSUS_GPKG, layer="Limite_Urbano_CPV24")
        urb_c = urb_all[urb_all["LOCALIDAD"].str.upper().str.contains(a.comuna.upper(), na=False)
                         | urb_all.get("ENTIDAD", "").astype(str).str.upper().str.contains(a.comuna.upper(), na=False)]
        if urb_c.empty:
            # fallback: la entidad mas poblada cuya comuna coincide via CUT no
            # esta indexada por nombre aqui -- se intenta con el propio nombre
            # de la localidad tal cual viene en el argumento.
            urb_c = urb_all[urb_all["LOCALIDAD"].str.upper() == a.comuna.upper()]
        if urb_c.empty:
            # FALLBACK A ALDEAS (2026-07-18), hermana del mismo fallback en
            # select_watchlist.py: las comunas rurales no tienen polígono en
            # Limite_Urbano_CPV24 y sin esto quedaban sin exposición ni índice
            # compuesto — justo las de mayor peligro del evento (Alto del
            # Carmen, Río Hurtado). `Aldeas_CPV24` trae el mismo esquema.
            ald_all = gpd.read_file(CENSUS_GPKG, layer="Aldeas_CPV24")
            ald_c = ald_all[ald_all["ENTIDAD"].astype(str).str.upper()
                            .str.contains(a.comuna.upper(), na=False)
                            | ald_all["COMUNA"].astype(str).str.upper()
                            .str.contains(a.comuna.upper(), na=False)]
            if not ald_c.empty:
                urb_c = ald_c
                print(f"[{a.name}] sin área urbana censada; exposición calculada "
                      f"sobre el polígono de ALDEA (Aldeas_CPV24)")
        if not urb_c.empty:
            urb_c = urb_c.sort_values("n_per", ascending=False)
            urb = urb_c.to_crs(crs)
    except Exception as e:
        print(f"[{a.name}] AVISO: sin limite urbano ({e})", file=sys.stderr)

    pob = int(urb.iloc[0].get("n_per", 0) or 0) if urb is not None else None
    urban_mask = None
    exposure = None
    if urb is not None and fp is not None:
        urban_mask = rasterize_urban(urb.iloc[0].geometry, hz.shape, win_transform)
        exposure = exposure_from_footprint(fp, urban_mask, pob)
        if exposure:
            print(f"[{a.name}] exposición: {exposure['urban_area_footprint_frac']*100:.0f}% del área "
                  f"urbana bajo footprint | población expuesta estimada "
                  f"{exposure['poblacion_expuesta_estimada']}")

    vuln = latest_vulnerability(a.comuna)
    if vuln:
        print(f"[{a.name}] vulnerabilidad social (Censo 2024, comuna {a.comuna}): "
              f"índice {vuln['indice_vulnerabilidad']:.3f} "
              f"(fuente: {os.path.relpath(vuln['_source'], os.path.expanduser('~/nowcast-forecast'))})")
    else:
        print(f"[{a.name}] AVISO: sin índice de vulnerabilidad disponible para {a.comuna} "
              f"— correr comuna_vulnerability.py", file=sys.stderr)

    # Peligro puntual: valor de hazard_x_susc en el punto exacto del pueblo
    # (misma capa que colorea el mapa) -- para el compuesto, no para el mapa.
    row_i, col_i = ~win_transform * (x0, y0)
    row_i, col_i = int(row_i), int(col_i)
    hazard_pt = None
    if 0 <= row_i < hz.shape[0] and 0 <= col_i < hz.shape[1]:
        v = float(hz[row_i, col_i])
        hazard_pt = v if np.isfinite(v) else None

    # --- término de peligro NO SATURADO --------------------------------------
    # susceptibilidad estática × excedencia I-D cruda, en vez del factor de
    # gatillo logístico (que llega a 0.98 en E=2 y deja de discriminar).
    acuerdo = model_agreement(forecast_dir, a.domain, a.lon, a.lat)
    print(f"[{a.name}] acuerdo entre modelos: {acuerdo['confianza']} — {acuerdo['nota']}")

    exc = id_exceedance_at_point(os.path.join(forecast_dir, a.domain), a.lon, a.lat,
                                  a.id_a, a.id_b, dt_hours=a.dt_hours)
    susc_pt = point_susceptibility(susc_path, a.lon, a.lat)
    peligro_ns = None
    if exc is not None and susc_pt is not None:
        peligro_ns = round(susc_pt * exc["exceedancia"], 3)
        print(f"[{a.name}] peligro no saturado: susc {susc_pt} × excedencia I-D "
              f"{exc['exceedancia']} (ventana {exc['duracion_h']:.0f} h, "
              f"{exc['intensidad_mm_h']} mm/h, {exc['profundidad_mm']} mm) = {peligro_ns}")
    else:
        print(f"[{a.name}] AVISO: sin término de peligro no saturado "
              f"(excedencia={exc is not None}, susc={susc_pt is not None}); "
              f"se cae al factor de gatillo saturado", file=sys.stderr)

    composite = composite_risk(
        peligro_ns if peligro_ns is not None else hazard_pt,
        exposure["urban_area_footprint_frac"] if exposure else None,
        vuln["indice_vulnerabilidad"] if vuln else None,
    )
    # El compuesto viejo (gatillo saturado) se conserva para poder auditar el
    # cambio de definición del 2026-07-18, no para reportarlo como resultado.
    composite_legacy = composite_risk(
        hazard_pt,
        exposure["urban_area_footprint_frac"] if exposure else None,
        vuln["indice_vulnerabilidad"] if vuln else None,
    )
    if composite is not None:
        print(f"[{a.name}] índice compuesto (media geométrica peligro×exposición×vulnerabilidad, "
              f"NO oficial, solo ranking relativo): {composite}"
              + (f"  [antes, con gatillo saturado: {composite_legacy}]"
                 if composite_legacy is not None else ""))

    with open(os.path.join(out, "riesgo.json"), "w") as f:
        json.dump({"name": a.name, "poblacion_urbana": pob, "hazard_en_punto": hazard_pt,
                  "exposicion": exposure, "vulnerabilidad": vuln,
                  "susceptibilidad_en_punto": susc_pt,
                  "acuerdo_modelos": acuerdo,
                  "exceedancia_id": exc,
                  "peligro_no_saturado": peligro_ns,
                  "peligro_nota": ("susceptibilidad × excedencia I-D cruda (E = I/(a·D^-b), "
                                   f"a={a.id_a}, b={a.id_b}, ventanas rodantes a {a.dt_hours} h). "
                                   "Reemplaza al factor de gatillo logístico desde 2026-07-18: "
                                   "ese factor llega a 0.98 en E=2 y dejaba de discriminar justo "
                                   "en régimen extremo. NO acotado a [0,1]: >1 = terreno "
                                   "susceptible bajo tormenta varias veces sobre el umbral"),
                  "indice_compuesto": composite,
                  "indice_compuesto_saturado_legacy": composite_legacy,
                  "indice_compuesto_nota": ("media geométrica (peligro×exposición×vulnerabilidad)^(1/3) "
                                            "-- NO oficial, solo para ranking relativo entre localidades; "
                                            "hereda descalce de escala (vulnerabilidad es a nivel comuna). "
                                            "El término de peligro NO satura (ver peligro_nota); "
                                            "indice_compuesto_saturado_legacy trae el valor con la "
                                            "definición vieja, solo para auditar el cambio"),
                  "cobertura_raster_sospechosa": cobertura["sospechoso"],
                  "cobertura_raster_nota": cobertura["nota"]},
                  f, indent=1, ensure_ascii=False)

    # --- capas GIS (GeoTIFF), una por dimension + el compuesto espacial ------
    gis_dir = os.path.join(out, "gis")
    os.makedirs(gis_dir, exist_ok=True)
    base_profile = dict(profile)
    base_profile.update(count=1, compress="deflate")

    def write_layer(fname, arr, nodata=np.nan, dtype="float32"):
        p = dict(base_profile)
        p.update(dtype=dtype, nodata=nodata)
        with rasterio.open(os.path.join(gis_dir, fname), "w", **p) as dst:
            dst.write(arr.astype(dtype), 1)

    write_layer("01_peligro_hazard_x_susc.tif", hz)
    if fp is not None:
        write_layer("02_footprint_runout.tif", fp, nodata=None, dtype="uint8")
    if urban_mask is not None:
        write_layer("03_exposicion_mascara_urbana.tif", urban_mask.astype("float32"),
                   nodata=None, dtype="uint8")
    if vuln:
        vuln_const = np.full(hz.shape, vuln["indice_vulnerabilidad"], dtype="float32")
        write_layer("04_vulnerabilidad_comuna_constante.tif", vuln_const)
    if urban_mask is not None and fp is not None and vuln:
        with np.errstate(invalid="ignore"):
            expo_spatial = (urban_mask & (fp >= 1)).astype("float32")
            prod = hz * expo_spatial * vuln["indice_vulnerabilidad"]
            comp_raster = np.where(prod > 0, np.cbrt(prod), np.nan).astype("float32")
        write_layer("05_indice_compuesto_espacial.tif", comp_raster)
        print(f"[{a.name}] capas GIS -> {gis_dir}/ (5 GeoTIFF: peligro, footprint, "
              f"exposición, vulnerabilidad, compuesto espacial)")
    else:
        print(f"[{a.name}] capas GIS parciales -> {gis_dir}/ (faltan insumos para el compuesto espacial)")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    HAZARD_CMAP = mcolors.LinearSegmentedColormap.from_list(
        "hazard", ["#eef1ee", "#cfe0d6", "#8fb99a", "#e8c468", "#c1652c", "#7a2015"])
    INK, PAPER, RULE, DATA_TEAL = "#1b232c", "#f2f0e8", "#c9c3b0", "#2c5f6e"

    b = rasterio.transform.array_bounds(hz.shape[0], hz.shape[1], win_transform)
    left, bottom, right, top = b
    fig, ax = plt.subplots(figsize=(7.2, 7.6), dpi=200)
    ax.set_facecolor(PAPER)
    im = ax.imshow(np.ma.masked_invalid(hz), extent=[left, right, bottom, top], origin="upper",
                   cmap=HAZARD_CMAP, vmin=0, vmax=1, zorder=2, interpolation="nearest")
    if fp is not None and np.nansum(fp) > 0:
        ax.imshow(np.ma.masked_where(fp < 1, fp), extent=[left, right, bottom, top], origin="upper",
                  cmap=mcolors.ListedColormap(["none", "#1b232c"]), vmin=0, vmax=1,
                  alpha=0.35, zorder=3, interpolation="nearest")
        ax.contour(fp, levels=[0.5], extent=[left, right, bottom, top], origin="upper",
                   colors=[INK], linewidths=1.1, zorder=4)
    if urb is not None:
        urb.boundary.plot(ax=ax, edgecolor=DATA_TEAL, linewidth=1.6, zorder=5)
    ax.plot(x0, y0, marker="*", markersize=18, markerfacecolor="#ffd23f",
           markeredgecolor=INK, markeredgewidth=1.0, zorder=6)
    ax.set_xlim(left, right); ax.set_ylim(bottom, top)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor(RULE)

    cax = fig.add_axes([0.14, 0.035, 0.72, 0.016])
    cb = plt.colorbar(im, cax=cax, orientation="horizontal")
    cb.set_label("Gatillo climático × susceptibilidad de terreno (30 m)", fontsize=8, color=INK)
    cb.outline.set_edgecolor(RULE)

    pob_txt = f" · población urbana censada {pob:,}" if pob else ""
    fig.suptitle(f"Mapa de riesgo — {a.name.replace('_', ' ').title()}", fontsize=13,
                fontweight="bold", color=INK, y=0.97, ha="center")
    fig.text(0.5, 0.925, f"Ventana {a.window_km:.0f} km · límite urbano real (Censo 2024){pob_txt}",
             fontsize=8.5, color="#5a6259", ha="center")
    rev_parts = []
    if exposure:
        rev_parts.append(f"Exposición: {exposure['urban_area_footprint_frac']*100:.0f}% del área urbana "
                         f"bajo footprint · ~{exposure['poblacion_expuesta_estimada']:,} personas expuestas (est.)")
    if vuln:
        rev_parts.append(f"Vulnerabilidad social (comuna, Censo 2024): índice {vuln['indice_vulnerabilidad']:.2f}")
    if composite is not None:
        rev_parts.append(f"Índice compuesto (no oficial): {composite}")
    if rev_parts:
        fig.text(0.5, 0.90, "  ·  ".join(rev_parts), fontsize=8, color="#8a5a30",
                fontweight="bold", ha="center")
    legend_txt = ("polígono teal = límite urbano · contorno negro = footprint runout ABM "
                  "(escenario ilustrativo, ver reporte para plausibilidad) · estrella = punto de referencia. "
                  "Peligro, exposición y vulnerabilidad se reportan SEPARADOS; el índice compuesto (media "
                  "geométrica de las tres) es una cuarta pieza para ranking relativo, no un reemplazo ni "
                  "un índice oficial.")
    fig.text(0.5, 0.055, legend_txt, fontsize=6.6, color="#5a6259", ha="center", wrap=True)

    png_path = os.path.join(out, "mapa_riesgo.png")
    plt.savefig(png_path, facecolor=PAPER, bbox_inches="tight")
    plt.close(fig)
    print(f"[{a.name}] mapa -> {png_path}")
    print(f"[{a.name}] recorte georreferenciado -> {out_tif}")


if __name__ == "__main__":
    main()
