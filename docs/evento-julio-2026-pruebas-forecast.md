# Pruebas de pronostico para evento 16-19 julio 2026

Fecha de prueba: 2026-07-15

## Objetivo

Probar si una tercera fuente gratuita de mayor resolucion, DWD ICON global
~13 km, mejora la discrepancia detectada entre GFS/ECMWF 0.25 deg y los montos
DMC para Atacama, especialmente Vallenar, Freirina y Huasco.

## Corridas ejecutadas

- GFS: `20260715T12z`, generado con `monitor/forecast_gfs.py` en
  `/tmp/nowcast-gfs-event`.
- ECMWF Open Data: `20260715T00z`, generado con `monitor/fetch_ecmwf.py` en
  `/tmp/nowcast-gfs-event`. El ciclo 12z aun no estaba disponible.
- ICON global DWD: `20260715T12z`, generado con `monitor/fetch_icon.py` en
  `/tmp/nowcast-icon-event`.

Comandos base:

```bash
python3 monitor/forecast_gfs.py \
  --workdir /tmp/nowcast-gfs-event \
  --nowcast-bin target/release/nowcast \
  --domain atacama:-71.4,-29.8,-68.9,-25.8 \
  --domain coquimbo:-71.8,-32.25,-69.75,-29.25

python3 monitor/fetch_ecmwf.py \
  --workdir /tmp/nowcast-gfs-event \
  --nowcast-bin target/release/nowcast \
  --domain atacama --domain coquimbo --hours 120

python3 monitor/fetch_icon.py \
  --workdir /tmp/nowcast-icon-event \
  --nowcast-bin target/release/nowcast \
  --domain atacama --domain coquimbo --hours 120
```

## Resultado por dominio

GFS 12z sigue casi sin gatillo en el norte:

- Atacama: pico regional-a5.5 = 0.08.
- Coquimbo: pico regional-a5.5 = 0.25.

ECMWF 00z e ICON 12z gatillan fuerte:

- ECMWF Atacama: pico regional-a5.5 = 1.0.
- ECMWF Coquimbo: pico regional-a5.5 = 1.0.
- ICON Atacama: pico regional-a5.5 = 1.0.
- ICON Coquimbo: pico regional-a5.5 = 1.0.

La prueba encontro problemas transitorios de red:

- ICON: dos `SSL: UNEXPECTED_EOF_WHILE_READING` en `f052` y `f055`.
- ECMWF: tres `HTTP 503 Slow Down` en `f108`, `f114`, `f117`.

Se agregaron retries con backoff y escritura temporal en los fetchers para no
dejar caches parciales.

## Muestreo por localidad

Valores en mm. Las coordenadas son centros urbanos aproximados; como la grilla
interna es 0.1 deg, el diagnostico debe leerse por orden de magnitud y junto a
los maximos 3x3/5x5.

| Localidad | Modelo | Ciclo | Total punto | Total 3x3 | Total 5x5 | Max 24h punto | Max 48h punto | Max 48h 3x3 | Hazard punto | Hazard 3x3 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Vallenar | GFS | 20260715T12z | 3.4 | 5.0 | 6.6 | 1.6 | 2.6 | 4.2 | 0.025 | 0.039 |
| Vallenar | ECMWF | 20260715T00z | 20.6 | 22.7 | 24.1 | 20.6 | 20.6 | 24.9 | 0.847 | 0.901 |
| Vallenar | ICON | 20260715T12z | 137.1 | 204.2 | 257.8 | 60.8 | 114.5 | 183.1 | 0.980 | 0.999 |
| Freirina | GFS | 20260715T12z | 4.9 | 7.3 | 9.8 | 4.5 | 4.8 | 7.4 | 0.032 | 0.042 |
| Freirina | ECMWF | 20260715T00z | 14.7 | 19.4 | 26.1 | 14.4 | 14.5 | 19.8 | 0.446 | 0.662 |
| Freirina | ICON | 20260715T12z | 175.8 | 229.6 | 229.6 | 86.0 | 138.6 | 191.8 | 0.998 | 1.000 |
| Huasco | GFS | 20260715T12z | 9.8 | 12.1 | 12.1 | 8.7 | 9.8 | 13.4 | 0.054 | 0.076 |
| Huasco | ECMWF | 20260715T00z | 21.5 | 26.1 | 31.7 | 20.1 | 20.8 | 26.9 | 0.746 | 0.869 |
| Huasco | ICON | 20260715T12z | 94.3 | 122.9 | 229.6 | 53.3 | 73.6 | 109.4 | 0.868 | 0.966 |
| Copiapo | GFS | 20260715T12z | 7.6 | 7.7 | 7.7 | 3.7 | 6.0 | 6.6 | 0.067 | 0.071 |
| Copiapo | ECMWF | 20260715T00z | 46.6 | 57.7 | 80.0 | 25.2 | 31.3 | 45.9 | 0.897 | 0.989 |
| Copiapo | ICON | 20260715T12z | 33.5 | 58.1 | 82.7 | 22.4 | 32.9 | 63.4 | 0.292 | 0.880 |
| Caldera | GFS | 20260715T12z | 3.5 | 3.6 | 5.0 | 1.8 | 1.9 | 3.1 | 0.025 | 0.028 |
| Caldera | ECMWF | 20260715T00z | 126.7 | 152.2 | 177.5 | 57.4 | 91.7 | 127.6 | 1.000 | 1.000 |
| Caldera | ICON | 20260715T12z | 34.4 | 47.8 | 48.1 | 24.7 | 34.2 | 60.7 | 0.586 | 0.838 |
| La Higuera | GFS | 20260715T12z | 9.8 | 12.1 | 14.5 | 8.7 | 9.8 | 13.4 | 0.054 | 0.076 |
| La Higuera | ECMWF | 20260715T00z | 222.5 | 228.6 | 234.7 | 103.6 | 162.4 | 185.8 | 0.999 | 0.999 |
| La Higuera | ICON | 20260715T12z | 155.9 | 167.0 | 228.9 | 73.1 | 120.4 | 151.9 | 0.992 | 0.993 |

## Lectura operacional

ICON corrige precisamente el punto que motivaba la investigacion: sobre el
valle del Huasco, GFS 12z mantiene 3-10 mm totales, ECMWF 00z sube a 15-22 mm,
e ICON 12z da 94-176 mm en el punto y 123-230 mm en el entorno 3x3. Sus
maximos 48 h son del mismo orden que las cifras DMC para Vallenar/Freirina/
Huasco.

La mejora no es uniforme. En Caldera, ECMWF 00z es mucho mas agresivo que ICON,
y en Copiapo ICON queda intermedio. Esto refuerza que el siguiente paso no debe
ser reemplazar GFS por ICON, sino usar un ensamble multi-fuente:

- GFS como miembro bajo en este evento.
- ECMWF como miembro europeo 0.25 deg.
- ICON como miembro global de mayor resolucion.
- Diagnostico por punto + maximos 3x3/5x5 para no depender del pixel exacto.

## Proximo paso recomendado

Agregar `monitor/fetch_icon.py` al flujo operacional de forecast y al reporte
HTML como tercera fuente. Para el evento 16-19 julio 2026, incluir una nota
explicita: GFS subestima el valle del Huasco, ECMWF queda moderado, ICON alinea
mejor con los montos DMC pero debe leerse como escenario experimental.
