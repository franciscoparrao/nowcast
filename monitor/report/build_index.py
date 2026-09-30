#!/usr/bin/env python3
"""Índice de la bitácora del pronóstico: UNA página para ver lo que sale día a día.

`build_registro.py` deja en `~/nowcast-forecast/informes/` un registro HTML+PDF por
ciclo (423 archivos a fines de sep-2026), pero cada uno es una foto aislada: no
había dónde ver la evolución ni encontrar el registro de un día sin abrirlos de a
uno. Este script escribe `informes/index.html`, autónomo (fuentes y estilos
embebidos, sin servidor: se abre con doble clic), con:

  · estado de los servicios (monitores, pronóstico, vigía) y del último ciclo;
  · línea de tiempo de 30 días: pico del índice de excedencia I-D por dominio y
    modelo (GFS/ECMWF/ICON), un pequeño múltiplo por dominio, umbral 0.5;
  · la bitácora por día: cada ciclo con sus cifras y el enlace a su registro.

Solo lee lo que la poda conserva siempre (forecast.json, icon.json, ecmwf.json,
comuna_impact.json, watchlist.json): no suma peso al disco. El mapeo informe →
ciclo se cachea en informes/.index_cache.json (los registros no cambian).

Qué es cada número: el PICO del índice de excedencia del umbral lluvia-duración
regional (a=5.5) dentro del horizonte de 120 h de ESE ciclo. Es un pronóstico,
no una observación, y no es una probabilidad calibrada ni un aviso oficial.

Uso:
    python3 build_index.py [--base ~/nowcast-forecast] [--dias 30]
"""

import argparse
import base64
import glob
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from variantes import VARIANTE_POR_MODELO, pico as pico_modelo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCL = ZoneInfo("America/Santiago")   # hora de Chile REAL (UTC−4/−3 según la fecha)
VARIANTE = VARIANTE_POR_MODELO["gfs"]   # la del primer cruce de GFS
NIVEL = 0.5                           # el ALERT_LEVEL del pronóstico
CICLO_RE = re.compile(r"^\d{8}T\d{2}z$")

# Norte → sur, y el barrido macro al final (cubre a varios de los otros).
DOMINIOS = [
    ("atacama", "Atacama"),
    ("coquimbo", "Coquimbo"),
    ("rm", "Región Metropolitana"),
    ("nuble-biobio", "Ñuble – Biobío"),
    ("araucania", "Araucanía"),
    ("chile-centro-sur", "Barrido macro"),
]
MODELOS = [("gfs", "GFS"), ("ecmwf", "ECMWF"), ("icon", "ICON")]


# --- lectura ---------------------------------------------------------------------

def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None




def leer_ciclo(d):
    """Cifras de un directorio de ciclo; None si no tiene ningún dominio."""
    dom = {}
    for key, _ in DOMINIOS:
        g = _json(os.path.join(d, key, "forecast.json"))
        if g is None:
            continue
        v = (g.get("variants") or {}).get(VARIANTE) or {}
        e = _json(os.path.join(d, key, "ecmwf", "ecmwf.json"))
        dom[key] = {
            "gfs": pico_modelo(g, "gfs"),
            "icon": pico_modelo(_json(os.path.join(d, key, "icon", "icon.json")), "icon"),
            "ecmwf": pico_modelo(e, "ecmwf"),
            "ecmwf_ciclo": (e or {}).get("cycle"),
            "cruce": v.get("first_crossing"),
            "iso0": g.get("iso0_mean_m"),
        }
    if not dom:
        return None
    ci = _json(os.path.join(d, "comuna_impact", "comuna_impact.json")) or {}
    wl = _json(os.path.join(d, "watchlist.json")) or {}
    sel = wl.get("selected") or []
    return {
        "dom": dom,
        "n_alto": ci.get("n_alto"),
        "n_medio": ci.get("n_medio"),
        "pob_alto": ci.get("poblacion_en_comunas_alto"),
        "watch": [s.get("label") or s.get("name") for s in sel],
    }


