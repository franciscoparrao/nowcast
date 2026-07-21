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

- **Área observada: 4.9 km²**, alargado y canalizado (bbox 5.9 × 7.2 km).

### Señal de magnitud (NO es validación todavía)

Contra los footprints del ABM operacional out-of-domain (252 runouts, **mediana
16.4 km²**, y los de fondo de valle como Rancagua/Requínoa 80–90 km²), el único
evento real mapeado fuera de Atacama es **~3× más chico que la mediana** y ~17×
más chico que los mayores. Es consistente con la sospecha documentada de que los
footprints out-of-domain están **sobredimensionados**. PERO es solo una señal de
magnitud, no una validación:

- son eventos distintos con forzantes distintas (no es un IoU controlado);
- los footprints de 80–90 km² son justo los de fondo de valle que el guard de
  plausibilidad física ya marca como esparcimiento difuso, no canalización;
- el ABM se siembra por **susceptibilidad**, y en VSL no existe susceptibilidad
  ML (está fuera de toda cuenca modelada — de hecho el ABM operacional ni corre
  ahí: `runout_localidad.py` haría `sys.exit` "SIN COBERTURA").

## Validación pendiente (estudio, no script)

Un IoU real en VSL requiere armar un caso al estilo `data/chanaral/` en
`swarm-abm/models/debris-flow` (todos los `.f32`: dem/slope/streams/
susceptibility/rain×3/isotherm/sediment + `ground_truth` + `bbox` + `meta.json`)
y correr `evaluate_masked` con el preset `chanaral_enhanced`. Piezas:

1. **DEM**: `runout_ondemand` ya baja Copernicus DEM 30 m on-demand para
   cualquier lon/lat (bucket AWS) → resuelto.
2. **ground_truth**: rasterizar `data/vsl_mudflow_obs.geojson` a la grilla.
3. **rain**: forzante del evento 2017-12-16 (IMERG/estación) — 3 días peores.
4. **susceptibility**: EL NUDO. No hay ML en VSL. Opciones: (a) proxy de fuente
   (máscara alta en el escarpe de inicio del deslizamiento, apex del footprint
   observado) — prueba la FÍSICA DE RUTEO/escala pero es metodológicamente
   distinto de los casos Copiapó/Chañaral (que usaron susceptibilidad ML); (b)
   derivar una susceptibilidad rápida para el AOI. Cualquiera cambia la
   comparabilidad y hay que declararlo.

Por eso VSL valida el **ruteo/escala** del ABM fuera de Atacama, no la config
operacional exacta (que en el centro-sur sí usa susceptibilidad ML pero carece
de evento mapeado). Ambas mitades del problema son reales y distintas.

## Honestidad operacional que ya está en pie

Independiente de este número, el reporte ya no lee un footprint out-of-domain
como verdad: guard de plausibilidad física (forma difusa → NO PLAUSIBLE),
`footprint_vacio` (sin producto), `calibration_domain_warning` (escala sin
validar aquí). Ver `runout_localidad.py` y `report/build_registro.py`.
