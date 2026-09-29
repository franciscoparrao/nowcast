#!/usr/bin/env python3
"""Paquete web de un ciclo de pronóstico: lo que consume el visor (etapa 1).

El visor web (Svelte + WASM, mismo stack que el de SurtGIS) es ESTÁTICO: lee
archivos, no llama a un servidor. Este script define ese contrato. Por ciclo
escribe `~/nowcast-forecast/web/<ciclo>/`:

    manifest.json                  ciclo, ventana, modelos por dominio, hora de
                                   cada paso, escala, atribuciones, aviso
    <dom>/hazard_<modelo>.tif      COG multibanda: una banda por paso (GFS/ICON
                                   horario, ECMWF cada 3 h), uint8
    <dom>/hazard_max_<modelo>.tif  máximo de la ventana, uint8
    <dom>/hazard_max_peor.tif      el peor de los modelos presentes, uint8
    <dom>/desacuerdo.tif           máx − mín entre modelos (≥2 modelos), uint8
    <dom>/lluvia_<modelo>.tif      lluvia líquida acumulada 120 h, float32 mm
    comunas.geojson                cruce comunal, geometría simplificada para web
    localidades.geojson            watchlist del ciclo (puntos)

Codificación del peligro: uint8 = round(prob × 250); 255 = sin dato. Error de
cuantización ≤ 0.002, ~4× más liviano que float32. El manifest lo declara
(`escala`) para que el visor no tenga que adivinarlo.

`web/latest` apunta al último ciclo y `web/ciclos.json` lista los disponibles.
Se conservan los últimos `--keep` (default 12, ~3 días); el resto se borra
(regenerable desde el ciclo mientras la poda no se lleve sus pasos).

Privado por ahora (decisión 2026-09-29): nada se publica; el visor lo lee local.

Uso:
    python3 web_package.py [--forecast ~/nowcast-forecast/latest] [--keep 12]
"""

import argparse
import glob
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
import shapely
import geopandas as gpd

VARIANTE = "regional-a5.5"
ESCALA = 250.0          # uint8 = round(prob × ESCALA)
NODATA_U8 = 255
SIMPLIFY_WEB_DEG = 0.005   # ~550 m: sobra para un coroplético comunal en pantalla
ESQUEMA = 1

DOMINIOS = {
    "atacama": "Atacama", "coquimbo": "Coquimbo", "rm": "Región Metropolitana",
    "nuble-biobio": "Ñuble – Biobío", "araucania": "Araucanía",
    "chile-centro-sur": "Barrido macro (Coquimbo → Los Ríos)",
}
# (subcarpeta dentro del dominio, meta json)
MODELOS = {"gfs": ("", "forecast.json"), "ecmwf": ("ecmwf", "ecmwf.json"),
           "icon": ("icon", "icon.json")}
ATRIBUCION = {
    "gfs": "NOAA/NCEP GFS 0.25° (dominio público)",
    "ecmwf": "ECMWF Open Data, IFS HRES 0.25° — CC BY 4.0, © ECMWF",
    "icon": "DWD ICON global — Open Data, CC BY 4.0, © Deutscher Wetterdienst",
    "comunas": "Límite comunal y población: Censo 2024, INE",
}
AVISO = ("Sistema experimental en modo sombra, sin validación operacional; umbral "
         "lluvia–duración regional calibrado en el Maipo. No es un aviso oficial: las "
         "fuentes de verdad son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.")


