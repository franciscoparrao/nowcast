#!/usr/bin/env python3
"""Trae la serie diaria de isoterma cero (H0) desde `agromet-scraper` a `data/`.

La serie se construye en el otro repo por regresión robusta de temperatura
contra elevación sobre la red INIA (~480 estaciones), y está validada contra
3.301 sondeos 12Z de Santo Domingo (IGRA v2): en hold-out temporal 2023-2026,
sesgo +248 m y MAE 362 m en el caso operacional, r=0.85.

Este script NO recalcula nada: copia con validación, para que el motor consuma
un archivo estable en `data/` (gitignored, como el resto de los derivados) y no
un path que cruza repos.

LÍMITES QUE VIAJAN CON EL DATO (y que el motor debe respetar):
  · Cubre 30-36°S. Atacama y Aysén necesitan su propio ajuste; en el sur la red
    no tiene gradiente vertical suficiente.
  · Tiene HUECOS DELIBERADOS: solo están los días que pasan los criterios
    físicos (>=8 estaciones sobre 800 m, span de elevación 1.500 m, R2 > 0.5,
    lapse rate < -4 °C/km). Un día ausente significa "no sé", nunca "isoterma
    baja".
  · Sesgo residual +250..+450 m según el estrato: los umbrales absolutos se
    fijan sobre la escala de ESTA serie, no sobre valores de radiosonda.
  · Hora de referencia 08:00 en UTC-4 fijo.

Uso:
    python3 scripts/extract_h0_transecta.py
"""

import csv
import shutil
import sys
from collections import Counter
from pathlib import Path

SRC = Path.home() / "proyectos/agromet-scraper/data/h0_centro_diaria_v3.csv"
DST = Path(__file__).resolve().parent.parent / "data" / "h0_centro_diaria.csv"
REQUIRED = {"fecha", "h0_m", "tormenta"}

# Eventos mayores con fecha compilada de fuentes públicas (CR2, decretos, prensa)
# y validados contra la propia red. Son 6 y de calidad heterogénea: tres con foco
# solo PARCIALMENTE en la transecta y uno (jul-2026) con confianza_fecha baja
# porque la fuente es un pronóstico, no un reporte post-evento. Alcanzan para
# descartar configuraciones malas, no para calibrar finamente.
SRC_EV = Path.home() / "proyectos/agromet-scraper/data/eventos_mayores_2018_2026.csv"
DST_EV = Path(__file__).resolve().parent.parent / "data" / "eventos_mayores.csv"


def main():
    if not SRC.exists():
        print(f"no existe la serie de origen: {SRC}\n"
              "Se genera en agromet-scraper con analisis/isoterma_cero_v3.py",
              file=sys.stderr)
        return 1

    with SRC.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{SRC} está vacío", file=sys.stderr)
        return 1

    faltan = REQUIRED - set(rows[0])
    if faltan:
        print(f"a {SRC} le faltan columnas requeridas: {sorted(faltan)}\n"
              f"tiene: {sorted(rows[0])}", file=sys.stderr)
        return 1

    # Chequeo de cordura: la serie debe tener huecos (si no, el criterio físico
    # no se aplicó) y los valores deben ser plausibles para una isoterma.
    h0 = [float(r["h0_m"]) for r in rows if r["h0_m"]]
    if not h0:
        print("ninguna fila trae h0_m numérico", file=sys.stderr)
        return 1
    fuera = [v for v in h0 if not (500 <= v <= 7000)]
    if fuera:
        print(f"AVISO: {len(fuera)} valores fuera de [500, 7000] m "
              f"(ej. {fuera[:3]}) — revisar el ajuste río arriba", file=sys.stderr)

    fechas = sorted(r["fecha"] for r in rows)
    por_mes = Counter(f[5:7] for f in fechas)
    tormentas = sum(1 for r in rows if r.get("tormenta") == "1")

    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)

    print(f"{DST.relative_to(DST.parent.parent)}: {len(rows)} días "
          f"({fechas[0]} .. {fechas[-1]}), {tormentas} de tormenta")
    print(f"  H0 min/máx: {min(h0):.0f} / {max(h0):.0f} m")
    print("  días por mes: " + " ".join(f"{m}:{n}" for m, n in sorted(por_mes.items())))
    print("  RECORDAR: los meses de verano tienen pocos días de TORMENTA, así que "
          "su p90\n  mensual queda indefinido — la vía absoluta es la que cubre "
          "esos meses.")

    if SRC_EV.exists():
        shutil.copyfile(SRC_EV, DST_EV)
        with SRC_EV.open() as f:
            ev = list(csv.DictReader(f))
        parcial = sum(1 for e in ev if e.get("en_transecta") != "si")
        baja = sum(1 for e in ev if e.get("confianza_fecha") == "baja")
        print(f"\n{DST_EV.relative_to(DST_EV.parent.parent)}: {len(ev)} eventos fechados "
              f"({parcial} con foco solo parcial en la transecta, {baja} con "
              f"confianza de fecha baja)")
        print("  OJO: n=6 y heterogéneos. Sirven para descartar configuraciones malas,\n"
              "  no para calibrar un parámetro con precisión.")
    else:
        print(f"\nAVISO: no está {SRC_EV} — el barrido de persistencia no podrá correr",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
