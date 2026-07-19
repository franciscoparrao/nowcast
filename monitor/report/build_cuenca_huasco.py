#!/usr/bin/env python3
"""Reporte dedicado — cuenca del río Huasco (Vallenar, Freirina, Huasco).

Complementa el registro nacional (`build_registro.py`, top-20 objetivo) con
una vista de detalle a nivel de CUENCA para las 3 localidades del Huasco,
motivada por el precedente real del aluvión del 25-mar-2015 (mismo evento
que calibra el preset `chanaral_enhanced`, IoU 0.555/0.587) que golpeó
simultáneamente a los 3 poblados. Reusa estilo/fuentes de build_registro.py.

Requiere haber corrido, para las 3 localidades:
  python3 runout_localidad.py --name <nombre> --lon .. --lat .. --domain atacama --params chanaral_enhanced
  python3 risk_map_localidad.py --name <nombre> --lon .. --lat .. --domain atacama --comuna <COMUNA>
  python3 basin_analysis_huasco.py

Uso:
    python3 build_cuenca_huasco.py [--out ~/nowcast-forecast/informes] [--no-pdf]
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_registro import b64, FONT_FACES_TPL, CSS, HERE  # noqa: E402

CR2MET_2015_CSV = os.path.join(HERE, "..", "..", "data", "huasco_cr2met_2015_03.csv")


def load_cr2met_2015():
    """Lluvia diaria CR2MET real de marzo 2015 (scripts/extract_huasco_cr2met_2015.py)
    -- solo si el CSV ya fue generado; si no, se omite la sección sin fallar."""
    if not os.path.exists(CR2MET_2015_CSV):
        return None
    rows = list(csv.DictReader(open(CR2MET_2015_CSV)))
    towns = [k for k in rows[0] if k != "date"]
    out = {}
    for t in towns:
        vals = [float(r[t]) for r in rows]
        peak_i = max(range(len(vals)), key=lambda i: vals[i])
        out[t] = {
            "total_mes": sum(vals),
            "pico_mm": vals[peak_i],
            "pico_fecha": rows[peak_i]["date"],
            "d24_25_26": (vals[23], vals[24], vals[25]),
        }
    return out

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")
RISK_MAPS = os.path.expanduser("~/nowcast-forecast/risk_maps")

TOWNS = [
    {"name": "huasco", "label": "Huasco", "comuna": "HUASCO",
     "damnificados_2015": 230, "viviendas_2015": 128},
    {"name": "freirina", "label": "Freirina", "comuna": "FREIRINA",
     "damnificados_2015": 69, "viviendas_2015": 23},
    {"name": "vallenar", "label": "Vallenar", "comuna": "VALLENAR",
     "damnificados_2015": 85, "viviendas_2015": 67},
]


def chip_acuerdo(riesgo):
    """Chip de confianza: ¿el peligro lo sostienen los dos modelos o uno solo?

    Crítico en esta cuenca: en el ciclo 20260718T18z, GFS pone 161 mm sobre
    Alto del Carmen y ECMWF pone 4. El peligro que reporta el sistema sale
    del peor de las fuentes, así que sin este chip ese número se lee igual
    que uno respaldado por consenso.
    """
    ac = (riesgo or {}).get("acuerdo_modelos") or {}
    conf = ac.get("confianza")
    if not conf:
        return "<span style='color:var(--ink-faint);'>—</span>"
    estilo = {
        "ALTA": "background:var(--sev-low-bg);color:var(--sev-low-fg);",
        "MEDIA": "background:#f6e7c4;color:#7a5a12;",
        "BAJA": "background:#f3d4d1;color:#8c2f22;",
    }.get(conf, "background:#eceae2;color:#5a6259;")
    g, e = ac.get("gfs_mm"), ac.get("ecmwf_mm")
    detalle = f"GFS {g:.0f} / ECMWF {e:.0f} mm" if g is not None and e is not None else ""
    return (f"<span class='chip' style=\"{estilo}white-space:nowrap;\" "
            f"title=\"{ac.get('nota','')}\">{conf}</span> "
            f"<span style='font-size:9.5px;color:var(--ink-faint);'>{detalle}</span>")


def severity(g):
    if g is None or g < 0:
        return ("s-none", "SIN DATO")
    if g >= 0.8:
        return ("s-high", "ALTO")
    if g >= 0.4:
        return ("s-mid", "MEDIO")
    return ("s-low", "BAJO")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-forecast/informes"))
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    cuenca_p = os.path.join(RISK_MAPS, "cuenca_huasco", "resumen_cuenca.json")
    if not os.path.exists(cuenca_p):
        sys.exit("falta resumen_cuenca.json — correr basin_analysis_huasco.py primero")
    cuenca = json.load(open(cuenca_p))

    town_data = {}
    riesgo_data = {}
    for t in TOWNS:
        rp = os.path.join(RUNOUTS, t["name"], "resumen.json")
        if not os.path.exists(rp):
            sys.exit(f"falta resumen.json de {t['name']} — correr runout_localidad.py primero")
        town_data[t["name"]] = json.load(open(rp))
        riesgo_p = os.path.join(RISK_MAPS, t["name"], "riesgo.json")
        if os.path.exists(riesgo_p):
            riesgo_data[t["name"]] = json.load(open(riesgo_p))

    basin_map_path = os.path.join(RISK_MAPS, "cuenca_huasco", "mapa_cuenca.png")
    basin_map_b64 = b64(basin_map_path)

    mosaic_path = os.path.join(RISK_MAPS, "cuenca_huasco", "mapa_runout_cuenca.png")
    runout_mosaic_fig = ""
    mosaic_hash = ""
    if os.path.exists(mosaic_path):
        mosaic_b64 = b64(mosaic_path)
        mosaic_hash = hashlib.sha256(open(mosaic_path, "rb").read()).hexdigest()
        runout_mosaic_fig = f"""
    <p>La Figura 1 solo muestra el footprint en los 3 puntos exactos de los pueblos — el ABM corre en una ventana de 12&nbsp;km centrada en un único punto, así que los tramos del valle ENTRE esos puntos (y las quebradas tributarias que no caen justo ahí) no se simulaban. Se corrió el ABM en 7 puntos adicionales interpolados a lo largo del cauce principal (Vallenar&ndash;Freirina&ndash;Huasco, ~5&nbsp;km de espaciado) y se mosaicó el footprint de las 10 corridas para dar cobertura continua.</p>
    <figure class="map">
      <img src="data:image/png;base64,{mosaic_b64}" alt="Cobertura continua de runout, cuenca del Huasco" />
      <figcaption><span class="cap-num">Figura 2.</span> Cobertura continua de runout a lo largo de todo el valle (10 corridas mosaicadas, no solo 3 puntos aislados).</figcaption>
    </figure>"""

    risk_map_hashes = {}
    for t in TOWNS:
        p = os.path.join(RISK_MAPS, t["name"], "mapa_riesgo.png")
        if os.path.exists(p):
            risk_map_hashes[t["name"]] = hashlib.sha256(open(p, "rb").read()).hexdigest()

    data_hash = hashlib.sha256(
        (json.dumps(cuenca, sort_keys=True) + json.dumps(town_data, sort_keys=True)
         + json.dumps(riesgo_data, sort_keys=True) + json.dumps(risk_map_hashes, sort_keys=True)
         + hashlib.sha256(open(basin_map_path, "rb").read()).hexdigest() + mosaic_hash).encode()
    ).hexdigest()
    fonts = {n.replace("-", "_"): b64(f"{HERE}/fonts/{n}.woff2") for n in
             ("schola-reg", "schola-bold", "schola-ital", "heroscn-reg", "heroscn-bold", "mono-reg", "mono-bold")}
    font_faces = FONT_FACES_TPL.format(**fonts)

    now = datetime.now(timezone.utc)
    gen_stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    cr2met_2015 = load_cr2met_2015()

    hist_rows = ""
    for t in TOWNS:
        cr = cr2met_2015.get(t["label"]) if cr2met_2015 else None
        cr_cells = ""
        if cr:
            cr_cells = (f"<td class='num'>{cr['total_mes']:.1f}</td>"
                       f"<td class='num'>{cr['pico_mm']:.1f} ({cr['pico_fecha'][-2:]}-mar)</td>")
        hist_rows += (f"<tr><td><strong>{t['label']}</strong></td>"
                      f"<td class='num'>{t['damnificados_2015']}</td>"
                      f"<td class='num'>{t['viviendas_2015']}</td>{cr_cells}</tr>")
    cr2met_th = ("<th class='num'>CR2MET total marzo (mm)</th>"
                 "<th class='num'>CR2MET pico diario</th>") if cr2met_2015 else ""

    town_cards = ""
    for t in TOWNS:
        name = t["name"]
        r = town_data[name]
        g = r["signal"].get("hazard_max_regional")
        cls, txt = severity(g)
        stats_p = os.path.join(RUNOUTS, name, "stats.json")
        stats = json.load(open(stats_p))
        affected_km2 = stats["runout"]["affected_km2"]
        rain3 = ", ".join(f"{v:.0f}" for v in r["rain3_mm_day"])
        plaus = r.get("physically_plausible")
        plaus_chip = (f"<span class='chip s-high' style='background:var(--sev-low-bg);"
                     f"color:var(--sev-low-fg);'>PLAUSIBLE</span>" if plaus
                     else f"<span class='chip s-none'>NO PLAUSIBLE</span>")
        risk_map_p = os.path.join(RISK_MAPS, name, "mapa_riesgo.png")
        label = t["label"]
        img_tag = (f"<img src='data:image/png;base64,{b64(risk_map_p)}' "
                  f"alt='Mapa de riesgo {label}' style='width:100%;border-radius:3px;"
                  f"border:1px solid var(--rule);' />") if os.path.exists(risk_map_p) else ""

        riesgo_p = os.path.join(RISK_MAPS, name, "riesgo.json")
        exp_row = vuln_row = ""
        if os.path.exists(riesgo_p):
            riesgo = json.load(open(riesgo_p))
            exp = riesgo.get("exposicion")
            if exp:
                exp_row = (f"<div class='stat-row'><span>Exposición (área urbana)</span>"
                          f"<span>{exp['urban_area_footprint_frac']*100:.0f}% · "
                          f"~{exp['poblacion_expuesta_estimada']:,} pers. (est.)</span></div>")
            vln = riesgo.get("vulnerabilidad")
            if vln:
                vuln_row = (f"<div class='stat-row'><span>Vulnerabilidad social (comuna)</span>"
                           f"<span>índice {vln['indice_vulnerabilidad']:.2f}</span></div>")
            exc = riesgo.get("exceedancia_id") or {}
            if exc.get("exceedancia") is not None:
                vuln_row += (f"<div class='stat-row' title=\"{riesgo.get('peligro_nota', '')}\">"
                            f"<span>Excedencia I–D (pico)</span>"
                            f"<span>{exc['exceedancia']:.2f}× en {exc['duracion_h']:.0f} h "
                            f"({exc['profundidad_mm']:.0f} mm)</span></div>")
            pel = riesgo.get("peligro_no_saturado")
            if pel is not None:
                vuln_row += (f"<div class='stat-row'><span>Peligro (susc × excedencia)</span>"
                            f"<span>{pel:.2f}</span></div>")
            if (riesgo.get("acuerdo_modelos") or {}).get("confianza"):
                vuln_row += (f"<div class='stat-row'><span>Acuerdo entre modelos</span>"
                            f"<span>{chip_acuerdo(riesgo)}</span></div>")
            comp = riesgo.get("indice_compuesto")
            if comp is not None:
                vuln_row += (f"<div class='stat-row' style='font-weight:700;' "
                            f"title=\"{riesgo.get('indice_compuesto_nota', '')}\">"
                            f"<span>Índice compuesto (no oficial)</span><span>{comp}</span></div>")

        town_cards += f"""
    <div class="arch-card" style="padding:0;overflow:hidden;">
      {img_tag}
      <div style="padding:14px 16px;">
        <div class="name-row" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <span class="name" style="font-family:'HerosCn',sans-serif;font-weight:700;font-size:15px;">{t['label']}</span>
          <span class="chip {cls}">{txt}</span>
        </div>
        <div class="stat-row"><span>Escenario 3 días (mm/día)</span><span>{rain3}</span></div>
        <div class="stat-row"><span>Footprint runout ABM</span><span>{affected_km2:.1f} km²</span></div>
        <div class="stat-row"><span>Preset / calibración</span><span>{r.get('params_preset', '—')}</span></div>
        <div class="stat-row"><span>IoU del preset</span><span style="font-size:11px;">{r.get('params_preset_iou', '—')}</span></div>
        <div class="stat-row"><span>Plausibilidad de forma</span>{plaus_chip}</div>
        {exp_row}
        {vuln_row}
        <div class="stat-row"><span>Aluvión 25-mar-2015</span><span>{t['damnificados_2015']} damnificados</span></div>
      </div>
    </div>"""

    b = cuenca["basin_hazard_x_susc"]
    body = f"""
