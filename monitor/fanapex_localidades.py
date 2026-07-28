#!/usr/bin/env python3
"""Persiste la clase FAN-APEX de cada comuna dentro de su `resumen.json`.

Cierra el hueco de docs/representacion-area-fanapex.md: la clasificación por
fan-apex (`fan_apex_localidad.py`) existía solo en tablas INTERACTIVAS
(`melton_localidad.py --watchlist --fan-apex`, `sweep_fanapex_watchlist.py`),
así que el registro del ciclo y el export GIS seguían viendo únicamente la
clase por centroide —la que llama "crecida" a Alto del Carmen— o ninguna. Este
script la escribe como un bloque `fan_apex` en el `resumen.json` de cada
localidad, que es la estructura que YA leen `report/build_registro.py`,
`export_gis.py` y `basin_analysis_*.py`. Nadie río abajo cambia de fuente: el
dato aparece donde ya estaban mirando.

POR QUÉ UN PASO APARTE (y no dentro de `runout_localidad.py`, que es quien
escribe el resumen): (a) la morfometría es ESTÁTICA —no cambia por ciclo— y se
cachea entre ciclos igual que `melton_cache.json`, mientras que el runout se
recorre entero cada ciclo; (b) el costo real no es el detector sino leer las
capas censales del país, que en un solo proceso se leen UNA vez para las ~40
comunas y en 40 procesos se leerían 40 veces; (c) `runout_localidad.py` ni
siquiera recibe `--comuna` (trabaja por punto), y el fan-apex es por comuna.

BLANDA por diseño, como la etapa que la precede: una comuna que falle (sin
hidrología, sin polígono censal, surtgis caído) queda con `disponible=false` y
su motivo, y el barrido sigue. El bloque nunca miente por omisión: la ausencia
de fan-apex se distingue de "no se pudo calcular".

ROTULADO: es DIAGNÓSTICO, no rótulo operacional. Deshace un sesgo documentado
del centroide (ver HALLAZGO-2 en melton_localidad.py) y su especificidad está
controlada (los pueblos de valle plano NO reciben abanico inventado), pero está
validado sobre pocos casos y con proxy D8; la cobertura física por ABM
(`scripts/abm_apex_footprint.py`) es la profundización per-comuna, demasiado
cara para el barrido.

Uso (normalmente lo invoca analysis_all.sh, después de run_localidades.py):
    python3 fanapex_localidades.py [--forecast ~/nowcast-forecast/latest]
        [--hydro-source surtgis|kingston] [--forzar] [--solo <name>]
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from melton_localidad import CACHE_PATH as MELTON_CACHE_PATH          # noqa: E402
from melton_localidad import _hydro_dir, _load_cache, classify_point  # noqa: E402
import fan_apex_localidad as FA                                       # noqa: E402

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")
CACHE_PATH = os.path.expanduser("~/nowcast-forecast/fanapex_cache.json")
# Sube cuando cambie el ALGORITMO (no los parámetros, que ya van en la clave):
# invalida el cache sin borrarlo a mano.
CACHE_VERSION = 1

# Umbral de runout bajo el cual se considera que el fan-apex CORRIGE al
# centroide con un alcance creíble — mismo criterio que sweep_fanapex_watchlist.
RUNOUT_CREIBLE_KM = 8.0

NOTA = (
    "clasificación por FAN-APEX: la comuna se representa como el conjunto de "
    "abanicos (quebradas empinadas) cuya ruta de flujo alcanza su área poblada, "
    "no como un centroide enganchado al cauce más cercano. La clase es la del "
    "apex que alcanza a MENOR runout, y `runout_a_alcanzar_km` es la distancia a "
    "la que ese flujo llega al poblado (no un cap elegido a mano). DIAGNÓSTICO: "
    "deshace el sesgo a 'crecida' del centroide (HALLAZGO-2 en "
    "melton_localidad.py), validado sobre pocos casos y con proxy D8 — la "
    "cobertura física por ABM (scripts/abm_apex_footprint.py) es la "
    "profundización per-comuna. NO es rótulo operacional ni aviso oficial."
)


def cache_key(comuna, basin, lon, lat, params):
    """Clave estable de la morfometría (estática entre ciclos). Incluye los
    parámetros: cambiar un umbral debe recalcular, no reusar en silencio."""
    return "|".join([
        f"v{CACHE_VERSION}", basin, comuna.upper(),
        f"{round(lon, 4)}", f"{round(lat, 4)}",
        f"w{params['window_km']}", f"b{params['fan_buffer_m']}",
        f"s{params['search_cap_m']}", f"m{params['melton_min']}",
        params["hydro_source"],
    ])


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(open(CACHE_PATH))
        except (OSError, ValueError):
            return {}
    return {}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        json.dump(cache, open(CACHE_PATH, "w"), indent=1, ensure_ascii=False)
    except OSError:
        pass


def bloque_no_disponible(comuna, basin, motivo, centroide=None):
    b = {"disponible": False, "comuna": comuna, "basin": basin, "motivo": motivo,
         "clase": None, "runout_a_alcanzar_km": None, "banda": None, "nota": NOTA}
    if centroide:
        b["centroide_clase"] = centroide.get("clase")
        b["centroide_melton"] = centroide.get("melton")
    return b


def bloque(fa, centroide, params):
    """Bloque PLANO (sin listas anidadas) para el resumen.json: `export_gis.py`
    aplana el resumen a columnas de QGIS y una lista de dicts saldría como un
    string ilegible. El detalle por apex vive en el cache y en el sweep; aquí va
    lo que el reporte y el GIS necesitan, más el apex que manda."""
    ro = fa.get("runout_a_alcanzar_km")
    apexes = fa.get("apex_que_alcanzan") or []
    cerca = apexes[0] if apexes else {}
    cen_clase = (centroide or {}).get("clase")
    # "cambia" = el fan-apex dice algo distinto del centroide. Se guarda la
    # comparación cruda (ambas clases quedan en el bloque) y además la lectura
    # en una sola bandera, para no obligar a cada consumidor a re-derivarla.
    fa_base = (fa.get("clase_comuna") or "").split(" /")[0]
    cambia = bool(cen_clase and fa_base and cen_clase != fa_base)
    return {
        "disponible": True,
        "comuna": fa.get("comuna"),
        "basin": fa.get("basin"),
        "clase": fa.get("clase_comuna"),
        "runout_a_alcanzar_km": ro,
        "banda": fa.get("banda"),
        "n_apex_detectados": fa.get("n_apex_detectados"),
        "n_apex_alcanzan": fa.get("n_apex_alcanzan"),
        "target_kind": fa.get("target_kind"),
        "hydro_source": fa.get("hydro_source"),
        "apex_cercano_melton": cerca.get("melton"),
        "apex_cercano_area_km2": cerca.get("area_km2"),
        "apex_cercano_clase": cerca.get("clase"),
        "apex_cercano_lon": cerca.get("lon"),
        "apex_cercano_lat": cerca.get("lat"),
        "centroide_clase": cen_clase,
        "centroide_melton": (centroide or {}).get("melton"),
        "cambia_vs_centroide": cambia,
        "corrige_a_aluvion": bool(
            cambia and cen_clase == "crecida fluvial"
            and ro is not None and ro <= RUNOUT_CREIBLE_KM),
        "melton_min": params["melton_min"],
        "nota": NOTA,
    }


def enriquecer(name, comuna, lon, lat, runouts, cache, mel_cache, params, forzar=False):
    """Devuelve (bloque, hit_cache). No escribe nada: el llamador decide."""
    rp = os.path.join(runouts, name, "resumen.json")
    if not os.path.exists(rp):
        return None, False
    try:
        res = json.load(open(rp))
    except (OSError, ValueError) as e:
        return bloque_no_disponible(comuna, None, f"resumen.json ilegible: {e}"), False
    susc = res.get("susceptibility")
    if not susc:
        return bloque_no_disponible(
            comuna, None, "la corrida no eligió raster de susceptibilidad "
                          "(punto fuera de las cuencas modeladas)"), False

    basin, hydro = _hydro_dir(susc)
    centroide = classify_point(susc, lon, lat, cache=mel_cache)
    if hydro is None:
        return bloque_no_disponible(
            comuna, basin,
            f"cuenca {basin} sin hidrología propia (transfer) — sin detector de "
            f"fan-apex aplicable; es el hueco 'costa sin modelo' ya documentado",
            centroide=centroide), False
    if not comuna:
        return bloque_no_disponible(
            comuna, basin, "la watchlist no trae comuna para esta localidad "
                           "(el objetivo poblado se busca por nombre de comuna)",
            centroide=centroide), False

    key = cache_key(comuna, basin, lon, lat, params)
    if not forzar and key in cache:
        return bloque(cache[key], centroide, params), True

    try:
        fa = FA.classify(comuna, basin, lon, lat, **params)
    except (Exception, SystemExit) as e:
        # SystemExit va EXPLÍCITO además de Exception: no deriva de Exception, y
        # una función de librería río abajo que llame sys.exit() (el patrón que
        # se corrigió en detect_fan_apex.run_hydrology) mataría el barrido
        # entero en vez de degradar una comuna. En una etapa blanda el ítem
        # falla, el lote sigue. Cubierto por --selftest.
        return bloque_no_disponible(
            comuna, basin, f"fallo del detector: {type(e).__name__}: {str(e)[:200]}",
            centroide=centroide), False
    if "error" in fa:
        return bloque_no_disponible(comuna, basin, fa["error"], centroide=centroide), False

    cache[key] = fa
    return bloque(fa, centroide, params), False


def selftest():
    """Autotest sin red ni KINGSTON: entradas HOSTILES al enriquecedor.

    Cubre el barrido de hermanas del fix "sys.exit dentro de una librería"
    (detect_fan_apex.run_hydrology / footprint_mask): lo que se prueba no es que
    el detector acierte, es que NINGUNA forma de fallo de una comuna tumbe el
    lote ni escriba un bloque que mienta por omisión."""
    import tempfile

    fallos = []

    def check(nombre, cond, detalle=""):
        print(f"  {'ok  ' if cond else 'FALLA'} {nombre}" + (f" — {detalle}" if detalle else ""))
        if not cond:
            fallos.append(nombre)

    params = {"window_km": 26.0, "fan_buffer_m": 400.0, "search_cap_m": 20000.0,
              "melton_min": 0.3, "hydro_source": "surtgis"}
    work = tempfile.mkdtemp(prefix="fanapex_selftest_")
    rd = os.path.join(work, "pueblo_x")
    os.makedirs(rd)
    susc = "/no/existe/09_rio_maipo/susc.tif"
    json.dump({"name": "pueblo_x", "susceptibility": susc},
              open(os.path.join(rd, "resumen.json"), "w"))

    orig_classify, orig_hydro, orig_melton = FA.classify, _hydro_dir, classify_point
    g = globals()
    g["_hydro_dir"] = lambda p: ("09_rio_maipo", "/fake/hydrology")
    g["classify_point"] = lambda *a, **k: {"clase": "crecida fluvial", "melton": 0.02}
    try:
        # 1. el detector levanta SystemExit (el patrón viejo: sys.exit en librería)
        g_fa = FA
        def _boom_exit(*a, **k):
            raise SystemExit("surtgis hydrology falló")
        g_fa.classify = _boom_exit
        b, _ = enriquecer("pueblo_x", "COMUNA X", -70.0, -33.0, work, {}, {}, params)
        check("SystemExit del detector NO tumba el lote",
              b is not None and b["disponible"] is False and "SystemExit" in b["motivo"])

        # 2. excepción común (rasterio/geopandas revientan)
        def _boom(*a, **k):
            raise RuntimeError("surtgis hydrology falló: exit 127")
        g_fa.classify = _boom
        b, _ = enriquecer("pueblo_x", "COMUNA X", -70.0, -33.0, work, {}, {}, params)
        check("RuntimeError del detector degrada la comuna",
              b["disponible"] is False and "RuntimeError" in b["motivo"])
        check("el bloque caído conserva la clase por centroide",
              b.get("centroide_clase") == "crecida fluvial")
        check("el bloque caído NO afirma una clase fan-apex", b["clase"] is None)

        # 3. el detector devuelve {'error': ...} (comuna/cuenca mal pareadas)
        g_fa.classify = lambda *a, **k: {"error": "objetivo poblado sin celdas en la ventana"}
        b, _ = enriquecer("pueblo_x", "COMUNA X", -70.0, -33.0, work, {}, {}, params)
        check("error del detector se propaga como motivo, no como clase",
              b["disponible"] is False and "sin celdas" in b["motivo"])

        # 4. cuenca transfer: sin hidrología no se inventa clase
        g["_hydro_dir"] = lambda p: ("17_costa_transfer", None)
        b, _ = enriquecer("pueblo_x", "COMUNA X", -70.0, -33.0, work, {}, {}, params)
        check("cuenca transfer queda no-disponible (no 'crecida' por defecto)",
              b["disponible"] is False and "transfer" in b["motivo"] and b["clase"] is None)
        g["_hydro_dir"] = lambda p: ("09_rio_maipo", "/fake/hydrology")

        # 5. localidad sin runout todavía → None (el lote la salta, no escribe)
        b, _ = enriquecer("no_existe", "COMUNA X", -70.0, -33.0, work, {}, {}, params)
        check("localidad sin resumen.json devuelve None (no se inventa archivo)", b is None)

        # 6. el cache NO se reusa si cambian los parámetros
        k1 = cache_key("COMUNA X", "09_rio_maipo", -70.0, -33.0, params)
        p2 = dict(params, melton_min=0.6)
        k2 = cache_key("COMUNA X", "09_rio_maipo", -70.0, -33.0, p2)
        check("cambiar melton_min invalida la clave de cache", k1 != k2)
        p3 = dict(params, hydro_source="kingston")
        check("cambiar la fuente de hidrología invalida la clave",
              k1 != cache_key("COMUNA X", "09_rio_maipo", -70.0, -33.0, p3))
        check("la clave es estable ante el mismo input",
              k1 == cache_key("comuna x", "09_rio_maipo", -70.00001, -33.0, params))

        # 7. bloque() con un fan-apex que NO alcanza: sin apex cercano inventado
        fa_vacio = {"comuna": "COMUNA X", "basin": "09_rio_maipo",
                    "clase_comuna": "crecida fluvial / sin fan-apex que alcance",
                    "runout_a_alcanzar_km": None, "banda": "no alcanza",
                    "n_apex_detectados": 3, "n_apex_alcanzan": 0,
                    "apex_que_alcanzan": []}
        b = bloque(fa_vacio, {"clase": "crecida fluvial", "melton": 0.02}, params)
        check("sin apex que alcance no hay apex_cercano ni corrección",
              b["apex_cercano_melton"] is None and b["corrige_a_aluvion"] is False
              and b["cambia_vs_centroide"] is False)

        # 8. runout largo: alcanza, pero NO cuenta como corrección creíble
        fa_lejos = dict(fa_vacio, clase_comuna="debris flood",
                        runout_a_alcanzar_km=14.4, banda="solo por flujo excepcional",
                        n_apex_alcanzan=1,
                        apex_que_alcanzan=[{"melton": 0.42, "area_km2": 2.0,
                                            "clase": "debris flood", "lon": -70.0,
                                            "lat": -33.0}])
        b = bloque(fa_lejos, {"clase": "crecida fluvial", "melton": 0.02}, params)
        check(f"runout de 14.4 km (> {RUNOUT_CREIBLE_KM:.0f}) no se rotula como corrección",
              b["cambia_vs_centroide"] is True and b["corrige_a_aluvion"] is False)
    finally:
        FA.classify = orig_classify
        g["_hydro_dir"] = orig_hydro
        g["classify_point"] = orig_melton
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n[selftest] {'TODO OK' if not fallos else f'{len(fallos)} FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=RUNOUTS)
    ap.add_argument("--hydro-source", choices=["surtgis", "kingston"], default="surtgis",
                    help="surtgis: recomputa la hidrología por ventana (consistente "
                         "con la validación del detector); kingston: la precomputada")
    ap.add_argument("--window-km", type=float, default=FA.WINDOW_KM)
    ap.add_argument("--fan-buffer-m", type=float, default=FA.FAN_BUFFER_M)
    ap.add_argument("--search-cap-m", type=float, default=FA.SEARCH_CAP_M)
    ap.add_argument("--melton-min", type=float, default=0.3)
    ap.add_argument("--forzar", action="store_true",
                    help="recalcular ignorando el cache de morfometría")
    ap.add_argument("--solo", help="enriquecer solo esta localidad (por name)")
    ap.add_argument("--selftest", action="store_true",
                    help="autotest de entradas hostiles, sin red ni KINGSTON")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())

    runouts = os.path.expanduser(a.runouts)
    forecast = os.path.realpath(os.path.expanduser(a.forecast))
    wl_p = os.path.join(forecast, "watchlist.json")
    if not os.path.exists(wl_p):
        sys.exit(f"[fanapex] falta {wl_p} — ¿corrió select_watchlist?")
    sel = json.load(open(wl_p)).get("selected", [])
    if not sel:
        sys.exit("[fanapex] watchlist sin localidades seleccionadas")
    if a.solo:
        sel = [s for s in sel if s.get("name") == a.solo]
        if not sel:
            sys.exit(f"[fanapex] '{a.solo}' no está en la watchlist vigente")

    params = {"window_km": a.window_km, "fan_buffer_m": a.fan_buffer_m,
              "search_cap_m": a.search_cap_m, "melton_min": a.melton_min,
              "hydro_source": a.hydro_source}

    cache = load_cache()
    # el cache de Melton por centroide se carga UNA vez y se pasa al lote: con
    # cache=None, classify_point lee y reescribe el archivo por cada comuna.
    mel_cache = _load_cache()

    print(f"[fanapex] ciclo {os.path.basename(forecast)} · {len(sel)} localidades · "
          f"hidrología {a.hydro_source}")
    escritos = sin_runout = no_disp = hits = corrigen = 0
    fallos = []
    for s in sorted(sel, key=lambda x: x.get("comuna") or ""):
        name, comuna = s.get("name"), s.get("comuna")
        lon, lat = s.get("lon"), s.get("lat")
        if not (name and lon is not None and lat is not None):
            continue
        b, hit = enriquecer(name, comuna, lon, lat, runouts, cache, mel_cache,
                            params, forzar=a.forzar)
        if b is None:
            sin_runout += 1
            continue
        hits += int(hit)

        rp = os.path.join(runouts, name, "resumen.json")
        try:
            res = json.load(open(rp))
            res["fan_apex"] = b
            tmp = rp + ".tmp"
            with open(tmp, "w") as f:
                json.dump(res, f, indent=1, ensure_ascii=False)
            os.replace(tmp, rp)   # atómico: nunca dejar un resumen.json truncado
            escritos += 1
        except (OSError, ValueError) as e:
            fallos.append(f"{name} ({e})")
            continue

        if not b["disponible"]:
            no_disp += 1
            print(f"  {(comuna or name)[:24]:26s} sin fan-apex — {b['motivo'][:70]}")
            continue
        corrigen += int(b["corrige_a_aluvion"])
        ro = b["runout_a_alcanzar_km"]
        flecha = "  ← CORRIGE" if b["corrige_a_aluvion"] else ""
        print(f"  {(comuna or name)[:24]:26s} {str(b['centroide_clase'])[:15]:16s} → "
              f"{b['clase'][:22]:24s} {(f'{ro:.1f} km' if ro is not None else '—'):>8s} "
              f"{b['banda'] or ''}{flecha}")

    save_cache(cache)
    try:
        os.makedirs(os.path.dirname(MELTON_CACHE_PATH), exist_ok=True)
        json.dump(mel_cache, open(MELTON_CACHE_PATH, "w"), indent=1, ensure_ascii=False)
    except OSError:
        pass
    print(f"[fanapex] fin: {escritos} resumen.json enriquecidos · {hits} desde cache · "
          f"{no_disp} sin fan-apex aplicable · {corrigen} corrigen el centroide a aluvión · "
          f"{sin_runout} sin runout todavía"
          + (f" · {len(fallos)} fallos de escritura: {fallos}" if fallos else ""))


if __name__ == "__main__":
    main()
