#!/usr/bin/env python3
"""Runout on-demand por LOCALIDAD, alimentado por el pronóstico vigente.

Envuelve al runner Rust (`runout_ondemand`) con todo lo que una corrida "en
caliente" necesita durante el evento: extrae del ciclo GFS vigente la señal
puntual (gatillo, hora de cruce, lluvia líquida diaria — elige los 3 peores
días consecutivos, que es lo que el ABM admite — e isoterma del dominio),
selecciona el raster de susceptibilidad de cuenca que cubre el punto, corre
la simulación y reporta el footprint EN RELACIÓN AL PUEBLO (fracción alcanzada
a 0.5/1/2 km del centro y distancia mínima).

ROTULADO: escenario ilustrativo (parámetros Atacama como análogo, sedimento
constante, lluvia uniforme en la ventana, 1 seed) — insumo de análisis
interno, no mapa de amenaza validado ni aviso oficial. Si la susceptibilidad
media de la ventana es ~0 (punto fuera de las cuencas modeladas), la corrida
NO es informativa y se dice a gritos: sin semilla de terreno no hay flujo.

PLAUSIBILIDAD FÍSICA (2026-07-17, hallazgo watchlist O'Higgins post-fix GFS):
el ABM está calibrado sobre terreno de abanico aluvial abrupto (Copiapó 2015,
>1000 m de relieve). En localidades de fondo de valle plano (Rancagua,
Requínoa, Graneros) el footprint se esparce de forma difusa sobre 50-60% de
la ventana sin concentrarse cuesta abajo — no es la firma de un aluvión
canalizado, es la ausencia de una fuente empinada real cerca del punto. Dos
criterios DUROS sobre `stats.json`, análogos al guard de `flood_localidad.py`:
sin relieve real en la ventana (<150 m), o el footprint no baja
proporcionalmente al relieve disponible (Δelevación/relieve < 0.08).

DOMINIO DE CALIBRACIÓN (2026-07-17, mismo hallazgo, alcance más amplio): el
preset por defecto (`de`, `data/best_params_de.json`) se ajustó por evolución
diferencial contra el footprint REAL de Copiapó 2015 — y ni ahí el ajuste es
apretado (IoU 0.171 contra el observado, ver `runout_ondemand.rs`). Fuera de
Atacama no hay ESE ni ningún otro anclaje real: no hay footprint mapeado de un
evento documentado en O'Higgins/Maule contra el cual recalibrar. Referencia
cualitativa (SERNAGEOMIN, remociones reales del sistema frontal 29-31 ene 2021
en Malloa/San Vicente de Tagua Tagua/Coinco — misma región, mismo tipo de
gatillo): los eventos reales se localizaron en sectores puntuales (Cuesta
Caracoles, La Vinilla, Tunca Arriba), no en decenas de km² — consistente con
que estos footprints, incluso los que pasan el guard de forma, puedan estar
sobredimensionados en escala. Por eso TODA corrida fuera de `atacama` lleva
`calibration_domain_warning=True` en `resumen.json`, independiente de
`physically_plausible` — son dos preguntas distintas (¿la forma es creíble? /
¿el tamaño tiene con qué validarse aquí?).

Uso:
    python3 runout_localidad.py --name santa_barbara --lon -72.021 --lat -37.667 \
        --domain nuble-biobio [--forecast ~/nowcast-forecast/latest] [--size-km 12]
Salidas en ~/nowcast-forecast/runouts/<name>/ (footprint, capas, resumen.json).
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

import numpy as np
import rasterio
from pyproj import Transformer

from susc_overlay import DOMAIN_BASINS, KINGSTON, raster_coverage_warning

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def point_value(path, lon, lat):
    with rasterio.open(path) as src:
        if src.crs and src.crs.to_epsg() != 4326:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            lon, lat = t.transform(lon, lat)
        return float(list(src.sample([(lon, lat)]))[0][0])


def pick_susceptibility(domain, lon, lat):
    """El raster de cuenca del dominio con DATO VÁLIDO en el punto, MÁS
    CERCANO al centro de su propio rectángulo — no el primero en la lista.

    No basta que los bounds contengan el punto: los rectángulos de cuencas
    vecinas se solapan y un punto del Maule cae dentro del rect de Rapel,
    donde el raster es nodata — sembraría el ABM con susceptibilidad 0 y la
    corrida vacía se leería como "seguro". Se muestrea el valor real. PERO
    eso no basta tampoco: si la cuenca vecina también tiene dato válido (no
    NaN) en el punto sin que el punto le pertenezca de verdad, "la primera
    con dato" elige por orden de lista, no por pertenencia real (hallazgo
    2026-07-17: Vallenar, en Huasco, elegía Copiapó/Ridge porque aparece
    antes en DOMAIN_BASINS — el footprint del ABM llevaba semanas corriendo
    con el modelo de otra cuenca). Sin polígono de cuenca real, la distancia
    al centro del rectángulo es la señal barata más razonable de
    pertenencia.
    """
    candidates = []
    for rel in DOMAIN_BASINS.get(domain, []):
        path = os.path.join(KINGSTON, rel)
        if not os.path.exists(path):
            continue
        with rasterio.open(path) as src:
            t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = t.transform(lon, lat)
            b = src.bounds
            if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                continue
            v = list(src.sample([(x, y)]))[0][0]
            if not np.isfinite(v):
                continue
            cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
            candidates.append((((x - cx) ** 2 + (y - cy) ** 2) ** 0.5, path))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--domain", required=True, choices=list(DOMAIN_BASINS))
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--size-km", type=float, default=12.0)
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--params", default="de",
                    choices=["de", "optuna", "18iters", "chanaral", "chanaral_enhanced"],
                    help="preset del ABM — 'de' (Copiapó, IoU 0.171) es el default historico; "
                         "'chanaral_enhanced' (IoU 0.555 medio/0.587 max out-of-sample) es la "
                         "mejor calibracion real disponible, mas apropiada para cuencas costeras "
                         "aridas de Atacama fuera de Copiapo mismo (2026-07-17).")
    ap.add_argument("--out-root", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--source", choices=["gfs", "ecmwf", "icon"], default="gfs",
                    help="qué pronóstico fuerza el ABM — usar 'icon' cuando el valle/orografía "
                         "queda sub-resuelto por GFS/ECMWF 0.25° (hallazgo Atacama 2026-07-15).")
    a = ap.parse_args()

    base_fdir = os.path.join(os.path.realpath(os.path.expanduser(a.forecast)), a.domain)
    fdir = base_fdir if a.source == "gfs" else os.path.join(base_fdir, a.source)
    if not os.path.isdir(fdir):
        sys.exit(f"no existe el pronóstico del dominio: {fdir}")

    # --- señal puntual del pronóstico -----------------------------------------
    signal = {}
    for key, fname in [("hazard_max_regional", "hazard_max_regional-a5.5.tif"),
                       ("first_crossing_fhour", "first_crossing_fhour_regional-a5.5.tif"),
                       ("rain_liquid_total_mm", "rain_liquid_total.tif")]:
        p = os.path.join(fdir, fname)
        signal[key] = round(point_value(p, a.lon, a.lat), 2) if os.path.exists(p) else None
    daily = defaultdict(float)
    import glob as _glob

    for f in sorted(_glob.glob(os.path.join(fdir, "steps", "step_*.tif"))):
        day = os.path.basename(f)[len("step_"):len("step_") + 8]
        daily[day] += point_value(f, a.lon, a.lat)
    days = sorted(daily)
    # peor tripleta consecutiva (el ABM cubre 72 h de patrones horarios)
    best_i, best_sum = 0, -1.0
    for i in range(max(1, len(days) - 2)):
        s = sum(daily[d] for d in days[i:i + 3])
        if s > best_sum:
            best_i, best_sum = i, s
    rain3 = [round(daily[d], 1) for d in days[best_i:best_i + 3]]
    if a.source != "gfs":
        # ECMWF/ICON ya entregan lluvia liquida; el ABM todavia necesita una
        # isoterma escalar para rotular el escenario. Se usa la media GFS del
        # mismo dominio/ciclo como aproximacion lenta, igual que en ECMWF.
        meta_name = "ecmwf.json" if a.source == "ecmwf" else "icon.json"
        meta = json.load(open(os.path.join(fdir, meta_name)))
        iso0 = json.load(open(os.path.join(base_fdir, "forecast.json")))["iso0_mean_m"]
    else:
        meta = json.load(open(os.path.join(fdir, "forecast.json")))
        iso0 = meta["iso0_mean_m"]

    susc = pick_susceptibility(a.domain, a.lon, a.lat)
    if susc is None:
        sys.exit(f"SIN COBERTURA de susceptibilidad para ({a.lon},{a.lat}) en "
                 f"{a.domain} — la corrida no sería informativa (sin semilla de "
                 f"terreno no hay flujo). No se corre.")
    cobertura = raster_coverage_warning(susc, a.lon, a.lat)

    out = os.path.join(a.out_root, a.name)
    os.makedirs(out, exist_ok=True)
    print(f"[{a.name}] ciclo {meta['cycle']} | gatillo pto {signal['hazard_max_regional']} "
          f"| cruce f+{signal['first_crossing_fhour']}h | lluvia líquida 120h "
          f"{signal['rain_liquid_total_mm']} mm | 3 días peores {rain3} (desde {days[best_i]}) "
          f"| iso0 {iso0:.0f} m")
    print(f"[{a.name}] susceptibilidad: {os.path.relpath(susc, KINGSTON)}")
    if cobertura["sospechoso"]:
        print(f"[{a.name}] AVISO QA cobertura de raster: {cobertura['nota']}")

    # Binario INSTALADO, nunca `cargo run` en caliente: nowcast-swarm depende
    # por path de swarm-abm, y un cambio allá (8ccad98 agregó Layers::trigger,
    # 21-oct-2026) rompió la compilación y con ella 40 runouts en dos ciclos del
    # pronóstico, igual que el borrado de target/ en sep-2026. Tras cada build:
    #   install -Dm755 target/release/examples/runout_ondemand ~/nowcast-monitor/bin/
    runner = os.environ.get("RUNOUT_BIN", os.path.expanduser("~/nowcast-monitor/bin/runout_ondemand"))
    if not os.path.isfile(runner):
        sys.exit(f"falta el runner instalado {runner}: cargo build --release -p nowcast-swarm "
                 f"--example runout_ondemand && install -Dm755 target/release/examples/runout_ondemand {runner}")
    cmd = [runner,
           f"--lon={a.lon}", f"--lat={a.lat}", "--size-km", str(a.size_km),
           "--rain-mm-per-day", ",".join(str(v) for v in rain3),
           "--isotherm-m", str(iso0), "--sediment", "0.5",
           "--susceptibility", susc, "--out", out,
           "--agents", str(a.agents), "--seed", str(a.seed), "--params", a.params]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        sys.exit(f"runner falló: {proc.stderr[-400:]}")

    with open(os.path.join(out, "stats.json")) as f:
        stats = json.load(f)

    # --- Plausibilidad física: criterios FIJADOS sobre datos de la ventana,
    # nunca leídos "a ojo" después de ver a qué localidad afectan (ver nota de
    # módulo). El ABM está calibrado en terreno abrupto (Copiapó 2015); sobre
    # fondo de valle plano no hay fuente empinada real que concentre el flujo
    # en un cauce, y el footprint se esparce difuso en vez de canalizarse.
    reasons = []
    relief_m = stats["dem"]["max_m"] - stats["dem"]["min_m"]
    r = stats["runout"]
    footprint_vacio = r["affected_km2"] <= 0 or r["mean_elev_footprint_m"] is None
    if footprint_vacio:
        # Footprint vacio (el ABM no movilizo nada con esta forzante) -- no hay
        # forma que evaluar, no es difuso ni canalizado, asi que NO se marca
        # implausible. Pero un footprint vacio con physically_plausible=true se
        # LEE como "plausible, solo que no alcanza al pueblo" (el label dice "NO
        # usar si plausible=false"): eso es enganoso en comunas costeras/de baja
        # pendiente (Valparaiso, Casablanca: relieve presente pero footprint
        # vacio) donde lo que pasa es que este modelo -- abanico aluvial abrupto
        # de Atacama -- no representa el modo de falla local. Se rotula explicito
        # con `footprint_vacio` para que el consumidor lo distinga de un
        # footprint real que no llega, y NO lo lea como evidencia de seguridad.
        # (En ensemble un miembro vacio sigue siendo un dato real, no un error.)
        pass
    elif relief_m < 150.0:
        reasons.append(
            f"relieve local insuficiente ({relief_m:.0f} m en la ventana de "
            f"{a.size_km:.0f} km) — sin frente montañoso real cerca del punto, "
            f"el ABM no tiene fuente empinada que alimentar"
        )
    else:
        elev_delta = r["mean_elev_window_rest_m"] - r["mean_elev_footprint_m"]
        delta_frac = elev_delta / relief_m
        if delta_frac < 0.08:
            reasons.append(
                f"footprint no se concentra cuesta abajo (Δelevación "
                f"{elev_delta:.0f} m = {delta_frac*100:.1f}% del relieve "
                f"disponible, {relief_m:.0f} m) — esparcimiento difuso sobre "
                f"{r['window_frac']*100:.0f}% de la ventana, no canalización"
            )
    plausible = len(reasons) == 0

    # Balance de agua/sedimento (informativo, NO gate pasa/no-pasa): techo
    # fisico independiente de los parametros internos del ABM. Volumen de agua
    # caida en la ventana (lluvia total x area) x un factor de "bulking"
    # generoso (3x, extremo alto de la literatura de debris flows) da el techo
    # de volumen agua+sedimento plausible; se compara contra el volumen que
    # IMPLICA el footprint asumiendo una profundidad promedio conservadora
    # (0.3 m, deliberadamente baja para no gatillar falsos positivos). Un
    # ratio <1 no prueba que el footprint sea correcto -- solo que no viola
    # conservacion de masa gruesa; ratio >1 si seria una imposibilidad fisica
    # real, independiente de si la forma pasa el guard de arriba.
    MASS_BULKING_FACTOR = 3.0
    MASS_MIN_DEPTH_M = 0.3
    rain_total_mm = sum(rain3)
    window_area_km2 = a.size_km ** 2
    water_vol_m3 = rain_total_mm / 1000.0 * window_area_km2 * 1e6
    mass_ceiling_m3 = MASS_BULKING_FACTOR * water_vol_m3
    implied_vol_m3 = r["affected_km2"] * 1e6 * MASS_MIN_DEPTH_M
    mass_balance_ratio = implied_vol_m3 / mass_ceiling_m3 if mass_ceiling_m3 > 0 else float("inf")
    if mass_balance_ratio > 1.0:
        reasons.append(
            f"footprint implica más volumen ({implied_vol_m3/1e6:.1f} Mm³ a "
            f"{MASS_MIN_DEPTH_M} m de profundidad) que el techo de balance de "
            f"agua/sedimento ({mass_ceiling_m3/1e6:.1f} Mm³, bulking "
            f"{MASS_BULKING_FACTOR}x sobre {water_vol_m3/1e6:.1f} Mm³ de lluvia) "
            f"— imposibilidad física de volumen, no solo de forma"
        )
        plausible = False

    # Dominio de calibracion: el preset 'de' solo tiene anclaje real (footprint
    # observado de Copiapo 2015, y flojo: IoU 0.171) dentro de atacama. Fuera de
    # ahi no hay evento real mapeado contra el cual validar la ESCALA del
    # footprint (independiente de si la forma pasa el guard de arriba).
    calib_domain_warning = a.domain != "atacama"

    # --- proximidad del footprint al centro del poblado ------------------------
    with rasterio.open(os.path.join(out, "runout_footprint.tif")) as src:
        fp = src.read(1)
        tr = src.transform
        t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x0, y0 = t.transform(a.lon, a.lat)
    rows, cols = np.where(fp == 1)
    prox = {"min_distance_m": None, "rings": {}}
    if rows.size:
        xs = tr.c + (cols + 0.5) * tr.a
        ys = tr.f + (rows + 0.5) * tr.e
        d = np.hypot(xs - x0, ys - y0)
        prox["min_distance_m"] = int(d.min())
        yy, xx = np.mgrid[0:fp.shape[0], 0:fp.shape[1]]
        dall = np.hypot(tr.c + (xx + 0.5) * tr.a - x0, tr.f + (yy + 0.5) * tr.e - y0)
        for r_km in (0.5, 1.0, 2.0):
            m = dall <= r_km * 1000
            prox["rings"][f"{r_km}km"] = round(float(fp[m].mean()), 3)
    PRESET_IOU = {
        "de": "0.171 (Copiapó, propio evento)",
        "optuna": "0.134 (Copiapó, propio evento)",
        "chanaral_enhanced": "0.555 medio / 0.587 máx out-of-sample (Chañaral)",
    }
    resumen = {"name": a.name, "lon": a.lon, "lat": a.lat, "domain": a.domain,
               "source": a.source, "cycle": meta["cycle"], "signal": signal,
               "rain3_mm_day": rain3, "rain3_start": days[best_i],
               "iso0_m": iso0, "susceptibility": susc,
               "params_preset": a.params,
               "params_preset_iou": PRESET_IOU.get(a.params, "sin IoU documentado"),
               "proximity": prox,
               "relief_m": relief_m,
               "window_frac": r["window_frac"],
               "mass_balance_ratio": round(mass_balance_ratio, 3),
               "mass_balance_note": (
                   f"{mass_balance_ratio*100:.0f}% del techo físico de agua/sedimento "
                   f"usado (bulking {MASS_BULKING_FACTOR}x, profundidad mín. "
                   f"{MASS_MIN_DEPTH_M} m) — informativo, no gate: <100% no confirma "
                   f"que el footprint sea correcto, solo que no viola conservación de "
                   f"masa gruesa"
               ),
               "physically_plausible": plausible,
               "implausibility_reasons": reasons,
               "footprint_vacio": footprint_vacio,
               "footprint_vacio_note": (
                   "el ABM de flujo detrítico no movilizó material en el punto con "
                   "esta forzante — SIN producto de footprint, NO evidencia de "
                   "seguridad. En comunas costeras/de baja pendiente suele indicar "
                   "que este modelo (calibrado en abanico aluvial abrupto de Atacama) "
                   "no representa el modo de falla local: interpretar como 'sin "
                   "modelo de footprint aplicable', no como 'sin peligro'."
               ) if footprint_vacio else None,
               "cobertura_raster_sospechosa": cobertura["sospechoso"],
               "cobertura_raster_nota": cobertura["nota"],
               "calibration_domain_warning": calib_domain_warning,
               "calibration_domain_note": (
                   "preset 'de' calibrado sobre Copiapó 2015 (IoU 0.171 en su propio "
                   "evento); sin evento real mapeado en esta región para validar la "
                   "ESCALA del footprint — referencia cualitativa SERNAGEOMIN (frontal "
                   "29-31 ene 2021, Malloa/San Vicente/Coinco): eventos reales "
                   "localizados a sectores puntuales, no a esta extensión"
               ) if calib_domain_warning else None,
               "label": "escenario ilustrativo — parámetros análogos Atacama, "
                        "sin calibrar en la cuenca; NO es aviso oficial. "
                        "NO usar si physically_plausible=false."}
    with open(os.path.join(out, "resumen.json"), "w") as f:
        json.dump(resumen, f, indent=1, ensure_ascii=False)
    rings = " | ".join(f"≤{k}: {v*100:.0f}%" for k, v in prox["rings"].items())
    print(f"[{a.name}] footprint vs pueblo: distancia mín "
          f"{prox['min_distance_m']} m | {rings}")
    if footprint_vacio:
        veredicto = "SIN FOOTPRINT — el ABM no movilizó material (no leer como seguro; probable modelo no aplicable en costa/plano)"
    elif plausible:
        veredicto = "PLAUSIBLE"
    else:
        veredicto = "NO PLAUSIBLE: " + "; ".join(reasons)
    print(f"[{a.name}] {veredicto}")
    if calib_domain_warning:
        print(f"[{a.name}] AVISO dominio de calibración: sin evento real mapeado "
              f"fuera de Atacama para validar la escala del footprint")
    print(f"[{a.name}] listo → {out}")


if __name__ == "__main__":
    main()