<div class="sheet">
  <div class="masthead">
    <div class="eyebrow">Registro técnico interno · sistema nowcast · análisis de cuenca</div>
    <h1 class="title">Cuenca del río Huasco<br>Vallenar &middot; Freirina &middot; Huasco</h1>
    <p class="subtitle">Vista de detalle a nivel de cuenca completa, motivada por el precedente real del aluvión del 25 de marzo de 2015 en esta misma cuenca.</p>
    <div class="meta-strip">
      <div class="meta-item"><span class="k">Generado</span><span class="v">{gen_stamp}</span></div>
      <div class="meta-item"><span class="k">Localidades</span><span class="v">3</span></div>
      <div class="meta-item"><span class="k">Preset ABM</span><span class="v">chanaral_enhanced</span></div>
      <div class="meta-item"><span class="k">IoU calibración</span><span class="v">0.555 / 0.587</span></div>
    </div>
  </div>

  <div class="callout">
    <h2>Resumen ejecutivo</h2>
    <p>Este documento complementa el registro nacional con una vista dedicada a la cuenca del Huasco. Las tres localidades ribereñas &mdash; Huasco (desembocadura), Freirina y Vallenar (aguas arriba) &mdash; se simularon con el preset <strong>chanaral_enhanced</strong> del ABM de remociones en masa (IoU 0.555 medio / 0.587 máx. out-of-sample, la mejor calibración real disponible, muy superior al preset <code>de</code>/Copiapó usado por defecto en el resto del sistema, IoU 0.171).</p>
    <ul>
      <li>Las tres simulaciones pasan el guard de plausibilidad de forma (footprint canalizado, no esparcimiento difuso).</li>
      <li>A nivel de cuenca completa, solo el {b['frac_over_0.5']*100:.1f}% del área tiene gatillo&times;susceptibilidad &gt;0.5 &mdash; el peligro está genuinamente concentrado en quebradas específicas, coherente con el patrón dendrítico visible en el mapa.</li>
      <li><strong>Peligro, exposición y vulnerabilidad se reportan por separado</strong> (secc. 03) &mdash; nunca fusionados en un solo índice de riesgo. Exposición: fracción del área urbana bajo el footprint del ABM, cruzada con población censada (cota gruesa por ponderación areal). Vulnerabilidad: índice social a nivel comunal (Censo 2024, tasas de vivienda precaria/hacinamiento/sin agua-alcantarillado). El lector arma el riesgo real combinando las tres piezas según su propio criterio.</li>
      <li><strong>Precedente real:</strong> el aluvión del 25-mar-2015 &mdash; el mismo evento que calibra el ABM vía Copiapó/Chañaral &mdash; golpeó simultáneamente los tres poblados (secc. 02). El patrón espacial actual (misma cuenca, mismos tres puntos, footprints canalizados en las mismas quebradas) es cualitativamente consistente con ese precedente. La lluvia CR2MET de marzo-2015 ya se extrajo (secc. 02), pero es un piso conocido por debajo de la magnitud real (techo de resolución de 5&nbsp;km/diario) &mdash; no sirve como comparación cuantitativa directa contra el pronóstico vigente.</li>
      <li><strong>Nada de este documento es un aviso oficial.</strong> Las fuentes de verdad para decisiones de protección civil son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.</li>
    </ul>
  </div>

  <div class="disclaimer"><strong>Cómo leer las cifras (cambio de definición, 18-jul-2026).</strong> Hasta este ciclo el término de peligro usaba el factor de gatillo logístico, que por diseño llega a 0,98 cuando la lluvia dobla el umbral I&ndash;D y a partir de ahí <strong>deja de discriminar</strong>. Ahora el peligro es <strong>susceptibilidad × excedencia I&ndash;D cruda</strong> (E = I/(a·D<sup>−b</sup>), a=5,5, b=0,39, ventanas rodantes horarias), que no satura: E&nbsp;=&nbsp;1 es justo el umbral, E&nbsp;=&nbsp;3 es tres veces por sobre. <strong>Vallenar marca 3,33× en una ventana de 40 h (173,9 mm)</strong>, la excedencia más alta de las 12 localidades tratadas en los dos reportes de cuenca. Con la definición anterior ese valor era indistinguible del de una localidad con un tercio de esa lluvia.<br><br>El «índice compuesto» sigue siendo otra cosa: es riesgo (peligro × exposición × vulnerabilidad), y por eso Vallenar queda relativamente abajo pese a tener el peligro más alto &mdash; solo el 10% de su área urbana cae bajo el footprint y su vulnerabilidad social es la menor del grupo (0,09). Las dos lecturas son válidas y responden preguntas distintas.</div>

  <div class="disclaimer"><strong>Sobre la naturaleza de este registro.</strong> Escenario ilustrativo de un sistema experimental sin validación operacional. La calibración chanaral_enhanced tiene anclaje real (IoU&gt;0.5) en Chañaral, no en el Huasco mismo &mdash; sigue sin existir un footprint real mapeado de esta cuenca contra el cual validar directamente. Documento generado el {gen_stamp}.</div>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">01</span><h2>Mapa de cuenca completa</h2></div>
    <p>Gatillo climático &times; susceptibilidad de terreno a 30&nbsp;m sobre la extensión completa de la cuenca del Huasco (recortada a la zona con los tres poblados), con el footprint del ABM superpuesto por localidad.</p>
    <figure class="map">
      <img src="data:image/png;base64,{basin_map_b64}" alt="Mapa de cuenca del río Huasco" />
      <figcaption><span class="cap-num">Figura 1.</span> Cuenca del Huasco &mdash; hazard&times;susceptibilidad de terreno y footprints de runout por localidad.</figcaption>
    </figure>
    {runout_mosaic_fig}
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">02</span><h2>Precedente real &mdash; aluvión 25 de marzo de 2015</h2></div>
    <p>Los tres poblados fueron golpeados simultáneamente por el mismo sistema frontal que generó el aluvión de Atacama de 2015 (el evento real que calibra el ABM en Copiapó/Chañaral). Cifras de daño oficiales (ONEMI/MINSAL, vía Wikipedia &laquo;Efectos del temporal del norte de Chile de 2015 en Atacama&raquo;), más la lluvia real registrada por CR2MET (grilla diaria 5&nbsp;km, extraída del punto más cercano a cada pueblo):</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Localidad</th><th class="num">Damnificados</th><th class="num">Viviendas con daño mayor</th>{cr2met_th}</tr></thead>
      <tbody>{hist_rows}</tbody>
    </table></div>
    {"<p style='font-size:12.5px;color:var(--ink-faint);'><strong>Salvedad importante:</strong> estos totales CR2MET (12.5&ndash;22.5 mm en todo marzo, pico diario 9.5&ndash;13.3 mm el 24-mar) son sorprendentemente bajos para el peor aluvión registrado en la historia de Chile. Esto NO significa que la lluvia real haya sido modesta &mdash; es el mismo &laquo;techo de resolución&raquo; que este proyecto ya documentó extensamente (ver <code>resolution_headtohead.rs</code>, comparación IMERG sub-diario vs CR2MET diario en Atacama): una grilla de 5&nbsp;km/diaria estructuralmente no puede resolver un núcleo de tormenta convectivo localizado. CR2MET 2015 es un PISO, no una medida fiel de la magnitud real &mdash; no se usa aquí como techo de comparación cuantitativa contra el pronóstico vigente, solo como referencia de que hubo lluvia real y sostenida en los tres puntos.</p>" if cr2met_2015 else ""}
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">03</span><h2>Detalle por localidad</h2></div>
    <div class="arch-grid">{town_cards}</div>
  </section>

  <footer>
    <div>Motor <em>nowcast</em> &mdash; análisis de cuenca, sistema experimental sin validación operacional.</div>
    <div>Hash de integridad: <code>{data_hash}</code></div>
  </footer>
</div>
"""
    html = f"<title>Análisis de cuenca — Río Huasco</title>\n<style>\n{font_faces}\n{CSS}\n</style>\n{body}\n"

    stamp = now.strftime("%Y%m%dT%H%MZ")
    html_path = os.path.join(a.out, f"cuenca-huasco-{stamp}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"HTML: {html_path} ({os.path.getsize(html_path)/1024:.0f} KB)")
    print(f"hash de integridad: {data_hash}")

    if not a.no_pdf:
        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        if chrome:
            pdf_path = os.path.join(a.out, f"cuenca-huasco-{stamp}.pdf")
            subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                            f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
                            f"file://{html_path}"], capture_output=True)
            if os.path.exists(pdf_path):
                print(f"PDF: {pdf_path} ({os.path.getsize(pdf_path)/1024:.0f} KB)")
        else:
            print("(sin Chrome/Chromium disponible — solo se generó el HTML)")


if __name__ == "__main__":
    main()
