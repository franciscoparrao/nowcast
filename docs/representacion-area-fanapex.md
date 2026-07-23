# Representación por área / fan-apex

> Plan de trabajo. Destraba el pendiente "aluvión/crecida por localidad"
> (bloqueado en la abstracción centroide) y realiza el pendiente "subir
> resolución/representación de la forzante". Creado 2026-07-21.

## Problema en una línea
Hoy cada comuna es **1 punto** (centroide urbano, censo `Limite_Urbano_CPV24`).
Ese punto es el **fondo de valle**, casi nunca el **abanico de la quebrada** que
produce el aluvión. Dos consecuencias ya documentadas (HALLAZGO/HALLAZGO-2 en
`monitor/melton_localidad.py`):

- **Melton**: el enganche va al cauce mapeado más cercano = el **río del valle**
  → clasifica "crecida" a sitios de aluviones mortales (Alto del Carmen R=0.21,
  La Serena/Pueblo Islón R=0.17).
- **ABM**: se siembra sobre susceptibilidad-en-el-punto; en fondo de valle plano
  no hay fuente empinada → footprint difuso o vacío.

La abstracción correcta: **la comuna = el conjunto de fan-apexes** (puntos donde
una quebrada empinada desemboca sobre el área urbana/valle). Cada apex es una
fuente de siembra y su propia cuenca de Melton.

## Premisa ya validada
`scripts/validate_footprint_vsl.py` **ya es la receta de fan-apex para un apex
conocido**: siembra un disco de 150 m en el apex del footprint observado y el
ABM (`runout_ondemand`, preset `chanaral_enhanced`) rutea correctamente la
escala (área 3.98 vs 4.93 km², IoU 0.18). Generalizar a producción =
**automatizar la detección del apex** en vez de leerlo de un footprint ya
mapeado. Su `build_source_susc()` es reutilizable tal cual para la siembra.

## El corazón técnico: detección automática del apex
Un **fan apex** es donde una cuenca tributaria empinada y confinada pierde
confinamiento y deposita, al pie del frente montañoso, adyacente al área
poblada. **Todo lo necesario ya está en disco** (por cuenca, bajo
`KINGSTON/<basin>/`): `dem_30m.tif`, `hydrology/flow_direction_d8.tif`,
`flow_accumulation.tif`, `stream_network.tif`. Algoritmo (reusa
`cuenca_aguas_arriba` + `clase_melton` de `monitor/melton_cuencas.py`):

1. **Candidatos**: celdas de `stream_network==1` con área aportante en la banda
   productiva de flujo detrítico (≈ 0.5–100 km²; las cuencas de miles de km² son
   crecida — ya lo garantiza `MAX_BACKTRACE_KM2`).
2. **Filtro Melton por-cauce**: para cada candidato, R de **su propia cuenca**
   (no del río vecino) — mantener R>umbral. Esto **es** el fix del sesgo
   "cauce más cercano = crecida".
3. **Localizar el apex**: bajar por el cauce hasta el **quiebre de pendiente**
   (transición confinado→abanico: caída brusca de slope local) o hasta que el
   flow-path entra al buffer del polígono urbano. Ese punto es el apex.
4. **Deduplicar**: un apex por tributaria (candidatos del mismo canal colapsan
   al de aguas abajo).
5. **Asociar a comuna**: el apex pertenece a la comuna si su **flow-path aguas
   abajo intersecta `Limite_Urbano_CPV24`** de esa comuna → "un flujo desde ese
   apex puede alcanzar el pueblo". Reemplaza el enganche geométrico por
   causalidad de drenaje.

Salida: `fan_apexes.geojson` por cuenca/comuna (cacheable — morfometría
estática, igual que `melton_cache.json`).

## Dónde se enchufa (cambios por etapa de la cadena)