def t_ciclo(nombre):
    return datetime.strptime(nombre[:11], "%Y%m%dT%H").replace(tzinfo=timezone.utc)


def informes_por_ciclo(inf_dir):
    """{ciclo: {'html': nombre, 'pdf': nombre|None}} con caché por archivo."""
    cache_p = os.path.join(inf_dir, ".index_cache.json")
    cache = _json(cache_p) or {}
    out, cambio = {}, False
    for p in sorted(glob.glob(os.path.join(inf_dir, "registro-evento-*.html"))):
        n = os.path.basename(p)
        if n not in cache:
            with open(p, errors="replace") as f:
                cab = f.read(900_000)        # la ficha va antes de las figuras
            m = re.search(r'Ciclo GFS base</span><span class="v">([0-9Tz]+)<', cab)
            cache[n] = m.group(1) if m else None
            cambio = True
        cyc = cache[n]
        if cyc:
            pdf = n[:-5] + ".pdf"
            # si un ciclo tiene varios registros, gana el último generado
            out[cyc] = {"html": n, "pdf": pdf if os.path.exists(os.path.join(inf_dir, pdf)) else None}
    if cambio:
        tmp = cache_p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, cache_p)
    return out


def servicios(base):
    """Estado de monitores, pronóstico y vigía a partir de sus marcas en disco."""
    ahora = datetime.now(timezone.utc).timestamp()
    wd = os.path.expanduser("~/nowcast-monitor/watchdog")
    caidos = {os.path.basename(p)[5:] for p in glob.glob(os.path.join(wd, "down_*"))
              if not p.endswith(".hyst")}
    filas = []

    def edad(p):
        try:
            return (ahora - int(open(p).read().strip())) / 3600
        except (OSError, ValueError):
            return None

    for cfg in sorted(glob.glob(os.path.expanduser("~/nowcast-monitor/domains/*/config.env"))):
        dom = os.path.basename(os.path.dirname(cfg))
        m = re.search(r'^WORK_DIR="?([^"\n]+)"?', open(cfg).read(), re.M)
        if not m:
            continue
        wdir = os.path.expanduser(m.group(1).replace("$HOME", "~"))
        run = _json(os.path.join(wdir, "out", "last_run.json")) or {}
        pico = max((s.get("max_probability") or 0) for s in run.get("steps") or [{}])
        filas.append({
            "nombre": f"Monitor {dom}", "edad_h": edad(os.path.join(wdir, "state", "last_ok")),
            "umbral_h": 3, "caido": f"monitor_{dom}" in caidos,
            "detalle": (f"{run.get('n_alerts', 0)} paso(s) en alerta · pico {pico:.2f}"
                        if run else "sin corrida registrada"),
        })
    filas.append({
        "nombre": "Pronóstico", "edad_h": edad(os.path.join(base, "state", "last_ok")),
        "umbral_h": 14, "caido": "forecast" in caidos,
        "detalle": "GFS + ECMWF + ICON, cada 6 h, horizonte 120 h",
    })
    return filas


# --- formato ---------------------------------------------------------------------

def esc(x):
    return html.escape(str(x), quote=True)


def nivel(p):
    if p is None:
        return "none", "sin dato"
    if p >= NIVEL:
        return "high", "alto"
    if p >= 0.3:
        return "mid", "medio"
    return "low", "bajo"


def chip(p, corto=False):
    cls, txt = nivel(p)
    val = "—" if p is None else f"{p:.2f}"
    if corto:
        return f'<span class="chip {cls}" title="{txt}">{val}</span>'
    return f'<span class="chip {cls}">{val}<em>{txt}</em></span>'