def _json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _t(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def pasos_modelo(ddir, modelo, meta):
    """[(hora UTC, ruta)] de los pasos de peligro del modelo, en orden."""
    carpeta = os.path.join(ddir, MODELOS[modelo][0], f"hazard_{VARIANTE}")
    tifs = sorted(glob.glob(os.path.join(carpeta, "hazard_*.tif")))
    if not tifs:
        return []
    # GFS/ICON nombran por hora (hazard_YYYYMMDDTHHMM); ECMWF por índice
    # (hazard_0000) y su hora sale de la ventana: desde + 3 h × i.
    m = re.search(r"hazard_(\d{8}T\d{4})\.tif$", tifs[0])
    if m:
        return [(_t(re.search(r"(\d{8}T\d{4})", p).group(1)), p) for p in tifs]
    v = (meta or {}).get("ventana") or {}
    if not v.get("desde") or not v.get("n_pasos"):
        raise ValueError(f"{modelo}: pasos por índice sin ventana en su meta")
    paso_h = (v.get("horas") or 0) / v["n_pasos"]
    t0 = _t(v["desde"])
    return [(t0 + timedelta(hours=paso_h * i), p) for i, p in enumerate(tifs)]


def a_u8(prob):
    u = np.full(prob.shape, NODATA_U8, np.uint8)
    ok = np.isfinite(prob)
    u[ok] = np.clip(np.round(prob[ok] * ESCALA), 0, 250).astype(np.uint8)
    return u


def escribir_cog(path, bandas, perfil, dtype, nodata, descripciones=None):
    """COG con INTERLEAVE=BAND: el visor lee UNA banda (un paso) por pedido."""
    tmp = path + ".tmp.tif"
    p = dict(perfil, driver="COG", count=len(bandas), dtype=dtype, nodata=nodata,
             compress="DEFLATE", interleave="BAND")
    p.pop("blockxsize", None), p.pop("blockysize", None), p.pop("tiled", None)
    with rasterio.open(tmp, "w", **p) as dst:
        for i, b in enumerate(bandas, 1):
            dst.write(b.astype(dtype), i)
            if descripciones:
                dst.set_band_description(i, descripciones[i - 1])
    os.replace(tmp, path)


def paquete_dominio(ddir, out, dom):
    """Escribe los rásters de un dominio; devuelve su entrada del manifest."""
    os.makedirs(out, exist_ok=True)
    entrada = {"nombre": DOMINIOS[dom], "modelos": {}}
    maximos, perfil, forma = {}, None, None
    for modelo, (sub, meta_name) in MODELOS.items():
        meta = _json(os.path.join(ddir, sub, meta_name))
        if meta is None:
            continue
        pasos = pasos_modelo(ddir, modelo, meta)
        if not pasos:
            continue
        bandas = []
        for _, p in pasos:
            with rasterio.open(p) as src:
                if perfil is None:
                    perfil = {k: src.profile[k] for k in ("width", "height", "crs", "transform")}
                    forma = (src.height, src.width)
                    entrada["grilla"] = {
                        "ancho": src.width, "alto": src.height, "crs": str(src.crs),
                        "bbox": [round(x, 4) for x in src.bounds],
                        "resolucion_deg": round(abs(src.transform.a), 4)}
                if (src.height, src.width) != forma:
                    raise ValueError(f"{dom}/{modelo}: grilla {src.height}×{src.width} "
                                     f"≠ {forma[0]}×{forma[1]} (los modelos deben compartir grilla)")
                bandas.append(src.read(1).astype("float64"))
        horas = [t.strftime("%Y-%m-%dT%H:%MZ") for t, _ in pasos]
        escribir_cog(os.path.join(out, f"hazard_{modelo}.tif"), [a_u8(b) for b in bandas],
                     perfil, "uint8", NODATA_U8, horas)
        mx = np.fmax.reduce(bandas)
        maximos[modelo] = mx
        escribir_cog(os.path.join(out, f"hazard_max_{modelo}.tif"), [a_u8(mx)],
                     perfil, "uint8", NODATA_U8)
        lluvia = os.path.join(ddir, sub, "rain_liquid_total.tif")
        if os.path.exists(lluvia):
            with rasterio.open(lluvia) as src:
                escribir_cog(os.path.join(out, f"lluvia_{modelo}.tif"),
                             [src.read(1).astype("float32")], perfil, "float32", float("nan"))
        v = (meta.get("variants") or {}).get(VARIANTE) or {}
        entrada["modelos"][modelo] = {
            "ciclo": meta.get("cycle"), "pasos": horas,
            "paso_horas": round((pasos[1][0] - pasos[0][0]).total_seconds() / 3600, 3)
            if len(pasos) > 1 else None,
            "pico": v.get("peak_prob"), "primer_cruce": v.get("first_crossing"),
            "lluvia": os.path.exists(lluvia),
        }
    if not maximos:
        return None
    pila = np.stack(list(maximos.values()))
    escribir_cog(os.path.join(out, "hazard_max_peor.tif"), [a_u8(np.fmax.reduce(pila))],
                 perfil, "uint8", NODATA_U8)
    if len(maximos) >= 2:
        # nanmax − nanmin sin warnings en celdas todo-NaN
        with np.errstate(all="ignore"):
            d = np.where(np.isfinite(pila).sum(0) >= 2,
                         np.nanmax(pila, 0) - np.nanmin(pila, 0), np.nan)
        escribir_cog(os.path.join(out, "desacuerdo.tif"), [a_u8(d)], perfil, "uint8", NODATA_U8)
    entrada["desacuerdo"] = len(maximos) >= 2
    return entrada


def comunas_web(fdir, out):
    g = os.path.join(fdir, "comuna_impact", "comuna_impact.geojson")
    if not os.path.exists(g):
        return None
    gdf = gpd.read_file(g)
    for c in gdf.select_dtypes(include="string").columns:
        gdf[c] = gdf[c].astype(object)
    gdf["geometry"] = shapely.coverage_simplify(gdf.geometry.values, SIMPLIFY_WEB_DEG)
    keep = [c for c in ("CUT", "COMUNA", "REGION", "PROVINCIA", "poblacion", "peak_gfs",
                        "peak_ecmwf", "peak_icon", "peak_terreno", "peak_worst", "nivel")
            if c in gdf.columns]
    p = os.path.join(out, "comunas.geojson")
    gdf[keep + ["geometry"]].to_file(p + ".tmp", driver="GeoJSON", COORDINATE_PRECISION=5)
    os.replace(p + ".tmp", p)
    return len(gdf)


def localidades_web(fdir, out):
    w = _json(os.path.join(fdir, "watchlist.json"))
    if not w:
        return None
    campos = ("name", "label", "comuna", "region", "domain", "peak_terreno", "peak_gfs",
              "peak_ecmwf", "peak_icon", "poblacion_comuna", "poblacion_urbana",
              "borderline", "en_lista")
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
              "properties": {k: s.get(k) for k in campos}}
             for s in w.get("selected") or [] if s.get("lon") is not None]
    p = os.path.join(out, "localidades.geojson")
    with open(p + ".tmp", "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)
    return len(feats)


def construir(fdir, web_dir):
    ciclo = os.path.basename(os.path.realpath(fdir))
    out = os.path.join(web_dir, ciclo)
    tmp_out = out + ".tmp"
    shutil.rmtree(tmp_out, ignore_errors=True)
    os.makedirs(tmp_out)
    dominios = {}
    for dom in DOMINIOS:
        ddir = os.path.join(fdir, dom)
        if os.path.isdir(ddir):
            e = paquete_dominio(ddir, os.path.join(tmp_out, dom), dom)
            if e:
                dominios[dom] = e
    if not dominios:
        shutil.rmtree(tmp_out)
        raise SystemExit(f"[web] {ciclo}: ningún dominio con pasos de peligro")
    n_com = comunas_web(fdir, tmp_out)
    n_loc = localidades_web(fdir, tmp_out)
    presentes = sorted({m for e in dominios.values() for m in e["modelos"]})
    manifest = {
        "esquema": ESQUEMA,
        "ciclo": ciclo,
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "variante": VARIANTE,
        "nivel_alerta": 0.5,
        "escala": {"hazard": {"dtype": "uint8", "divisor": ESCALA, "nodata": NODATA_U8,
                              "nota": "prob = valor / divisor; 255 = sin dato"},
                   "lluvia": {"dtype": "float32", "unidad": "mm", "nodata": "NaN"}},
        "modelos_presentes": presentes,
        "modelos_ausentes": [m for m in MODELOS if m not in presentes],
        "dominios": dominios,
        "vectores": {"comunas": n_com, "localidades": n_loc},
        "atribucion": {k: v for k, v in ATRIBUCION.items() if k in presentes or k == "comunas"},
        "aviso": AVISO,
    }
    with open(os.path.join(tmp_out, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    # swap atómico del directorio del ciclo: el visor nunca ve un paquete a medias
    viejo = out + ".old"
    shutil.rmtree(viejo, ignore_errors=True)
    if os.path.exists(out):
        os.replace(out, viejo)
    os.replace(tmp_out, out)
    shutil.rmtree(viejo, ignore_errors=True)
    return ciclo, manifest


def indexar(web_dir, keep):
    """Retención + web/latest + web/ciclos.json."""
    ciclos = sorted((d for d in os.listdir(web_dir)
                     if re.fullmatch(r"\d{8}T\d{2}z", d) and os.path.isdir(os.path.join(web_dir, d))),
                    reverse=True)
    for viejo in ciclos[keep:]:
        shutil.rmtree(os.path.join(web_dir, viejo), ignore_errors=True)
    ciclos = ciclos[:keep]
    lista = []
    for c in ciclos:
        m = _json(os.path.join(web_dir, c, "manifest.json")) or {}
        picos = {d: {mo: v.get("pico") for mo, v in e.get("modelos", {}).items()}
                 for d, e in (m.get("dominios") or {}).items()}
        lista.append({"ciclo": c, "modelos": m.get("modelos_presentes"), "picos": picos})
    with open(os.path.join(web_dir, "ciclos.json.tmp"), "w") as f:
        json.dump({"ciclos": lista}, f, ensure_ascii=False)
    os.replace(os.path.join(web_dir, "ciclos.json.tmp"), os.path.join(web_dir, "ciclos.json"))
    if ciclos:
        link = os.path.join(web_dir, "latest")
        tmp = link + ".tmp"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(ciclos[0], tmp)
        os.replace(tmp, link)
    return len(ciclos)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--web", default=os.path.expanduser("~/nowcast-forecast/web"))
    ap.add_argument("--keep", type=int, default=12)
    a = ap.parse_args()
    os.makedirs(a.web, exist_ok=True)
    ciclo, m = construir(os.path.expanduser(a.forecast), a.web)
    n = indexar(a.web, a.keep)
    tam = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(os.path.join(a.web, ciclo))
              for f in fs)
    print(f"[web] {ciclo}: {len(m['dominios'])} dominios, modelos {m['modelos_presentes']}"
          f"{' (sin ' + ', '.join(m['modelos_ausentes']) + ')' if m['modelos_ausentes'] else ''}, "
          f"{tam / 2**20:.1f} MB · {n} ciclos en {a.web}")


if __name__ == "__main__":
    main()
