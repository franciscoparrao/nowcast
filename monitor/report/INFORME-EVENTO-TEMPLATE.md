# Evento hidrometeorológico del 15–20 de julio de 2026 — informe del monitor `nowcast`

> **Plantilla.** Rellenar tras el evento con `collect_event.sh` + `replay_event.py`.
> Las secciones marcadas ⬜ requieren datos externos (reportes oficiales/prensa).
> Regla de la casa: cifras con procedencia, hallazgos negativos incluidos.

## 1. Resumen ejecutivo

- Qué se monitoreó: 3 dominios (RM/Cajón del Maipo, Ñuble–Biobío, Araucanía),
  fusión noisy-OR de IMERG Early (~4-5 h) ⊕ GOES-East QPE (~min) ⊕
  pluviómetros DMC (~20-60 min), umbral I-D Caine, susceptibilidad uniforme
  (**monitor de timing**, no de "dónde" — declararlo siempre).
- ⬜ N cruces de umbral en M dominios; primer cruce ______ UTC; lead time
  mediano vs reportes oficiales ______ h.
- ⬜ Veredicto en una frase (honesto: aciertos, falsas alarmas, huecos).

## 2. El evento

⬜ Sinóptica breve (los dos sistemas frontales + ríos atmosféricos, fechas,
regiones; citar pronóstico previo y montos observados). Mapa de contexto.

## 3. Configuración del sistema (congelada antes del evento)

| Dominio | BBox | Grilla | Estaciones in-situ | Alert level |
|---|---|---|---|---|
| rm | -70.75,-34.25,-69.75,-33.25 | 11×11 0.1° | El Colorado, Guayacán, La Florida | 0.5 |
| nuble-biobio | -72.05,-38.25,-71.05,-36.25 | 21×11 | Chillán Ad., Mayulermo | 0.5 |
| araucania | -72.35,-39.95,-71.05,-38.25 | 17×13 | Lonquimay, Pucón, Villarrica, Panguipulli | 0.5 |

Parámetros: Caine global (a=14.82, b=0.39), k=4, ventana 48 h, paso 30 min.
Software: commit ______ (congelado el 2026-07-12). Hardware: 1 nodo doméstico
(Celeron, 3.6 GB RAM) — dejar dicho: el cómputo es trivial, el valor está en
los datos y la arquitectura.

## 4. Resultados por dominio

Para cada dominio (de `replay/timeline.csv` y `crossings.json`):

### 4.x <dominio>
- Figura: max_prob vs tiempo (30 min) con umbral y cruces sombreados;
  segunda banda: lluvia por feed (mm/30 min, promedio del dominio).
- Tabla de cruces: inicio, fin, duración, pico, celdas.
- ⬜ Qué feed lo vio primero (replay `--primary-only` vs fusionado: el delta
  ES la ganancia de latencia de la fase A, medida en un evento real).

## 5. Lead times contra el mundo real ⬜

La tabla que importa. Una fila por evento reportado (SERNAGEOMIN, SENAPRED,
municipios, prensa con hora):

| Evento reportado | Fuente y hora del reporte | Dominio/celdas | Cruce del monitor | Lead (h) |
|---|---|---|---|---|

Incluir también la columna inversa: cruces del monitor SIN evento reportado
(falsas alarmas aparentes — o eventos no reportados: discutir ambigüedad).

## 6. Trazabilidad de las alertas (XAI)

Por cada cruce relevante: `nowcast explain` sobre la celda pico — ventana I-D
dominante (duración, intensidad, exceedancia), driver, y el contrafactual
(`intensity_to_alert`). El argumento de venta: cada alerta es una fórmula
cerrada inspeccionable, no una caja negra.

## 7. Evaluación honesta ⬜

- POD / FAR / lead sobre el evento (n será chico: decirlo).
- Qué se perdió y por qué (¿cordillera de Ñuble sin gauge? ¿sesgo GOES QPE en
  lluvia orográfica templada? ¿mes/fecha de reportes ambiguos?).
- Falsas alarmas estructurales del modo timing (susceptibilidad uniforme).
- Salud operacional: uptime de feeds, huecos, staleness (de status/history).

## 8. Primera calibración con positivos reales ⬜

Isotónica sobre (índice, evento±tolerancia) del evento + backtest Maipo
histórico. Diagrama de fiabilidad antes/después, Brier/ECE. Desde aquí las
alertas pueden hablar en probabilidad.

## 9. Limitaciones y siguiente paso

Timing-only (RF pendiente), umbral global no regional, un evento no valida
un sistema (el modo sombra sigue todo el invierno), QPE satelital ≠ radar.
Fase B: forzante pronosticada (pySTEPS/GOES → `ensemble_hazard`) = lead
positivo.

## Apéndice A — reproducibilidad

```bash
./collect_event.sh sentinel ./event-data
python3 ../replay_event.py --data ./event-data/rm --out ./event-data/rm/replay \
    --nowcast-bin ../../target/release/nowcast --hazard-tifs
# contrafactual sin fusión (la ganancia de la fase A):
python3 ../replay_event.py --data ./event-data/rm --out /tmp/rm-solo --primary-only
```