def hora_local(dt):
    loc = dt.astimezone(SCL)
    off = int(loc.utcoffset().total_seconds() // 3600)
    return f"{loc:%d-%m %H:%M} (UTC{off:+d})"


def cruce_txt(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.strptime(iso.rstrip("Z")[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return esc(iso)
    return f'{hora_local(dt)}'


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_larga(d):
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


# --- gráfico ------------------------------------------------------------------------

W, H, PADL, PADR, PADT, PADB = 760, 70, 4, 4, 6, 6


def svg_multiple(key, serie, t0, t1, huecos=(), rotular=False):
    """Un pequeño múltiplo: 3 líneas (modelos) sobre el umbral 0.5. Los datos van en
    data-* para el tooltip; las líneas se cortan si faltan >13 h (ciclo perdido)."""
    span = (t1 - t0).total_seconds() or 1
    x = lambda t: PADL + (W - PADL - PADR) * (t - t0).total_seconds() / span
    y = lambda v: PADT + (H - PADT - PADB) * (1 - v)
    partes = []
    # Huecos sin NINGÚN ciclo (>24 h): banda rotulada, para que un sistema caído
    # no se lea como "no hubo nada que pronosticar" (sep-2026: 23 días así).
    for a, b in huecos:
        xa, xb = x(max(a, t0)), x(min(b, t1))
        partes.append(f'<rect class="gap" x="{xa:.1f}" y="0" width="{max(xb - xa, 0):.1f}" height="{H}"/>')
        if rotular and xb - xa > 90:
            dias = (b - a).total_seconds() / 86400
            partes.append(f'<text class="gaplbl" x="{(xa + xb) / 2:.1f}" y="{H * 0.80:.1f}" '
                          f'text-anchor="middle">sin ciclos · {dias:.0f} días</text>')
    partes += [f'<line class="thr" x1="{PADL}" x2="{W - PADR}" y1="{y(NIVEL):.1f}" y2="{y(NIVEL):.1f}"/>',
              f'<line class="base" x1="{PADL}" x2="{W - PADR}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>']
    for m, _ in MODELOS:
        tramos, cur, prev = [], [], None
        for t, vals in serie:
            v = vals.get(m)
            if v is None or (prev is not None and (t - prev) > timedelta(hours=13)):
                if len(cur) > 1:
                    tramos.append(cur)
                elif len(cur) == 1:
                    tramos.append(cur)
                cur = []
            if v is not None:
                cur.append((x(t), y(v)))
                prev = t
        if cur:
            tramos.append(cur)
        for tr in tramos:
            if len(tr) == 1:
                partes.append(f'<circle class="m-{m}" cx="{tr[0][0]:.1f}" cy="{tr[0][1]:.1f}" r="2.5"/>')
            else:
                pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in tr)
                partes.append(f'<polyline class="m-{m}" points="{pts}"/>')
    datos = [[round(x(t), 1), t.strftime("%Y%m%dT%Hz"),
              *[None if vals.get(m) is None else round(vals[m], 3) for m, _ in MODELOS]]
             for t, vals in serie]
    return (f'<svg class="mult" viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
            f'role="img" aria-label="Pico de excedencia por modelo, dominio {esc(key)}" '
            f"data-pts='{esc(json.dumps(datos))}'>"
            + "".join(partes)
            + f'<line class="cross" y1="0" y2="{H}" x1="-10" x2="-10"/></svg>')


# --- página --------------------------------------------------------------------------

CSS = """
:root { --m-gfs:#1170a3; --m-ecmwf:#b5541d; --m-icon:#7b4a9e; }
:root[data-theme="dark"] { --m-gfs:#4a99cc; --m-ecmwf:#cf7b45; --m-icon:#a476cf; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --m-gfs:#4a99cc; --m-ecmwf:#cf7b45; --m-icon:#a476cf; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
  font:16px/1.55 'Schola', 'Century Schoolbook', 'TeX Gyre Schola', Georgia, serif; }
a { color:var(--data); text-underline-offset:2px; }
.sheet { max-width:1000px; margin:0 auto; padding:clamp(20px,4vw,48px) clamp(16px,4vw,40px) 96px; }
.kicker, .lbl, th { font:700 11px/1.3 'HerosCn', 'Arial Narrow', 'Liberation Sans Narrow', sans-serif;
  letter-spacing:.14em; text-transform:uppercase; color:var(--ink-faint); }
.kicker { color:var(--accent); display:flex; gap:12px; align-items:center; }
.kicker::before { content:""; width:28px; height:1px; background:var(--accent); }
h1 { font:700 clamp(30px,4.6vw,46px)/1.05 'HerosCn', 'Arial Narrow', sans-serif; margin:14px 0 6px;
  letter-spacing:-.01em; }
.sub { font-style:italic; color:var(--ink-soft); margin:0 0 18px; max-width:62ch; }
.meta { display:flex; flex-wrap:wrap; gap:6px 34px; padding:14px 0; border-top:1px solid var(--rule);
  border-bottom:2px solid var(--ink); }
.meta .v { display:block; font:13px/1.4 'MonoInstr', 'DejaVu Sans Mono', monospace; color:var(--ink); }
h2 { font:700 19px/1.2 'HerosCn', 'Arial Narrow', sans-serif; margin:56px 0 4px; display:flex;
  gap:12px; align-items:baseline; }
h2 .n { font:13px 'MonoInstr', monospace; color:var(--accent); }
.nota { color:var(--ink-soft); font-size:14.5px; margin:4px 0 18px; max-width:70ch; }
.svc { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:0;
  border-top:1px solid var(--rule); }
.svc > div { padding:14px 18px 14px 0; border-bottom:1px solid var(--rule); }
.svc .estado { font:700 15px 'HerosCn', sans-serif; display:flex; align-items:center; gap:8px; }
.dot { width:9px; height:9px; border-radius:50%; background:var(--sev-low-fg); flex:none; }
.dot.warn { background:var(--sev-mid-fg); } .dot.bad { background:var(--sev-high-fg); }
.svc small { display:block; color:var(--ink-soft); font-size:13.5px; }
table { width:100%; border-collapse:collapse; font-size:14.5px; }
th { text-align:left; padding:8px 10px 8px 0; border-bottom:1px solid var(--rule-strong);
  vertical-align:bottom; }
td { padding:9px 10px 9px 0; border-bottom:1px solid var(--rule); vertical-align:top; }
td.num { text-align:right; font-family:'MonoInstr', monospace; font-size:13.5px; }
th.num { text-align:right; }
.chip { display:inline-flex; align-items:baseline; gap:6px; padding:1px 7px; border-radius:3px;
  font:13px 'MonoInstr', monospace; }
.chip em { font:italic 12px 'Schola', serif; }
.chip.high { background:var(--sev-high-bg); color:var(--sev-high-fg); }
.chip.mid { background:var(--sev-mid-bg); color:var(--sev-mid-fg); }
.chip.low { background:var(--sev-low-bg); color:var(--sev-low-fg); }
.chip.none { background:var(--sev-none-bg); color:var(--sev-none-fg); }
.legend { display:flex; gap:22px; flex-wrap:wrap; margin:8px 0 14px; font-size:14px; }
.legend span { display:inline-flex; align-items:center; gap:8px; }
.legend i { width:22px; height:2px; display:inline-block; }
.legend i.thr { height:0; border-top:1px dashed var(--ink-faint); }
.row { display:grid; grid-template-columns:150px 1fr; gap:14px; align-items:center;
  padding:6px 0; border-bottom:1px solid var(--rule); position:relative; }
.row .lbl { color:var(--ink-soft); }
.mult { width:100%; height:70px; display:block; overflow:visible; cursor:crosshair; }
.mult polyline { fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.mult .m-gfs { stroke:var(--m-gfs); fill:var(--m-gfs); }
.mult .m-ecmwf { stroke:var(--m-ecmwf); fill:var(--m-ecmwf); }
.mult .m-icon { stroke:var(--m-icon); fill:var(--m-icon); }
.mult polyline.m-gfs, .mult polyline.m-ecmwf, .mult polyline.m-icon { fill:none; }
.mult .thr { stroke:var(--ink-faint); stroke-dasharray:3 3; stroke-width:1; }
.mult .base { stroke:var(--rule-strong); stroke-width:1; }
.mult .cross { stroke:var(--ink); stroke-width:1; opacity:0; pointer-events:none; }
.mult:hover .cross { opacity:.35; }
.mult .gap { fill:var(--sev-none-bg); }
.mult .gaplbl { font:11px 'HerosCn', sans-serif; letter-spacing:.1em; fill:var(--ink-faint); text-transform:uppercase; }
.axis { display:grid; grid-template-columns:150px 1fr; gap:14px; font:12px 'MonoInstr', monospace;
  color:var(--ink-faint); padding-top:6px; }
.axis div { display:flex; justify-content:space-between; }
#tip { position:fixed; pointer-events:none; background:var(--paper-raised); color:var(--ink);
  border:1px solid var(--rule-strong); box-shadow:var(--shadow); padding:9px 12px; font-size:13.5px;
  min-width:190px; opacity:0; transition:opacity .12s; z-index:9; }
#tip b { font:700 12px 'HerosCn', sans-serif; letter-spacing:.08em; display:block; margin-bottom:4px; }
#tip div { display:flex; justify-content:space-between; gap:14px; align-items:center; }
#tip i { width:14px; height:2px; display:inline-block; margin-right:6px; vertical-align:middle; }
#tip span.v { font-family:'MonoInstr', monospace; }
#tip small { color:var(--ink-faint); display:block; margin-top:4px; }
details { border-bottom:1px solid var(--rule); }
summary { list-style:none; cursor:pointer; display:grid; grid-template-columns:190px 1fr auto;
  gap:14px; align-items:center; padding:12px 0; }
summary::-webkit-details-marker { display:none; }
summary .dia { font:700 16px 'HerosCn', sans-serif; }
summary .dia::before { content:"▸"; color:var(--accent); display:inline-block; width:16px;
  transition:transform .2s cubic-bezier(.22,1,.36,1); }
details[open] summary .dia::before { transform:rotate(90deg); }
summary .res { color:var(--ink-soft); font-size:14px; }
details table { margin:0 0 16px 16px; width:calc(100% - 16px); }
.links a { font:13px 'MonoInstr', monospace; margin-right:10px; }
.muted { color:var(--ink-faint); }
.foot { margin-top:64px; padding-top:16px; border-top:2px solid var(--ink); color:var(--ink-soft);
  font-size:14px; max-width:72ch; }
.theme { float:right; background:none; border:1px solid var(--rule-strong); color:var(--ink-soft);
  font:11px 'HerosCn', sans-serif; letter-spacing:.12em; text-transform:uppercase; padding:5px 10px;
  cursor:pointer; }
@media (max-width:640px) {
  .row, .axis { grid-template-columns:1fr; gap:2px; }
  summary { grid-template-columns:1fr auto; } summary .res { display:none; }
  .hide-sm { display:none; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
"""

JS = """
(function(){
  const tip=document.getElementById('tip');
  const nombres={gfs:'GFS',ecmwf:'ECMWF',icon:'ICON'};
  const fmt=v=>v===null?'—':v.toFixed(2);
  document.querySelectorAll('svg.mult').forEach(svg=>{
    const pts=JSON.parse(svg.dataset.pts), cross=svg.querySelector('.cross');
    const dom=svg.closest('.row').dataset.label;
    function near(ev){
      const r=svg.getBoundingClientRect(), vx=(ev.clientX-r.left)*760/r.width;
      let b=null,bd=1e9; for(const p of pts){const d=Math.abs(p[0]-vx); if(d<bd){bd=d;b=p;}}
      return b;
    }
    svg.addEventListener('mousemove',ev=>{
      const p=near(ev); if(!p) return;
      cross.setAttribute('x1',p[0]); cross.setAttribute('x2',p[0]);
      const rows=['gfs','ecmwf','icon'].map((m,i)=>
        `<div><span><i style="background:var(--m-${m})"></i>${nombres[m]}</span><span class="v">${fmt(p[2+i])}</span></div>`).join('');
      const link=window.INFORMES[p[1]]?'<small>clic: abrir el registro de este ciclo</small>':'<small>sin registro para este ciclo</small>';
      tip.innerHTML=`<b>${dom} · ciclo ${p[1]}</b>${rows}${link}`;
      const x=Math.min(ev.clientX+16, window.innerWidth-tip.offsetWidth-8);
      tip.style.left=x+'px'; tip.style.top=(ev.clientY+14)+'px'; tip.style.opacity=1;
    });
    svg.addEventListener('mouseleave',()=>{tip.style.opacity=0; cross.setAttribute('x1',-10); cross.setAttribute('x2',-10);});
    svg.addEventListener('click',ev=>{const p=near(ev); const f=p&&window.INFORMES[p[1]]; if(f) window.open(f,'_blank');});
  });
  const btn=document.querySelector('.theme'), root=document.documentElement;
  try{const t=localStorage.getItem('nowcast-theme'); if(t) root.dataset.theme=t;}catch(e){}
  btn.addEventListener('click',()=>{
    const dark=root.dataset.theme?root.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme=dark?'light':'dark';
    try{localStorage.setItem('nowcast-theme',root.dataset.theme);}catch(e){}
  });
})();
"""


def fuentes_y_tokens():
    """Fuentes y tokens de color DEL REGISTRO: una sola fuente de verdad visual."""
    import build_registro as br
    b64 = lambda p: base64.b64encode(open(p, "rb").read()).decode()
    fonts = {n.replace("-", "_"): b64(f"{HERE}/fonts/{n}.woff2") for n in
             ("schola-reg", "schola-bold", "schola-ital", "heroscn-reg", "heroscn-bold",
              "mono-reg", "mono-bold")}
    tokens = br.CSS.split("* { box-sizing")[0]
    return br.FONT_FACES_TPL.format(**fonts), tokens


def construir(base, dias):
    ahora = datetime.now(timezone.utc)
    inf_dir = os.path.join(base, "informes")
    informes = informes_por_ciclo(inf_dir) if os.path.isdir(inf_dir) else {}
    desde = ahora - timedelta(days=dias)

    ciclos = []
    for d in sorted(glob.glob(os.path.join(base, "2026*"))):
        n = os.path.basename(d)
        if not CICLO_RE.match(n) or not os.path.isdir(d) or os.path.islink(d):
            continue
        t = t_ciclo(n)
        if t < desde:
            continue
        c = leer_ciclo(d)
        if c:
            c["nombre"], c["t"] = n, t
            ciclos.append(c)
    if not ciclos:
        raise SystemExit(f"[index] sin ciclos en {base} en los últimos {dias} días")
    ult = ciclos[-1]

    # --- servicios
    svc_html = []
    for s in servicios(base):
        e = s["edad_h"]
        if s["caido"] or (e is not None and e > s["umbral_h"]):
            dot, est = "bad", "DETENIDO"
        elif e is None:
            dot, est = "warn", "sin marca todavía"
        else:
            dot, est = "", "corriendo"
        cuando = "—" if e is None else (f"hace {e * 60:.0f} min" if e < 1 else f"hace {e:.1f} h")
        svc_html.append(f'<div><span class="lbl">{esc(s["nombre"])}</span>'
                        f'<div class="estado"><span class="dot {dot}"></span>{est}</div>'
                        f'<small>último ciclo completo {cuando}</small>'
                        f'<small>{esc(s["detalle"])}</small></div>')

    # --- último ciclo
    ecmwf_ultimo = next((c for c in reversed(ciclos)
                         if any(v.get("ecmwf") is not None for v in c["dom"].values())), None)
    filas = []
    for key, label in DOMINIOS:
        v = ult["dom"].get(key)
        if not v:
            continue
        filas.append(f'<tr><td>{esc(label)}</td>'
                     + "".join(f'<td class="num">{chip(v.get(m), corto=True)}</td>' for m, _ in MODELOS)
                     + f'<td class="num hide-sm">{cruce_txt(v.get("cruce"))}</td>'
                     f'<td class="num hide-sm">{"—" if v.get("iso0") is None else f"{v["iso0"]:.0f} m"}</td></tr>')
    reg = informes.get(ult["nombre"])
    reg_link = (f'<a href="{esc(reg["html"])}">registro del ciclo</a>'
                + (f' · <a href="{esc(reg["pdf"])}">PDF</a>' if reg.get("pdf") else "")) if reg else \
        '<span class="muted">registro aún no generado</span>'
    avisos = []
    if ecmwf_ultimo is not ult:
        cuando = ecmwf_ultimo["nombre"] if ecmwf_ultimo else "fuera de la ventana"
        avisos.append(f"ECMWF no aporta a este ciclo (último ciclo con ECMWF: {esc(cuando)}); "
                      "el peor escenario usa solo GFS e ICON.")
    watch = ", ".join(esc(w) for w in ult["watch"][:10]) + (f" y {len(ult['watch']) - 10} más" if len(ult["watch"]) > 10 else "")

    # --- línea de tiempo
    t0, t1 = desde, max(ult["t"], desde + timedelta(hours=6))
    ts = [t0] + [c["t"] for c in ciclos]
    huecos = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > timedelta(hours=24)]
    mults = []
    for key, label in DOMINIOS:
        serie = [(c["t"], c["dom"][key]) for c in ciclos if key in c["dom"]]
        if not serie:
            continue
        rotular = not mults      # el rótulo del hueco, solo en el primer panel
        mults.append(f'<div class="row" data-label="{esc(label)}"><span class="lbl">{esc(label)}</span>'
                     f'{svg_multiple(key, serie, t0, t1, huecos, rotular)}</div>')
    ticks = [t0 + (t1 - t0) * i / 4 for i in range(5)]
    eje = "".join(f"<span>{t.astimezone(SCL):%d-%m}</span>" for t in ticks)

    # --- bitácora por día
    por_dia = {}
    for c in ciclos:
        por_dia.setdefault(c["t"].astimezone(SCL).date(), []).append(c)
    dias_html = []
    for i, (dia, cs) in enumerate(sorted(por_dia.items(), reverse=True)):
        def peor(c, m):
            vals = [v.get(m) for v in c["dom"].values() if v.get(m) is not None]
            return max(vals) if vals else None
        peor_dia = max((p for c in cs for m, _ in MODELOS if (p := peor(c, m)) is not None), default=None)
        alto_dia = max((c["n_alto"] or 0) for c in cs)
        filas_d = []
        for c in sorted(cs, key=lambda c: c["t"], reverse=True):
            r = informes.get(c["nombre"])
            links = (f'<a href="{esc(r["html"])}">HTML</a>' + (f'<a href="{esc(r["pdf"])}">PDF</a>' if r.get("pdf") else "")) \
                if r else '<span class="muted">—</span>'
            top = max(c["dom"].items(), key=lambda kv: max((kv[1].get(m) or 0) for m, _ in MODELOS))
            filas_d.append(
                f'<tr><td class="num">{esc(c["nombre"])}<br><span class="muted">{hora_local(c["t"])}</span></td>'
                + "".join(f'<td class="num">{chip(peor(c, m), corto=True)}</td>' for m, _ in MODELOS)
                + f'<td class="hide-sm">{esc(dict(DOMINIOS).get(top[0], top[0]))}</td>'
                f'<td class="num">{"—" if c["n_alto"] is None else c["n_alto"]}</td>'
                f'<td class="links">{links}</td></tr>')
        dias_html.append(
            f'<details{" open" if i < 3 else ""}><summary><span class="dia">{fecha_larga(dia)}</span>'
            f'<span class="res">{len(cs)} ciclo(s) · hasta {alto_dia} comuna(s) en nivel alto</span>'
            f'{chip(peor_dia)}</summary><table><thead><tr><th>Ciclo (UTC · Chile)</th>'
            + "".join(f'<th class="num">{n}</th>' for _, n in MODELOS)
            + '<th class="hide-sm">Dominio con el pico</th><th class="num">Comunas alto</th>'
            '<th>Registro</th></tr></thead><tbody>' + "".join(filas_d) + "</tbody></table></details>")

    font_faces, tokens = fuentes_y_tokens()
    informes_js = {k: v["html"] for k, v in informes.items()}
    gen = f"{ahora:%Y-%m-%dT%H:%MZ}"
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bitácora del pronóstico</title>
<style>{font_faces}{tokens}{CSS}</style>
</head>
<body>
<div class="sheet">
<button class="theme" type="button">claro / oscuro</button>
<div class="kicker">Bitácora · sistema nowcast</div>
<h1>Qué está pronosticando el sistema</h1>
<p class="sub">Índice regenerado al cierre de cada ciclo. Cada cifra es el pico del índice de excedencia
del umbral lluvia–duración dentro de las 120 h de ese ciclo: un pronóstico, no una observación. Umbral
regional a = 5.5 para GFS y ECMWF, Caine global para ICON (su malla no diluye los picos; ver variantes.py).</p>
<div class="meta">
  <div><span class="lbl">Generado</span><span class="v">{gen} · {hora_local(ahora)}</span></div>
  <div><span class="lbl">Último ciclo</span><span class="v">{esc(ult["nombre"])}</span></div>
  <div><span class="lbl">Ventana</span><span class="v">{dias} días · {len(ciclos)} ciclos</span></div>
</div>

<h2><span class="n">01</span>Servicios</h2>
<p class="nota">Según las marcas que dejan los propios ciclos (<code>last_ok</code>) y el vigía.</p>
<div class="svc">{"".join(svc_html)}</div>

<h2><span class="n">02</span>Último ciclo · {esc(ult["nombre"])}</h2>
<p class="nota">{reg_link}. {("<strong>" + " ".join(avisos) + "</strong>") if avisos else ""}</p>
<table><thead><tr><th>Dominio</th>{"".join(f'<th class="num">{n}</th>' for _, n in MODELOS)}
<th class="num hide-sm">Primer cruce GFS (Chile)</th><th class="num hide-sm">Isoterma 0 °C</th></tr></thead>
<tbody>{"".join(filas)}</tbody></table>
<p class="nota">{ult["n_alto"] if ult["n_alto"] is not None else "—"} comunas en nivel alto y
{ult["n_medio"] if ult["n_medio"] is not None else "—"} en medio (cruce comunal grueso, peor de los modelos;
población censal total, no expuestos reales). Watchlist: {watch or "vacía"}.</p>

<h2><span class="n">03</span>Últimos {dias} días</h2>
<p class="nota">Un panel por dominio, eje 0–1. La línea discontinua es el nivel de alerta 0.5. Pasa el cursor
para ver los tres modelos en cada ciclo; clic abre su registro. Los cortes en una línea son ciclos sin ese modelo.</p>
<div class="legend">{"".join(f'<span><i style="background:var(--m-{m})"></i>{n}</span>' for m, n in MODELOS)}
<span><i class="thr"></i>nivel de alerta 0.5</span></div>
{"".join(mults)}
<div class="axis"><span></span><div>{eje}</div></div>

<h2><span class="n">04</span>Bitácora por día</h2>
<p class="nota">El peor valor entre dominios para cada modelo, con el registro de constancia del ciclo.</p>
{"".join(dias_html)}

<p class="foot">Sistema experimental en modo sombra. Nada de esta página es un aviso oficial: las fuentes de
verdad para decisiones de protección civil son SENAPRED, SERNAGEOMIN y la Dirección Meteorológica de Chile.
Horas de Chile con el huso vigente en cada fecha (America/Santiago).</p>
</div>
<div id="tip" role="status" aria-live="polite"></div>
<script>window.INFORMES={json.dumps(informes_js)};{JS}</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=os.path.expanduser("~/nowcast-forecast"))
    ap.add_argument("--dias", type=int, default=30)
    a = ap.parse_args()
    out_dir = os.path.join(a.base, "informes")
    os.makedirs(out_dir, exist_ok=True)
    pagina = construir(a.base, a.dias)
    out = os.path.join(out_dir, "index.html")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        f.write(pagina)
    os.replace(tmp, out)
    print(f"[index] {out} ({len(pagina) // 1024} KB)")


if __name__ == "__main__":
    main()
