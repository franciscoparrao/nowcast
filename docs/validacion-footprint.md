# Validación del footprint del ABM de flujo detrítico

Estado de verificación del footprint de `runout_localidad.py` (ABM `debris-flow`
sobre `swarm-abm`, presets en `swarm-abm/models/debris-flow/data/`), y el asset
que abre la validación **fuera de Atacama**.

## Lo que SÍ está verificado (Atacama)

El footprint no está "sin verificar": tiene una validación out-of-sample real
dentro de Atacama.

| Caso | IoU | Naturaleza |
|---|---|---|
| Copiapó 2015 (preset `de`) | 0.171 | in-sample (el evento de calibración; ajuste flojo) |
| **Chañaral (preset `chanaral_enhanced`)** | **0.555 medio / 0.587 máx** | **out-of-sample (semillas frescas)** |

La cifra que importa es la de Chañaral: **IoU > 0.5 fuera de la muestra de
ajuste**, buena para un footprint de flujo detrítico. La maquinaria de IoU vive
en `swarm-abm/models/debris-flow` (`load` + `evaluate_masked(...).iou`, DE en
`src/bin/calibrate_chanaral.rs`). Ver `CALIBRATION.md` de ese repo.

## La brecha real: fuera de Atacama

Lo genuinamente no verificado es la **escala/forma fuera de Atacama**: los
presets se ajustaron sobre eventos de Atacama (abanico aluvial abrupto), y para
las cuencas modeladas del centro-sur (Rapel, Maule, Biobío, Araucanía) —que sí
tienen susceptibilidad ML y por tanto sí corren el ABM operacionalmente— **no
había un footprint mapeado de un evento documentado contra el cual validar**.
Por eso toda corrida fuera de `atacama` lleva `calibration_domain_warning=True`
(ver `runout_localidad.py`).

## Asset nuevo: Villa Santa Lucía (EMSN053) — primer ground truth out-of-Atacama

`data/EMSN053_WGS84_V2.gdb_.zip` (Copernicus EMS, activación EMSN053) mapea el
flujo detrítico de **Villa Santa Lucía** (Chaitén, Los Lagos, 2017-12-16, ~22
muertos) — lat −43.4, lon −72.4, **muy al sur del dominio de calibración**. La
capa `P03_FLOODRISK01MUDFLOWDAMAGEASS` es el footprint observado, extraído a
`data/vsl_mudflow_obs.geojson`.

- **Área observada: 4.9 km²**, alargado y canalizado (cae de 1457 m a 221 m).

## Resultado de la validación (2026-07-21)

Reproducible con `scripts/validate_footprint_vsl.py`. Como no hay
susceptibilidad ML en VSL (fuera de toda cuenca modelada — el ABM operacional ni
corre ahí: `runout_localidad.py` haría `sys.exit` "SIN COBERTURA"), se fabrica
una susceptibilidad de **fuente**: un disco de 150 m en el apex del footprint
observado (su celda más alta sobre el DEM Copernicus GLO-30 que `runout_ondemand`
baja on-demand). El preset `chanaral_enhanced` (`seeding_power=0.376`) concentra
los agentes en la fuente; el terreno rutea el resto. Isoterma alta (verano
austral, toda lluvia líquida). Es un test de **ruteo/escala dado un origen
conocido**, no de la config operacional susc-driven.

| Métrica | ABM (chanaral_enhanced, 6 semillas) | Observado |
|---|---|---|
| Área footprint | **3.98 km²** (± 0.00) | 4.93 km² |
| IoU | **0.181** | — |
| Precisión | 0.343 | — |
| Recall | 0.277 | — |

Lectura honesta:

- **La ESCALA generaliza fuera de Atacama**: 3.98 vs 4.93 km² (~81 %). Cuando se
  siembra en la fuente, el footprint NO sale sobredimensionado — al contrario,
  queda algo por debajo del observado. Desmiente la lectura ingenua de "todo
  footprint out-of-domain está inflado": los 80-90 km² de Rancagua/Requínoa son
  el artefacto de **sembrado difuso** (susceptibilidad amplia sobre fondo de
  valle sin fuente empinada), justo lo que el guard de plausibilidad ya marca —
  no una propiedad del ruteo.
- **La UBICACIÓN generaliza peor**: IoU 0.181, nivel Copiapó in-sample (0.171),
  bien por debajo de Chañaral out-of-sample (0.555). El flujo baja en parte por
  una vía adyacente a la observada — el ruteo por el DEM Copernicus 30 m no
  clava el canal real de este evento.
- **Insensible a la lluvia** (IoU idéntico de 20/40/30 a 60/120/90 mm/día) y
  **determinista** (T≈0 en el preset): el footprint lo fija la geometría del
  terreno desde la fuente, no el volumen — así que no depende de reconstruir la
  lluvia exacta del 2017-12-16.
- El preset `de` (`seeding_power=0`, sembrado uniforme) da footprint **vacío**
  con una máscara de fuente rala (casi ningún agente cae en el disco de 79
  celdas): es interacción de config, no falla del modelo. `chanaral_enhanced`
  es el preset correcto para sembrado concentrado.

Conclusión: primera evidencia out-of-Atacama de que la **escala** del ABM es
razonable (no inflada) cuando el origen es conocido, con **ubicación** modesta
(IoU ~0.18) limitada por el ruteo sobre DEM global 30 m. Valida el ruteo/escala,
no la config operacional susc-driven (que en el centro-sur usa susceptibilidad
ML pero aún carece de evento mapeado). Es UN evento — no generaliza a todo el
sur, pero mueve la aguja de "sin verificar fuera de Atacama" a "un punto de
control real, con número".

## Honestidad operacional que ya está en pie

Independiente de este número, el reporte ya no lee un footprint out-of-domain
como verdad: guard de plausibilidad física (forma difusa → NO PLAUSIBLE),
`footprint_vacio` (sin producto), `calibration_domain_warning` (escala sin
validar aquí). Ver `runout_localidad.py` y `report/build_registro.py`.
