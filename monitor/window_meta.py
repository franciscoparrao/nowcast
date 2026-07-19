#!/usr/bin/env python3
"""Ventana temporal cubierta por un ciclo de pronóstico.

Módulo aparte y sin dependencias pesadas a propósito: lo importan los TRES
fetchers (`forecast_gfs.py`, `fetch_ecmwf.py`, `fetch_icon.py`), y meter esto
en `susc_overlay.py` —el otro sitio compartido— crearía una dependencia de la
capa de descarga hacia la de análisis, que es al revés de como fluye el
pipeline. Duplicarlo en los tres sería peor: es exactamente el patrón de
"se arregló en uno y quedó la hermana" que el checklist del repo advierte.

Por qué existe (hallazgo 2026-07-19): las tres fuentes NO cubren el mismo
periodo y nada lo decía. DWD publica ICON progresivamente y el fetcher toma
los lead times disponibles al momento de correr, así que su acumulado puede
cubrir 24 h mientras GFS cubre 119. Comparar esos totales no significa nada
—ICON se ve ~5× más seco por construcción— y el problema es INTERMITENTE:
un ciclo alcanzaba 92 pasos nativos y era comparable, el siguiente 25 y no.
Sin la ventana escrita en el metadata, aguas abajo no hay forma de saberlo.
"""

from datetime import datetime

STAMP_FMT = "%Y%m%dT%H%M"


def ventana_desde_keys(keys, step_hours=None):
    """Ventana cubierta por una lista de claves de paso ('20260719T0100').

    Devuelve n_pasos, desde, hasta y horas. `horas` es el LAPSO entre el
    primer y el último paso; con `step_hours` se suma el propio ancho del
    primer paso para reflejar el periodo total acumulado, que es lo que
    representa un `rain_liquid_total`.
    """
    if not keys:
        return None
    ordenadas = sorted(str(k) for k in keys)
    t0, t1 = ordenadas[0], ordenadas[-1]
    horas = None
    try:
        lapso = (datetime.strptime(t1, STAMP_FMT)
                 - datetime.strptime(t0, STAMP_FMT)).total_seconds() / 3600.0
        horas = lapso + (float(step_hours) if step_hours else 0.0)
    except ValueError:
        pass
    return {"n_pasos": len(ordenadas), "desde": t0, "hasta": t1,
            "horas": round(horas, 1) if horas is not None else None,
            "nota": ("periodo que realmente cubre el acumulado de esta fuente; "
                     "NO comparar totales entre fuentes con ventanas distintas")}
