#!/usr/bin/env python3
"""Alerta push (ntfy) para runouts de remoción en masa PLAUSIBLES sobre umbral.

Complementa el reporte HTML (que solo se lee cuando alguien lo abre): revisa
los `resumen.json` de las localidades de la watchlist vigente y dispara UNA
notificación por ciclo listando las que (a) cruzan el umbral de gatillo y (b)
pasan el guard de forma (`physically_plausible=true`, ver runout_localidad.py)
— las marcadas NO PLAUSIBLE nunca alertan aunque su número de gatillo sea alto,
porque esa forma ya se sabe que es un artefacto de terreno plano, no señal.

El aviso de dominio de calibración (`calibration_domain_warning`, ver mismo
módulo) NO filtra la alerta — se incluye como caveat en el cuerpo del mensaje,
porque silenciar TODO lo que está fuera de Atacama dejaría sin alerta a casi
toda la watchlist actual (18/20). Es información para el lector, no un gate.

Histéresis por localidad (mismo patrón que `monitor.sh`): una localidad que
ya alertó no vuelve a alertar antes de `--hysteresis-hours`, pero una
localidad NUEVA que empieza a calificar en este ciclo sí dispara de inmediato
aunque otras estén en histéresis. Si nadie califica (o todas están
suprimidas), no se manda nada — silencio, no aviso vacío.

Uso:
    python3 alert_runouts.py [--forecast ~/nowcast-forecast/latest]
        [--runouts ~/nowcast-forecast/runouts] [--alert-level 0.8]
        [--hysteresis-hours 6] [--state-dir ~/nowcast-forecast/state] [--dry-run]

Config de ntfy vía env (mismas variables que monitor.sh/config.env):
NTFY_URL (default https://ntfy.sh), NTFY_TOPIC (requerido para enviar de
verdad; sin él, --dry-run implícito con aviso).
"""

import argparse
import json
import os
import sys
import time
import urllib.request


def load_watchlist_meta(forecast_dir):
    """name -> (label, region, comuna)."""
    p = os.path.join(forecast_dir, "watchlist.json")
    wl = json.load(open(p))
    return {s["name"]: (s["label"], s["region"], s.get("comuna", s["label"])) for s in wl["selected"]}


def hysteresis_ok(state_dir, name, hours):
    """True si la localidad NO está suprimida (nunca alertó, o pasó la ventana)."""
    if hours <= 0:
        return True
    hfile = os.path.join(state_dir, f"alert_runout_{name}.ts")
    last = 0.0
    if os.path.exists(hfile):
        try:
            last = float(open(hfile).read().strip())
        except (ValueError, OSError):
            last = 0.0
    return (time.time() - last) >= hours * 3600


def mark_alerted(state_dir, name):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, f"alert_runout_{name}.ts"), "w") as f:
        f.write(str(time.time()))


def send_ntfy(title, body, priority, ntfy_url, ntfy_topic):
    req = urllib.request.Request(
        f"{ntfy_url}/{ntfy_topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default=os.path.expanduser("~/nowcast-forecast/latest"))
    ap.add_argument("--runouts", default=os.path.expanduser("~/nowcast-forecast/runouts"))
    ap.add_argument("--alert-level", type=float, default=0.8,
                     help="umbral de hazard_max_regional (mismo corte 'ALTO' del reporte)")
    ap.add_argument("--hysteresis-hours", type=float, default=6.0)
    ap.add_argument("--state-dir", default=os.path.expanduser("~/nowcast-forecast/state"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    forecast_dir = os.path.realpath(os.path.expanduser(a.forecast))
    meta = load_watchlist_meta(forecast_dir)

    candidates = []
    for name, (label, region, comuna) in meta.items():
        p = os.path.join(a.runouts, name, "resumen.json")
        if not os.path.exists(p):
            continue
        r = json.load(open(p))
        g = r.get("signal", {}).get("hazard_max_regional")
        if g is None or g < a.alert_level:
            continue
        if not r.get("physically_plausible", False):
            continue
        candidates.append((g, name, label, region, r))

    if not candidates:
        print("sin localidades plausibles sobre el umbral — silencio, no se envía nada")
        return

    candidates.sort(key=lambda x: -x[0])
    fresh = [c for c in candidates if hysteresis_ok(a.state_dir, c[1], a.hysteresis_hours)]
    if not fresh:
        print(f"{len(candidates)} localidad(es) califican pero todas en histéresis "
              f"(<{a.hysteresis_hours} h desde la última alerta) — no se envía nada")
        return

    lines = []
    for g, name, label, region, r in fresh:
        dom = " · sin calibrar aquí (ver reporte)" if r.get("calibration_domain_warning") else ""
        lines.append(f"• {label} ({region}): gatillo {g:.2f}{dom}")
    body = ("Runouts plausibles sobre umbral (escenario ilustrativo, NO aviso oficial):\n"
            + "\n".join(lines))
    title = f"nowcast: {len(fresh)} localidad(es) con runout plausible sobre {a.alert_level}"

    print(title)
    print(body)

    ntfy_topic = os.environ.get("NTFY_TOPIC", "")
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh")
    # Hermana de notify() en monitor.sh: la marca de histéresis se estampa SOLO
    # tras una entrega confirmada (12-ago-2026). Acá el orden invertido pegaba
    # dos veces y más fuerte que en monitor.sh: (a) un --dry-run marcaba las
    # localidades como alertadas, o sea una corrida en seco suprimía las
    # alertas REALES de las horas siguientes —lo contrario de lo que significa
    # "en seco"—; y (b) con el envío fallido se marcaba igual, convirtiendo un
    # fallo de red en silencio. Ahora nada de eso muta el estado: solo el envío
    # que devolvió OK lo hace.
    if a.dry_run or not ntfy_topic:
        if not ntfy_topic:
            print("(NTFY_TOPIC no configurado — no se envía y NO se marca: "
                  "el próximo ciclo vuelve a intentar)", file=sys.stderr)
        else:
            print("(--dry-run: no se envía y NO se marca)", file=sys.stderr)
        return

    try:
        send_ntfy(title, body, "default", ntfy_url, ntfy_topic)
    except Exception as e:
        print(f"WARN: envío a ntfy falló ({e}) — NO se marca, se reintenta el próximo ciclo",
              file=sys.stderr)
        return
    for _, name, *_ in fresh:
        mark_alerted(a.state_dir, name)


if __name__ == "__main__":
    main()
