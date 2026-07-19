#!/usr/bin/env python3
"""Reporte dedicado — Región de Coquimbo (cuencas Elqui, Limarí, provincia
del Choapa). Análogo a `build_cuenca_huasco.py`, pero multi-cuenca: 9
localidades tratadas con el ABM (chanaral_enhanced) + mapa de riesgo +
exposición/vulnerabilidad/compuesto, motivado por precedente real 2015 y
pronóstico oficial de prensa (Combarbalá 328mm, Punitaqui 290mm, 16-19 jul
2026) para exactamente este evento.

Requiere haber corrido, para las 9 localidades:
  runout_localidad.py + risk_map_localidad.py (domain=coquimbo)
  basin_analysis_coquimbo.py (mosaico regional)

Uso:
    python3 build_cuenca_coquimbo.py [--out ~/nowcast-forecast/informes] [--no-pdf]
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_registro import b64, FONT_FACES_TPL, CSS, HERE  # noqa: E402

RUNOUTS = os.path.expanduser("~/nowcast-forecast/runouts")
RISK_MAPS = os.path.expanduser("~/nowcast-forecast/risk_maps")

TOWNS = [
    {"name": "coquimbo", "label": "Coquimbo", "comuna": "COQUIMBO", "cuenca": "Elqui"},
    {"name": "ovalle", "label": "Ovalle", "comuna": "OVALLE", "cuenca": "Limarí"},
    {"name": "punitaqui", "label": "Punitaqui", "comuna": "PUNITAQUI", "cuenca": "Limarí"},
    {"name": "monte_patria", "label": "Monte Patria", "comuna": "MONTE PATRIA", "cuenca": "Limarí"},
    {"name": "combarbala", "label": "Combarbalá", "comuna": "COMBARBALÁ", "cuenca": "Limarí"},
    {"name": "canela", "label": "Canela", "comuna": "CANELA", "cuenca": "Choapa (transferencia)"},
    {"name": "illapel", "label": "Illapel", "comuna": "ILLAPEL", "cuenca": "Choapa (transferencia)"},
    {"name": "salamanca", "label": "Salamanca", "comuna": "SALAMANCA", "cuenca": "Choapa (transferencia)"},
    {"name": "los_vilos", "label": "Los Vilos", "comuna": "LOS VILOS", "cuenca": "Choapa (transferencia)"},
]

DAMAGE_2015 = {
    "Vicuña": (208, 50), "Combarbalá": (80, 20), "Coquimbo": (84, 18),
    "Andacollo": (46, 9), "Salamanca": (22, 9), "Punitaqui": (11, 2),
    "Canela": (8, 3), "Paihuano": (37, 4), "La Serena": (2, 7), "Illapel": (2, 0),
}
CONTEXT_ONLY_2015 = ["Vicuña", "Andacollo", "Paihuano", "La Serena"]

# localidades cuyo punto cae fuera de la cuenca real delineada del raster que
# las modela (zona de relleno del rectángulo DEM, sin textura de terreno real
# -- std local <0.02 vs. 0.1-0.25 típico dentro de la cuenca real). Sin cuenca
# alternativa disponible en DOMAIN_BASINS para reasignar (a diferencia de
# Canela, que sí tenía Choapa transferencia más cerca). Hallazgo 2026-07-17.
LOW_CONFIDENCE_COVERAGE = {
    "coquimbo": "el punto urbano (desembocadura del Elqui) cae fuera de la cuenca "
                "delineada del modelo 07_rio_elqui (zona de relleno sin textura de "
                "terreno real, std local 0.014 vs. ~0.2 dentro de la cuenca) — el "
                "modelo se entrenó sobre la cuenca alta, no sobre el valle costero. "
                "Sin alternativa en DOMAIN_BASINS. Peligro/exposición en el punto "
                "tratar como referencia débil, no como señal de terreno real.",
}


def chip_acuerdo(riesgo):
    """Chip de confianza: ¿el peligro lo sostienen los dos modelos o uno?

    El peligro sale del peor de las fuentes disponibles, así que un número
    alto puede venir de un consenso o de un solo modelo contra otro que ve
    casi nada. Sin esto, Alto del Carmen (solo GFS: 161 mm contra 4 mm de
    ECMWF) y Juntas (los dos sobre 100 mm) se ven idénticos en la tabla.
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
            f"title=\"{ac.get('nota','')}\">{conf}</span>"
            f"<div style='font-size:9.5px;color:var(--ink-faint);margin-top:2px;'>{detalle}</div>")


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

    regional_p = os.path.join(RISK_MAPS, "cuenca_coquimbo", "resumen_regional.json")
    if not os.path.exists(regional_p):
        sys.exit("falta resumen_regional.json — correr basin_analysis_coquimbo.py primero")
    regional = json.load(open(regional_p))

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

    map_path = os.path.join(RISK_MAPS, "cuenca_coquimbo", "mapa_regional.png")
    map_b64 = b64(map_path)
    map_hash = hashlib.sha256(open(map_path, "rb").read()).hexdigest()

    risk_map_hashes = {}
    for t in TOWNS:
        p = os.path.join(RISK_MAPS, t["name"], "mapa_riesgo.png")
        if os.path.exists(p):
            risk_map_hashes[t["name"]] = hashlib.sha256(open(p, "rb").read()).hexdigest()

    data_hash = hashlib.sha256(
        (json.dumps(regional, sort_keys=True) + json.dumps(town_data, sort_keys=True)
         + json.dumps(riesgo_data, sort_keys=True) + json.dumps(risk_map_hashes, sort_keys=True)
         + map_hash).encode()
    ).hexdigest()

    fonts = {n.replace("-", "_"): b64(f"{HERE}/fonts/{n}.woff2") for n in
             ("schola-reg", "schola-bold", "schola-ital", "heroscn-reg", "heroscn-bold", "mono-reg", "mono-bold")}
    font_faces = FONT_FACES_TPL.format(**fonts)

    now = datetime.now(timezone.utc)
    gen_stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ranking por PELIGRO ---------------------------------------------
    # Ordenado por peligro (susceptibilidad × excedencia I-D), no por el índice
    # compuesto: la pregunta que este documento contesta primero es "dónde está
    # pegando más fuerte la tormenta sobre terreno susceptible". El compuesto
    # (riesgo: peligro × exposición × vulnerabilidad) va como segunda columna
    # porque responde otra cosa — dónde va a doler más — y los dos ordenamientos
    # difieren fuerte: Combarbalá encabeza el peligro y es último en compuesto.
    ranked = []
    for t in TOWNS:
        r = riesgo_data.get(t["name"], {})
        pel = r.get("peligro_no_saturado")
        ranked.append((pel if pel is not None else -1, t))
    ranked.sort(key=lambda x: -x[0])

    ranking_rows = ""
    for i, (pel, t) in enumerate(ranked, 1):
        r = riesgo_data.get(t["name"], {})
        exp = r.get("exposicion") or {}
        pobexp = exp.get("poblacion_expuesta_estimada")
        pobexp_s = f"{pobexp:,}" if pobexp is not None else "—"
        pel_s = f"{pel:.2f}" if pel >= 0 else "—"
        comp = r.get("indice_compuesto")
        comp_s = f"{comp}" if comp is not None else "—"
        e = (r.get("exceedancia_id") or {}).get("exceedancia")
        e_s = f"{e:.2f}&times;" if e is not None else "—"
        ranking_rows += (f"<tr><td class='num'>{i}</td><td><strong>{t['label']}</strong></td>"
                         f"<td>{t['cuenca']}</td><td class='num'>{pel_s}</td>"
                         f"<td>{chip_acuerdo(r)}</td>"
                         f"<td class='num'>{comp_s}</td><td class='num'>{e_s}</td>"
                         f"<td class='num'>{pobexp_s}</td></tr>")

    # --- tabla 2015 (tratadas + contexto) ------------------------------
    hist_rows = ""
    for t in TOWNS:
        dmn, viv = DAMAGE_2015.get(t["label"], (None, None))
        hist_rows += (f"<tr><td><strong>{t['label']}</strong></td><td>tratamiento completo</td>"
                      f"<td class='num'>{dmn if dmn is not None else '—'}</td>"
                      f"<td class='num'>{viv if viv is not None else '—'}</td></tr>")
    for name in CONTEXT_ONLY_2015:
        dmn, viv = DAMAGE_2015[name]
        hist_rows += (f"<tr><td>{name}</td><td style='color:var(--ink-faint);'>solo contexto (sin ABM hoy)</td>"
                      f"<td class='num'>{dmn}</td><td class='num'>{viv}</td></tr>")

    # --- tarjetas por pueblo --------------------------------------------
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

        riesgo = riesgo_data.get(name, {})
        exp_row = vuln_row = ""
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
        ac = riesgo.get("acuerdo_modelos") or {}
        if ac.get("confianza"):
            vuln_row += (f"<div class='stat-row'><span>Acuerdo entre modelos</span>"
                        f"<span>{chip_acuerdo(riesgo)}</span></div>")
        comp = riesgo.get("indice_compuesto")
        if comp is not None:
            vuln_row += (f"<div class='stat-row' style='font-weight:700;' "
                        f"title=\"{riesgo.get('indice_compuesto_nota', '')}\">"
                        f"<span>Índice compuesto (no oficial)</span><span>{comp}</span></div>")

        coverage_note = ""
        if name in LOW_CONFIDENCE_COVERAGE:
            coverage_note = (f"<div class='stat-row' style='color:#9a5a1a;font-size:10.5px;' "
                            f"title=\"{LOW_CONFIDENCE_COVERAGE[name]}\">"
                            f"<span>⚠ cobertura de modelo débil en el punto — ver nota QA</span></div>")

        town_cards += f"""
    <div class="arch-card" style="padding:0;overflow:hidden;">
      {img_tag}
      <div style="padding:14px 16px;">
        <div class="name-row" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <span class="name" style="font-family:'HerosCn',sans-serif;font-weight:700;font-size:15px;">{t['label']}</span>
          <span class="chip {cls}">{txt}</span>
        </div>
        <div class="region" style="font-size:11px;color:var(--ink-faint);margin-bottom:6px;">{t['cuenca']}</div>
        <div class="stat-row"><span>Escenario 3 días (mm/día)</span><span>{rain3}</span></div>
        <div class="stat-row"><span>Footprint runout ABM</span><span>{affected_km2:.1f} km²</span></div>
        <div class="stat-row"><span>Plausibilidad de forma</span>{plaus_chip}</div>
        {exp_row}
        {vuln_row}
        {coverage_note}
      </div>
    </div>"""

    body = f"""
<div class="sheet">
  <div class="masthead">
    <div class="eyebrow">Registro técnico interno · sistema nowcast · análisis regional</div>
    <h1 class="title">Región de Coquimbo<br>Cuencas Elqui &middot; Limarí &middot; Provincia del Choapa</h1>
    <p class="subtitle">9 localidades con tratamiento completo, motivado por reportes de prensa (328&nbsp;mm Combarbalá, 290&nbsp;mm Punitaqui pronosticados oficialmente para 16&ndash;19 jul 2026) y precedente real del aluvión de marzo 2015.</p>
    <div class="meta-strip">
      <div class="meta-item"><span class="k">Generado</span><span class="v">{gen_stamp}</span></div>
      <div class="meta-item"><span class="k">Localidades</span><span class="v">9</span></div>
      <div class="meta-item"><span class="k">Cuencas</span><span class="v">Elqui · Limarí · Choapa (transf.)</span></div>
      <div class="meta-item"><span class="k">Preset ABM</span><span class="v">chanaral_enhanced (IoU 0.555)</span></div>
    </div>
  </div>

  <div class="callout">
    <h2>Resumen ejecutivo</h2>
    <p>A diferencia de la cuenca del Huasco (un valle lineal único), la Región de Coquimbo tiene tres sistemas hidrográficos distintos: Elqui (Coquimbo/La Serena), Limarí (Ovalle/Punitaqui/Monte Patria/Combarbalá) y la provincia del Choapa (Illapel/Salamanca/Los Vilos/Canela), que <strong>no tenía modelo de susceptibilidad propio</strong> — se construyó una transferencia real desde Limarí (modelo sustituto RandomForest, R²=0.916 contra el output publicado de Limarí, 14 de 15 factores del modelo original vía SurtGIS sobre DEM Copernicus propio de Choapa; ver secc. 03). Canela se modela con esta misma transferencia, no con el raster crudo de Limarí — ver nota QA abajo.</p>
    <ul>
      <li><strong>Dónde pega más fuerte la tormenta</strong> (peligro, sin exposición ni vulnerabilidad): <strong>Combarbalá</strong> encabeza con excedencia I&ndash;D 2,46× sobre terreno de susceptibilidad 0,86, seguida de Punitaqui (1,81×) y Ovalle (1,73×). En el Huasco, Vallenar llega a <strong>3,33×</strong> — la excedencia más alta de todas las localidades tratadas en ambos reportes.</li>
      <li><strong>El ranking (secc. 02) va ordenado por peligro</strong>, no por el índice compuesto: <strong>Combarbalá</strong> (2,11) encabeza, seguida de Punitaqui (1,36) y Los Vilos (1,23).</li>
      <li><strong>El compuesto ordena muy distinto</strong> y va como segunda columna: ahí Canela (0,441) queda primera y <strong>Combarbalá cae al último lugar</strong> (0,207) pese a tener el peligro más alto — solo el 2,2% de su área urbana queda bajo el footprint, unas 121 personas. Es la diferencia entre «dónde pega más fuerte» y «dónde va a doler más»; las dos preguntas son legítimas y dan respuestas casi opuestas.</li>
      <li><strong>Precedente real 2015:</strong> Vicuña fue la comuna MÁS dañada de toda la región (208 damnificados, 50 viviendas con daño mayor) — no se corrió tratamiento completo hoy, queda como contexto (secc. 02).</li>
      <li><strong>Pronóstico oficial ya circulando en prensa</strong> (13-jul-2026, Diario El Día): 328&nbsp;mm en Combarbalá y 290&nbsp;mm en Punitaqui para esta ventana, citando "no enfrenta un evento así desde 2015".</li>
      <li>Peligro, exposición y vulnerabilidad se reportan por separado (secc. 04); el índice compuesto es una cuarta pieza no oficial.</li>
      <li><strong>Nada de este documento es un aviso oficial.</strong> Las fuentes de verdad son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.</li>
    </ul>
  </div>

  <div class="disclaimer"><strong>Nota QA (17-jul-2026).</strong> Los rasters de susceptibilidad de paper1 cubren el rectángulo DEM completo, no solo la cuenca real delineada; fuera de la cuenca el valor no es señal de terreno, es relleno del modelo/interpolación de borde (confirmado por textura: varianza local &lt;0.02 en el relleno vs. 0.1&ndash;0.25 dentro de la cuenca real). Dos localidades de este reporte caían en esa zona: <strong>Canela</strong> (en el relleno de 08_rio_limari, con artefacto adicional de enrutamiento de flujo diagonal) se reasignó a la transferencia Choapa, más cercana y con cobertura real en su punto — índice compuesto corregido de 0.456 a 0.383. <strong>Coquimbo</strong> (desembocadura del Elqui) también cae en el relleno de 07_rio_elqui, pero no hay cuenca alternativa en el registro (Limarí/Choapa quedan aún más lejos) — su fila queda marcada como cobertura débil (⚠, secc. 04) en vez de reasignarse o eliminarse. El mapa regional (secc. 01) recorta cada capa a 20&nbsp;km de sus propias localidades para no mostrar la franja de relleno. Barrido más amplio sobre las ~40 localidades tratadas en otras regiones del proyecto (mismo patrón, magnitud aún no cuantificada) queda pendiente — ver conversación con el usuario.</div>

  <div class="disclaimer"><strong>Cómo leer la tabla (cambio de definición, 18-jul-2026).</strong> Hasta este ciclo el término de peligro usaba el factor de gatillo logístico, que por diseño llega a 0,98 cuando la lluvia dobla el umbral I&ndash;D y a partir de ahí <strong>deja de discriminar</strong>: en un evento extremo todas las localidades quedaban con el mismo ≈1,0 y el ranking terminaba decidido solo por la susceptibilidad estática. Ahora el peligro es <strong>susceptibilidad × excedencia I&ndash;D cruda</strong> (E = I/(a·D<sup>−b</sup>), a=5,5, b=0,39, ventanas rodantes horarias): E&nbsp;=&nbsp;1 es justo el umbral, E&nbsp;=&nbsp;3 es tres veces por sobre, y no satura. Por eso el peligro ya no está acotado a 1.<br><br><strong>Las tres columnas responden preguntas distintas y no hay que confundirlas.</strong> «Excedencia» y «Peligro» dicen <em>dónde la tormenta pega más fuerte sobre terreno susceptible</em>. El «Índice compuesto» es riesgo: multiplica ese peligro por exposición urbana y vulnerabilidad social, así que una localidad chica, pobre y muy expuesta puede encabezarlo con menos lluvia que una ciudad grande y mejor equipada. Ambas lecturas son legítimas; usar la equivocada lleva a conclusiones opuestas.</div>

  <div class="disclaimer"><strong>Sobre la naturaleza de este registro.</strong> Escenario ilustrativo de un sistema experimental sin validación operacional. La transferencia Choapa es dos pasos removida del modelo real (sustituto + factores incompletos) — ver meta.json. Documento generado el {gen_stamp}.</div>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">01</span><h2>Mapa regional</h2></div>
    <p>Mosaico de las 3 fuentes de peligro×susceptibilidad (Elqui, Limarí, transferencia Choapa) a 30&nbsp;m, con las 9 localidades marcadas.</p>
    <figure class="map">
      <img src="data:image/png;base64,{map_b64}" alt="Mapa regional de Coquimbo" />
      <figcaption><span class="cap-num">Figura 1.</span> Región de Coquimbo — peligro×susceptibilidad, cuencas Elqui/Limarí + transferencia Choapa.</figcaption>
    </figure>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">02</span><h2>Ranking por peligro</h2></div>
    <p>Ordenado por <strong>peligro</strong> (susceptibilidad × excedencia I&ndash;D): dónde la tormenta pega más fuerte sobre terreno que puede fallar. El <strong>índice compuesto</strong> va en la segunda columna y ordena distinto — es riesgo, incorpora exposición urbana y vulnerabilidad social. Ver la nota de lectura arriba.</p>
    <p><strong>La columna «acuerdo modelos» dice cuánto creerle a la fila.</strong> El peligro se calcula sobre el peor de los pronósticos disponibles, lo que es prudente pero esconde si ese número lo sostienen dos modelos o uno solo contra otro que ve casi nada. <span class="chip" style="background:var(--sev-low-bg);color:var(--sev-low-fg);">ALTA</span> = GFS y ECMWF coinciden en que llueve ahí (el menor es ≥50% del mayor); <span class="chip" style="background:#f6e7c4;color:#7a5a12;">MEDIA</span> = ambos ven lluvia pero difieren fuerte; <span class="chip" style="background:#f3d4d1;color:#8c2f22;">BAJA</span> = la señal la sostiene un solo modelo. <strong>La etiqueta mide acuerdo, no magnitud</strong>: hay que mirar los milímetros al lado para saber si además la lluvia es grande. ICON queda fuera de la etiqueta por una razón que se verifica automáticamente en cada corrida: <strong>acumula menos horas que las otras dos</strong>. DWD publica ICON progresivamente y el fetcher toma los lead times disponibles al momento de correr, así que su total puede cubrir 24&nbsp;h mientras GFS cubre 119 — comparar esos acumulados no significa nada, ICON se vería más seco por construcción. No es discrepancia meteorológica. El campo <code>ventanas</code> de cada <code>riesgo.json</code> trae la ventana real de cada fuente.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Localidad</th><th>Cuenca</th><th class="num">Peligro</th><th>Acuerdo modelos</th><th class="num">Índice compuesto</th><th class="num">Excedencia I&ndash;D</th><th class="num">Pob. expuesta est.</th></tr></thead>
      <tbody>{ranking_rows}</tbody>
    </table></div>

    <h3 style="margin-top:24px;">Precedente real — aluvión 25 de marzo de 2015</h3>
    <p>Cifras oficiales (ONEMI, vía Wikipedia «Efectos del temporal del norte de Chile de 2015 en Atacama»). Incluye comunas sin tratamiento completo hoy, como contexto.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Localidad</th><th>Estado hoy</th><th class="num">Damnificados 2015</th><th class="num">Viviendas daño mayor 2015</th></tr></thead>
      <tbody>{hist_rows}</tbody>
    </table></div>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">03</span><h2>Metodología — transferencia Choapa</h2></div>
    <p>Illapel, Salamanca y Los Vilos no tienen cuenca de susceptibilidad propia. Se construyó <code>18_choapa_transfer</code>: (1) DEM Copernicus GLO-30 propio de Choapa vía streaming COG (SurtGIS); (2) 14 factores geomorfométricos (HAND, TWI, TPI, openness ±, convergencia, curvatura, TRI, SVF, hillshade, flow accumulation MFD, slope, dev, elevación) computados con <code>surtgis terrain all</code> + <code>surtgis hydrology all</code>; (3) modelo <strong>sustituto</strong> (RandomForest) entrenado para reproducir el output YA PUBLICADO de Limarí (el clasificador XGBoost original no está persistido en disco) — R²=0.916 held-out contra ese output; (4) aplicado al stack de Choapa. Se omite <code>s_CSI</code> (índice espectral, requeriría imagen satelital aparte) — 14 de 15 factores del modelo real. Es una transferencia más floja que <code>16_araucania_transfer</code>/<code>17_costa_transfer</code> (sustituto + factor incompleto), rotulada como tal.</p>
  </section>

  <section class="doc">
    <div class="sec-head"><span class="sec-num">04</span><h2>Detalle por localidad</h2></div>
    <div class="arch-grid">{town_cards}</div>
  </section>

  <footer>
    <div>Motor <em>nowcast</em> &mdash; análisis regional, sistema experimental sin validación operacional.</div>
    <div>Hash de integridad: <code>{data_hash}</code></div>
  </footer>
</div>
"""
    html = f"<title>Análisis regional — Coquimbo/Choapa</title>\n<style>\n{font_faces}\n{CSS}\n</style>\n{body}\n"

    stamp = now.strftime("%Y%m%dT%H%MZ")
    html_path = os.path.join(a.out, f"cuenca-coquimbo-{stamp}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"HTML: {html_path} ({os.path.getsize(html_path)/1024:.0f} KB)")
    print(f"hash de integridad: {data_hash}")

    if not a.no_pdf:
        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        if chrome:
            pdf_path = os.path.join(a.out, f"cuenca-coquimbo-{stamp}.pdf")
            subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                            f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
                            f"file://{html_path}"], capture_output=True)
            if os.path.exists(pdf_path):
                print(f"PDF: {pdf_path} ({os.path.getsize(pdf_path)/1024:.0f} KB)")
        else:
            print("(sin Chrome/Chromium disponible — solo se generó el HTML)")


if __name__ == "__main__":
    main()
