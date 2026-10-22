# Paquete de calibración: eventos y no-eventos (RM, O'Higgins, Valparaíso)

Generado por `nowcast/scripts/calibracion_runout/armar_paquete.py` el 2026-10-21.
Para calibrar el inicio de flujos del runout ABM de swarm-abm (arreglo 3 de la
lista acordada) y el umbral I-D de nowcast. Regenerable: el script lee el catálogo
de SERNAGEOMIN y la base de agromet-scraper; ningún número está escrito a mano.

## Qué hay

| Archivo | Contenido |
|---|---|
| `eventos.geojson` | 214 flujos/remociones del catálogo nacional de SERNAGEOMIN fechados al **día**, desde nov-2016 (inicio de agromet), en RM, O'Higgins y Valparaíso. `fecha_ini`/`fecha_fin` normalizadas desde `DIA_EVENTO` ("30 al 31", "29 y 30"…). 204 con detonante «Lluvias». |
| `eventos_cobertura.csv` | Por evento: estación más cercana, distancia, lámina máxima en `[ini−1, fin+1]` y estado (con lluvia medida / lluvia < 1 mm / sin dato / sin estación a 15 km). |
| `estaciones.csv` | 85 estaciones con precipitación en la zona (INIA, DMC, MMA-DMC y otras), con su cobertura. |
| `lluvia_diaria.csv` | Precipitación diaria por estación, nov-2016 → oct-2026. |
| `lluvia_horaria.csv` | Precipitación horaria solo en ventanas de ±3 días alrededor de cada evento, para estaciones a ≤ 30 km. |
| `dias_etiquetados.csv` | Cada día con ≥ 1 mm en cada estación: lámina del día, lámina de 3 días, eventos del catálogo a ≤ 15 km y en `[ini−1, fin+1]`, y etiqueta `evento` / `no_evento`. |
| `resumen.json` | Conteos. |

## Lo que el paquete NO tiene (leer antes de calibrar)

- **Pocos eventos con lluvia medida.** De los 214 eventos, solo **56** tienen una
  estación a ≤ 15 km con lluvia registrada en su fecha, y salen de **tres
  temporales**: 29–31 ene 2021 (34), 22–24 jun 2023 (21) y 26 abr 2022 (1). En
  `dias_etiquetados.csv` eso son **12 días-estación `evento`** contra 11.785
  `no_evento`. Cualquier umbral que salga de aquí está sostenido por tres temporales.
- **118 eventos no tienen estación a 15 km** (la red es de valle; la cordillera
  casi no tiene pluviómetros) y **35 caen en estaciones sin dato** ese día —
  entre ellos los 34 flujos del 24–26 feb 2017 en el Cajón del Maipo y
  Valparaíso, anteriores a la serie de las estaciones cercanas.
- **5 eventos ocurrieron con < 1 mm en la estación más cercana** (ene y jun 2023):
  chubascos convectivos que la red no ve, el mismo mecanismo del aluvión de Las
  Condes del 8-oct-2026. No están en `dias_etiquetados.csv` como evento; están en
  `eventos_cobertura.csv`. **No usarlos como evidencia de que lluvia baja basta.**
- **`no_evento` significa "sin flujo reportado", no "sin flujo".** El catálogo
  es incompleto y su completitud depende de dónde hubo daño y quién fue a mirar.
  Tratarlo como aprendizaje positivo-sin etiqueta (PU) o ponderar; ver el
  proyecto `landslide-inventory-bias`.

## Convenciones (supuestos, no verificados para todas las estaciones)

- **Día de agromet = día en UTC−4 fijo**, todo el año (verificado para El
  Colorado EXT-108; se asume igual para el resto de la base).
- **Fecha del catálogo = día civil local** (no lo documenta el catálogo).
- La ventana `[ini−1, fin+1]` absorbe el desfase entre ambas convenciones y el
  error de fecha del catálogo.
- Coordenadas en WGS84; distancias equirectangulares corregidas por latitud.

## Para extender

La forma de recuperar los eventos sin estación es una forzante grillada (IMERG
Final semihoraria desde 2000, ya usada en el paper de nowcast con
`scripts/extract_event_imerg.py`), con su sesgo conocido: IMERG pierde los
chubascos convectivos de piedemonte.