| Etapa / archivo | Hoy (punto) | Con fan-apex (área) |
|---|---|---|
| **Selección** `select_watchlist.py` | 1 centroide urbano/comuna | comuna entra si tiene ≥1 apex con R>umbral cuyo abanico toca el polígono urbano |
| **Melton** `melton_localidad.py` | snap al cauce más cercano (sesgo valle) | R de la cuenca **del apex** → destraba la clasificación (pendiente #2) |
| **ABM** `runout_localidad.py` | siembra susc-en-punto | siembra **disco en cada apex** (receta VSL) → footprint por apex, unión = amenaza de la comuna |
| **Exposición** `risk_map_localidad.py` | distancia footprint↔centroide | footprint↔polígono urbano (ya trabaja por área) |

## Validación (cómo sabríamos que mejora)
El único anclaje son los **footprints mapeados** (Copiapó in-sample IoU 0.17,
Chañaral out-of-sample 0.55, VSL 0.18). Test limpio: **¿el apex auto-detectado
coincide con el apex del footprint observado?** Corre el detector sobre las
cuencas de Chañaral/Copiapó/VSL y compara apex-detectado vs apex-observado
(distancia en m) y el IoU resultante vs el IoU con apex-leído-a-mano. Si el
detector recupera el apex a <1–2 celdas, la generalización está validada sin
footprint nuevo.

## Esfuerzo y riesgos honestos
- **Núcleo (detector + Melton por-apex)**: reusa hidrología y
  `cuenca_aguas_arriba`. Es el 70% del valor y destraba el pendiente #2. Medio,
  sin datos nuevos.
- **Integración ABM multi-apex + exposición**: mecánico, reusa
  `build_source_susc`. Chico-medio.
- **Riesgo 1 — DEM 30m**: el quiebre de pendiente en abanicos suaves es sutil a
  30m; en Atacama abrupto funciona (VSL lo probó), en abanicos tendidos puede
  fallar. Mitigable con umbral de slope conservador + reportar confianza.
- **Riesgo 2 — validación rala**: seguimos con 3 footprints. El detector se
  valida geométricamente (apex detectado vs observado), no con footprints
  nuevos.
- **Riesgo 3 — cuencas sin hidrología** (transfer: choapa, costa): heredan el
  "indeterminado" actual, sin regresión.

## Decisiones abiertas
1. **Umbral Melton para admitir apex**: R>0.6 (solo flujo detrítico franco,
   conservador) vs R>0.3 (incluye debris-flood, más cobertura, más ruido).
2. **Alcance del primer corte**: solo detector + Melton por-apex (destraba #2,
   sin tocar el ABM), o la cadena completa hasta ABM multi-apex.
3. **¿Validar el detector** sobre Chañaral/Copiapó/VSL antes de cablearlo a la
   watchlist? (recomendado).

## Plan de ejecución
1. **[LISTO] Detector de apex** (`scripts/detect_fan_apex.py`) — ver resultados abajo.
2. Melton por-apex en `melton_localidad.py` (destraba pendiente #2).
3. ABM multi-apex en `runout_localidad.py` + `select_watchlist.py`.

## Resultados del paso 1 (2026-07-21)

### El detector funciona
`scripts/detect_fan_apex.py` corre end-to-end sobre hidrología georreferenciada
(SurtGIS `hydrology all` sobre un DEM, o rásters KINGSTON precomputados; misma
convención D8). Sobre un DEM Copernicus de 17×19 km la hidrología tarda ~3 s.
Reglas: banda de área 0.5–100 km², Melton>umbral de la **cuenca propia** del
apex, quiebre de pendiente de canal (empinado arriba → tendido en el apex, con
contraste mínimo), y **filtro terminal** (un apex por tributaria: descarta
knickpoints interiores que tienen otro apex aguas abajo).

### Hallazgo: los dos footprints mapeados son del género geomorfológico equivocado
El plan original era validar el detector contra los footprints observados
(VSL, Chañaral). **Ninguno sirve como test cuantitativo de un detector de
fan-apex**, y descubrirlo es en sí un resultado:

- **VSL** (`data/vsl_mudflow_obs.geojson`): flujo **encajonado de valle** desde
  un deslizamiento alto discreto (Chaitén). Su celda más alta (1456 m) es la
  **cicatriz de la fuente**, no un fan-apex. Al aplicar el filtro terminal el
  detector se aleja de esa celda (592 m → 1579 m), correctamente: el apex del
  abanico está aguas abajo de la cicatriz. VSL no tiene abanico.
- **Chañaral** (`ground_truth.f32` del ABM, georreferenciado vía el transform de
  `chanaral_prediction_best.tif`, EPSG:32719): la **inundación urbana del pueblo
  en la costa por el Río Salado** — un main-stem de **1647 km²**, crecida
  hiperconcentrada, no un abanico de quebrada pequeña. El apex real (boca del
  Salado) queda fuera de la banda 0.5–100 km² *por diseño*. Ver
  `docs/img/fanapex-chanaral-mismatch.png`: el footprint (naranjo) sigue el
  main-stem azul, y los apexes detectados son quebradas laterales que no son el
  evento.

Los eventos de Atacama 2015 (Chañaral, Copiapó) son **crecidas regionales de
cuencas grandes** ruteadas por el main-stem a pueblos costeros/de valle — NO el
mecanismo de abanico aluvial de quebrada empinada que el detector (y la razón de
Melton>0.3) targetean. El detector es la herramienta correcta para el otro
mecanismo (Pueblo Islón, Alto del Carmen, quebradas del Cajón del Maipo), del
que no hay footprint mapeado disponible.

### Validación efectiva: deshace la misclasificación documentada
En vez de un IoU contra footprints del género equivocado, se validó contra la
**falla operacional documentada** (HALLAZGO en `monitor/melton_localidad.py`):
pueblos de aluviones reales que el centroide clasifica "crecida" porque engancha
al río del valle. El detector (ventana ±13 km, hidrología KINGSTON, candidatos a
<6 km del pueblo) recupera la quebrada empinada correcta:

| Pueblo | Cuenca | Centroide (hoy) | Detector fan-apex |
|---|---|---|---|
| Alto del Carmen | 06_rio_huasco | R=0.208 → **crecida** | 13 apexes; R hasta 1.32, varios **flujo detrítico** (quebradas 0.5–4.6 km²) |
| Monte Patria | 08_rio_limari | R=0.148 → **crecida** | apex R=0.888 **flujo detrítico** (0.81 km²) |
| La Serena | 07_rio_elqui | R=0.170 → **crecida** | **0 apexes** cerca del centroide |

`docs/img/fanapex-alto-del-carmen.png`: el pueblo está en el fondo de valle
(por eso el centroide ve "crecida"); los apexes detectados ("flujo detrítico")
están en el frente montañoso, en las bocas de las quebradas empinadas cuyas
rutas de flujo D8 bajan hacia el pueblo — el mecanismo de los aluviones 2015.
La Serena=0 es informativo: Pueblo Islón es un **sector** lejos del centroide de
la ciudad; el centroide genuinamente no tiene quebrada empinada cerca → doblemente
mal representado por un punto (refuerza la tesis de representación por área).

### Reproducción
Artefactos de trabajo en `scratch_fanapex/` (no versionado, regenerable):
```
# VSL / Chañaral (género equivocado, para el registro):
gdalwarp -t_srs EPSG:32718 -te <bbox+6km> -tr 30 30 <copernicus_tile> dem.tif
surtgis hydrology all --outdir hydro --threshold 500 dem.tif
python3 scripts/detect_fan_apex.py --dem dem.tif --hydro hydro --footprint <obs>

# Casos-falla (validación efectiva): crop KINGSTON ±13 km + hydrology, luego
python3 scripts/detect_fan_apex.py --dem dem.tif --hydro hydro \
    --near-lon <lon> --near-lat <lat> --near-km 6
```

## Resultados del paso 2 (2026-07-21) — módulo `monitor/fan_apex_localidad.py`

Decisiones fijadas con el usuario: **Melton R>0.3 rotulado por clase** +
**asociación por ruta-de-flujo que alcanza el área poblada** (no radio).

El módulo: (1) objetivo poblado de la comuna = polígono `Limite_Urbano_CPV24`, o
puntos `Aldeas_CPV24`+buffer si la comuna es rural (Alto del Carmen no tiene
polígono urbano — mismo fallback que `select_watchlist.py`); (2) ventana de
hidrología KINGSTON de la cuenca; (3) `detect_fan_apex.detect()`; (4) asociación:
un apex pertenece a la comuna si su **ruta de flujo D8 pasa a ≤400 m del objetivo
dentro de ~5 km de runout**. La proximidad (no entrada exacta a la celda) y el
cap por DISTANCIA de runout (no por área) son deliberados: el D8 es un thalweg de
una línea pero el flujo detrítico se abre en abanico 2D, y el pueblo suele estar
en el fondo de valle sobre el main-stem (el área no sirve de guarda ahí).

| Comuna | Centroide (hoy) | fan-apex (módulo) |
|---|---|---|
| **Alto del Carmen** | R=0.21 → crecida | **flujo detrítico** — 4 apexes asociados (R 0.57–0.95), flujo llega a ≤391 m |
| Monte Patria | R=0.15 → crecida | crecida / sin fan-apex — apexes detectados a 4.7–13.7 km, ninguno alcanza en 5 km |
| La Serena | R=0.17 → crecida | crecida / sin fan-apex — 0 apexes cerca del centroide de la ciudad |

`docs/img/fanapex-alto-del-carmen-asociados.png`: el caso limpio. El pueblo en el
valle; los 4 apexes asociados en quebradas empinadas con su ruta bajando al
pueblo (rojo); los detectados-pero-no-asociados (gris) tienen fan que no alcanza.
**Deshace la misclasificación**: donde el centroide veía "crecida" por engancharse
al río del valle, el fan-apex ve la quebrada de aluvión real.

Monte Patria y La Serena quedan "crecida / sin fan-apex". Es la respuesta HONESTA
(no el falso 'crecida' del centroide): o el pueblo NO está sobre un abanico
empinado (La Serena — centroide de la ciudad costera; Pueblo Islón es un *sector*
lejano, refuerza la tesis de área) o el abanico más cercano no alcanza en el
runout modelado (Monte Patria). NO se fuerza un fan-apex donde no lo hay.

### Consistencia de hidrología — RESUELTA (2026-07-21)
`load_window(hydro_source=...)`: default **`surtgis`** recomputa la hidrología con
`surtgis hydrology all` sobre el DEM de la ventana (~6 s/comuna), CONSISTENTE con
la validación del paso 1; `kingston` lee la precomputada (más rápido, otra red).
Con `surtgis`, Alto del Carmen sube a **9 apexes asociados** (R 0.57–1.32, todos
flujo detrítico) — coincide con el visual del paso 1. La fuente ya no es una duda.

### Hallazgo: la clase depende críticamente del cap de RUNOUT
Al re-verificar con hidrología consistente emergió que la asociación es sensible
al `max_runout_m`. Monte Patria:

| max_runout | apex asociados | clase |
|---|---|---|
| 5 km | 0 | crecida / sin fan-apex |
| 8 km | 3 | flujo detrítico |
| 12 km | 6 | flujo detrítico |

La quebrada R=0.89 (a 4.2 km del pueblo) **sí alcanza Monte Patria, pero a ~7 km
de runout** — plausible para un flujo detrítico real. El default de 5 km era
demasiado corto: Monte Patria probablemente ES "flujo detrítico". La Serena en
cambio es robustamente "sin fan-apex" (1 solo apex en 26 km, costa tendida).

Implicación: un cap de runout FIJO es un proxy grueso. La respuesta honesta no es
un binario bajo un cap arbitrario, sino **el runout mínimo al que un fan-apex
alcanza la comuna** (Alto del Carmen ~0.5 km = sobre el abanico; Monte Patria
~7 km = alcanzable por un flujo grande; La Serena = nunca). El cap correcto es en
el fondo físico → lo resuelve el footprint del ABM (paso 3: asociación = footprint
del ABM desde el apex solapa el objetivo, sin cap arbitrario).

### Reencuadre a runout-a-alcanzar — LISTO (2026-07-21)
Se eliminó el cap de runout arbitrario. `runout_to_reach()` reporta, por apex, la
distancia de runout a la que su ruta de flujo alcanza el objetivo (proximidad
≤fan_buffer); `runout_band()` la interpreta (≤2 km "sobre/al pie del abanico";
2–8 km "alcanzable por flujo detrítico"; >8 km "solo por flujo excepcional"). La
comuna se rotula con la clase del apex que alcanza a MENOR runout + su banda:

| Comuna | Centroide (hoy) | fan-apex: runout-a-alcanzar |
|---|---|---|
| **Alto del Carmen** | crecida R=0.21 | flujo detrítico, **0.5 km** — sobre el abanico (inequívoco) |
| **Monte Patria** | crecida R=0.15 | flujo detrítico, **7.0 km** — alcanzable por flujo grande |
| **La Serena** | crecida R=0.17 | debris flood, **14.4 km** — solo por flujo excepcional (≈ no amenazado en el centroide) |

El runout-a-alcanzar (0.5 / 7 / 14 km) es infinitamente más informativo que el
binario crecida/aluvión, y no esconde un parámetro elegido a mano. Alto del Carmen
queda desmentido con fuerza; La Serena confirma que su centroide urbano NO está
sobre un abanico (el riesgo de Pueblo Islón vive en otro punto → tesis de área).

### Barrido a la watchlist — LISTO (2026-07-21, `monitor/sweep_fanapex_watchlist.py`)
Sobre la watchlist vigente (ciclo 20260721T12z, 36 comunas): 12 con hidrología
propia, 24 en cuencas transfer (costa sin modelo, hueco conocido). De las 12
modeladas, **5 cambian** de "crecida" (centroide) a una clase de aluvión con
runout creíble (≤8 km):

| Comuna | Cuenca | Centroide | fan-apex | runout-a-alcanzar |
|---|---|---|---|---|
| Colbún | 11_rio_maule | crecida | debris flood | 0.0 km (sobre el abanico) |
| Angol | 12_rio_biobio | crecida | debris flood | 0.3 km (sobre el abanico) |
| Alto del Carmen | 06_rio_huasco | crecida | flujo detrítico | 0.5 km (sobre el abanico) |
| Vallenar | 06_rio_huasco | crecida | debris flood | 3.8 km (alcanzable) |
| Ovalle | 08_rio_limari | crecida | debris flood | 4.1 km (alcanzable) |

**Especificidad (el control clave): el detector NO inventa abanicos.** Los pueblos
de valle/costa planos (Linares, Longaví, Los Ángeles, Parral, San Clemente, San
Rosendo) quedan correctamente "crecida / no alcanza". Salida completa en
`~/nowcast-forecast/fanapex_sweep.json`.

## Resultados del paso 3 (2026-07-21) — `scripts/abm_apex_footprint.py`

Asociación por **footprint físico del ABM** (abanico 2D) en vez del thalweg D8 +
buffer: siembra `runout_ondemand` (preset `chanaral_enhanced`) con un disco de
fuente en cada fan-apex (receta validada del paso 1) y mide qué fracción del
objetivo poblado cubre el footprint. Fin del cap de runout arbitrario: la
extensión la fija la física del ruteo.

| Comuna | Apex (Melton, proxy-D8) | Footprint ABM | Cubre del pueblo |
|---|---|---|---|
| **Alto del Carmen** | R0.84 · 0.5 km | 2.9 km² | **16.8%** |
| | R0.57 · 1.0 km | 4.0 km² | **21.9%** |
| | R0.85 · 2.0 km | 2.3 km² | **16.8%** |
| **Vallenar** | R0.35 · 3.8 km | 1.9 km² | 10.6% |
| | R0.49 · 14.7 km | 5.8 km² | 10.5% |
| | R0.48 · 15.2 km | 6.3 km² | 10.5% |

**Alto del Carmen** queda confirmado FÍSICAMENTE: 3/3 apexes cercanos (0.5–2 km)
cubren 16.8–21.9% del pueblo → flujo detrítico inequívoco.

**Hallazgo — el paso 3 discrimina lo que el paso 2 no puede.** Vallenar tiene UN
polígono urbano compacto (5×4 km); los footprints de apexes a 3.8, 14.7 y 15.2 km
cubren ~10.5% del pueblo → banda **débil**, vs Alto del Carmen ~16–22% → **fuerte**.
El proxy D8 del paso 2 los igualaba ("alcanzable"); el footprint físico separa el
abanico que cubre el pueblo de lleno (Alto del Carmen) del que solo roza su borde
de valle (Vallenar). **La MAGNITUD de cobertura es el discriminador** que el proxy
no tenía.

Nota de honestidad — hipótesis falsificada: primero se atribuyó el ~10% de
Vallenar a un **footprint difuso de valle** (esparcido, no canalizado). Al añadir
la lectura de `stats.json` (paso 1 del pendiente) resultó **falso**: los footprints
de Vallenar SÍ concentran cuesta abajo (delta_frac ≥ 0.08, el guard de difuso NO
dispara). El ~10% no es esparcimiento: es que Vallenar es un pueblo de VALLE
mayormente **fuera** de los abanicos, cuyos bordes tocan las lenguas de flujo. El
discriminador correcto es la banda de cobertura, no la difusividad. El flag difuso
queda como guarda válida (para casos que sí lo sean), aquí no aplicó.

Rótulo del paso 3 (implementado en `abm_apex_footprint.py`): banda de cobertura
**fuerte ≥15% / débil 5–15% / marginal <5%** + flag **difuso** (delta_frac<0.08,
mismo criterio que `runout_localidad.py`). Asocia LIMPIO si banda≠marginal Y no
difuso. Comuna: clase del apex limpio de mayor Melton, con aviso "(cobertura
débil)" si ninguno es fuerte. Verificado: Alto del Carmen flujo detrítico (fuerte);
Vallenar debris flood (cobertura débil).

## Cableado diagnóstico — LISTO (2026-07-22)
`monitor/melton_localidad.py --watchlist --fan-apex` imprime la tabla combinada
CENTROIDE ‖ FAN-APEX (clase + runout-a-alcanzar + banda), reusando
`fan_apex_localidad.classify` (proxy paso 2; la cobertura ABM del paso 3 queda
como profundización per-comuna vía `abm_apex_footprint.py`, no en el barrido por
su costo on-demand). El header del módulo marca su HALLAZGO-2 como RESUELTO y
apunta a los módulos fan-apex. `target_geoms` cachea las capas censales (evita
releer la capa país por comuna). Verificado sobre la watchlist vigente: el fix
aparece en vivo y en AMBAS direcciones — Colbún/Ovalle/Santa Bárbara/Rancagua/
Melipilla/Paine suben crecida→aluvión con runout creíble; Ralco BAJA debris flood
(centroide)→crecida/no-alcanza (fan-apex), señal sana de que no solo sube.

## Próximo paso
1. Rural vs urbano: Alto del Carmen usó objetivo aldea; verificar que el buffer de
   aldea (400 m) no sub/sobre-asocie vs polígono urbano.
2. (Opcional) Persistir la clase fan-apex en el `resumen.json` por comuna para que
   fluya al registro/reporte del ciclo, no solo a la tabla interactiva.
