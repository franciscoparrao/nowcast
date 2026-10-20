#!/usr/bin/env python3
"""Lluvia medida por las 9 EMAs DMC de los dominios RM y O'Higgins el 20-oct-2026,
desde el minutario archivado por archive_ema.py (copia filtrada en
evidencia/ema/<code>-20261020.jsonl: momento en UTC, lámina por minuto).

Para cada estación: total del día UTC, lámina por hora (hora Chile = UTC−3) y el
máximo de la lámina en ventanas móviles de 30 min, 1 h y 3 h, comparado con la
curva I-D regional a*=5.5 (b=0.39) y con Caine global (a=14.82), los dos
umbrales que usan los monitores (rm y ohiggins: 5.5 en sombra; atacama: Caine).
E = I / I_crit(D): E ≥ 1 es estar sobre la curva en ese punto."""
import json, os, re
from datetime import datetime, timedelta

E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidencia", "ema")
EST = [("330019", "Tobalaba Ad.", "RM", "La Reina", 650), ("330122", "La Florida (Aguas Andinas)", "RM", "La Florida", 790),
       ("330112", "San José de Guayacán", "RM", "San José de Maipo", 960), ("330077", "El Colorado", "RM", "Lo Barnechea", 2750),
       ("340045", "Rancagua", "VI", "Rancagua", 482), ("340048", "Quimávida", "VI", "Coltauco", 316),
       ("340093", "Portezuelo-Codelco", "VI", "Machalí", 1185), ("340066", "Nilahue-La Quebrada", "VI", "Pumanque", 81),
       ("340115", "Pichilemu Ad.", "VI", "Pichilemu", 25)]
B = 0.39
CURVAS = {"a*=5.5": 5.5, "Caine": 14.82}
DESDE, HASTA = datetime(2026, 10, 20, 0, 0), datetime(2026, 10, 20, 19, 0)  # hasta el último dato común

print(f"ventana: {DESDE:%Y-%m-%d %H:%M} → {HASTA:%H:%M} UTC (hora Chile = UTC−3)")
print(f"{'estación':28s} {'comuna':18s} {'m':>5s} {'total':>6s} {'máx 30 min':>11s} {'máx 1 h':>16s} {'máx 3 h':>9s}  E(a*=5.5) E(Caine)")
for code, nom, reg, com, alt in EST:
    mm = {}
    for l in open(os.path.join(E, f"{code}-20261020.jsonl")):
        r = json.loads(l)
        t = datetime.strptime(r["momento"], "%Y-%m-%d %H:%M")
        m = re.findall(r"[\d.]+", str(r.get("aguaCaidaDelMinuto")))
        if DESDE <= t < HASTA and m:
            mm[t] = float(m[0])
    n_min = int((HASTA - DESDE).total_seconds() // 60)
    serie = [mm.get(DESDE + timedelta(minutes=i), 0.0) for i in range(n_min)]
    cober = len(mm) / n_min

    def maxwin(w):
        s = sum(serie[:w]); best, t_best = s, 0
        for i in range(w, n_min):
            s += serie[i] - serie[i - w]
            if s > best:
                best, t_best = s, i - w + 1
        return best, DESDE + timedelta(minutes=t_best)

    res = {D: maxwin(int(D * 60)) for D in (0.5, 1, 3)}
    e = {k: max(res[D][0] / D / (a * D ** -B) for D in res) for k, a in CURVAS.items()}
    m1, t1 = res[1]
    hora = f"{(t1 - timedelta(hours=3)):%H:%M} Chile" if m1 > 0 else "   —       "
    print(f"{nom:28s} {com:18s} {alt:5d} {sum(serie):6.1f} {res[0.5][0]:6.1f} mm   {m1:5.1f} mm {hora}"
          f" {res[3][0]:6.1f} mm  {e['a*=5.5']:5.2f}     {e['Caine']:5.2f}   (cobertura {cober:.0%})")
    horas = {}
    for t, v in mm.items():
        horas[t.replace(minute=0)] = horas.get(t.replace(minute=0), 0) + v
    lluvia = {f"{(h - timedelta(hours=3)):%H}h": round(v, 1) for h, v in sorted(horas.items()) if v >= 0.5}
    print(f"{'':28s} horas Chile con ≥0.5 mm: {lluvia if lluvia else '—'}")
print("\nI_crit(D) = a·D^−0.39 → a*=5.5: 7.2 mm/h a 30 min, 5.5 a 1 h, 3.6 a 3 h; Caine: 19.4 / 14.8 / 9.7 mm/h")
