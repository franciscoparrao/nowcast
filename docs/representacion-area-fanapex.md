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

> ⚠ **Los números de esta sección están SUPERADOS.** Se dejan como registro de
> cómo se llegó al resultado. Los de Alto del Carmen salen del disco de 400 m
> (corregido el 23-jul, ver corolario abajo) y TODOS dividen por un objetivo
> recortado por la ventana (corregido el 29-jul). **Tabla vigente: "Tabla del
> paso 3, corregida" en la sección de re-verificación, al final del documento.**

| Comuna | Apex (Melton, proxy-D8) | Footprint ABM | Cubre del pueblo |
|---|---|---|---|
| **Alto del Carmen** | R0.84 · 0.5 km | 2.9 km² | ~~16.8%~~ → 3.9% |
| | R0.57 · 1.0 km | 4.0 km² | ~~21.9%~~ → 15.1% |
| | R0.85 · 2.0 km | 2.3 km² | ~~16.8%~~ → 3.9% |
| **Vallenar** | R0.35 · 3.8 km | 1.9 km² | 10.6% (se sostiene) |
| | R0.49 · 14.7 km | 5.8 km² | 10.5% (se sostiene) |
| | R0.48 · 15.2 km | 6.3 km² | 10.5% (se sostiene) |

**Alto del Carmen** queda confirmado FÍSICAMENTE como aluvión, pero la lectura de
2026-07-21 ("3/3 apexes cubren 16.8–21.9% → flujo detrítico inequívoco") era un
artefacto del disco: con el polígono real es **1/3** apexes (el R0.57, 15.1%) y
la clase es **debris flood**. Sigue siendo aluvión, no crecida — que es lo que el
paso 3 vino a decidir — pero con una sub-clase distinta y mucho menos holgura.

**Hallazgo — el paso 3 discrimina lo que el paso 2 no puede.** Vallenar tiene UN
polígono urbano compacto (5×4 km); los footprints de apexes a 3.8, 14.7 y 15.2 km
cubren ~10.5% del pueblo → banda **débil**, vs Alto del Carmen **fuerte** (15.1%
con la métrica corregida; 16–22% era el disco). El proxy D8 del paso 2 los
igualaba ("alcanzable"); el footprint físico separa el abanico que cubre el pueblo
de lleno (Alto del Carmen) del que solo roza su borde de valle (Vallenar). **La
MAGNITUD de cobertura es el discriminador** que el proxy no tenía. El hallazgo
sobrevive las dos correcciones posteriores, con menos margen del que se creyó
(15.1 vs 10.6%, no 22 vs 10.5%) — y en 2026-07-29 se le suma un caso más fuerte:
Ovalle, donde el footprint físico **desmiente** al proxy en vez de solo matizarlo.

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
débil)" si ninguno es fuerte. Desde 2026-07-29 la fracción se mide sobre la
**localidad** mejor cubierta (no sobre el área poblada de toda la comuna) y
contra su área geométrica completa — ver la re-verificación al final.

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

## Rural vs urbano (buffer de aldea) — RESUELTO (2026-07-23)
`Aldeas_CPV24` resultaron ser **MultiPolygon** del extent poblado (no puntos),
igual que `Limite_Urbano_CPV24`. El disco de 400 m en el centroide era una
aproximación innecesaria → `target_mask`/`target_overlap` ahora rasterizan el
polígono real (400 m queda solo como fallback defensivo). El objetivo de Alto del
Carmen pasa de ~1.0 km² (2 discos) al polígono real 0.256 km²; la clasificación
es ROBUSTA — sigue flujo detrítico, fan más cercano 0.6 km (antes 0.5). El buffer
no forzaba el resultado.

## surtgis-core 1.0.0 — RESUELTO (2026-07-23)
Al 2026-07-23 `surtgis-core` saltó a **1.0.0** (bump mayor) e `insar-core` pedía
`^0.18` → `cargo run -p nowcast-swarm` fallaba la resolución del workspace,
rompiendo TODO el path del ABM (paso 3 + `runout_localidad.py` operacional). Fix
(mismo baile 0.16→0.17 de antes): insar-core solo usa API estable de surtgis
(`CRS`, `GeoTransform`, `Raster`, `io::{read_geotiff, write_geotiff}` con args
`None`), que sobrevivió el bump; se subió la constraint en
`insar-rs/Cargo.toml` (`surtgis-core` 0.18→1.0). Verificado: insar-core compila +
159 tests, `nowcast-swarm` compila, y el ABM corre end-to-end. Cross-repo:
commit en insar-rs (Cargo.toml+lock) + nowcast (Cargo.lock).

Corolario para el paso 3: al restaurar el ABM con el objetivo-polígono real del
#3, la cobertura de Alto del Carmen es más honesta que con el disco: apex R0.84
cae de 16.8% (disco sobredimensionado) a 3.9% (marginal), y domina apex R0.57 con
15.2% (fuerte) → **debris flood** (sigue aluvión, sub-clase distinta). Las cifras
16.8–21.9% de la tabla del paso 3 eran del disco de 400 m; con el polígono real
son menores. El paso 2 (proxy) se mantuvo robusto; el paso 3 (físico) sí es
sensible a la geometría exacta del objetivo — otra razón para preferir el
polígono real sobre el disco.

## Persistencia en el ciclo — LISTO (2026-07-26)
La clase fan-apex vivía solo en tablas INTERACTIVAS (`melton_localidad.py
--watchlist --fan-apex`, `sweep_fanapex_watchlist.py`): el registro del ciclo y
el export GIS seguían viendo únicamente la clase por centroide. Ahora
`monitor/fanapex_localidades.py` (etapa **5b** de `analysis_all.sh`, entre
`run_localidades` y `build_registro`) la escribe como bloque `fan_apex` dentro
del `resumen.json` de cada localidad — la estructura que esos consumidores YA
leen, así que nadie río abajo cambia de fuente.

Por qué un paso aparte y no dentro de `runout_localidad.py` (que es quien
escribe el resumen): la morfometría es **estática** y se cachea entre ciclos
(`~/nowcast-forecast/fanapex_cache.json`, clave con los parámetros → cambiar un
umbral recalcula, no reusa en silencio), mientras el runout se recorre entero
cada ciclo; el costo real no es el detector sino leer las **capas censales del
país**, que en un proceso se leen una vez para las ~40 comunas y en 40 procesos
se leerían 40 veces; y `runout_localidad.py` trabaja por punto, ni recibe
`--comuna`. El orden 5b es DURO: `run_localidades` reescribe cada `resumen.json`
de cero, así que un enriquecimiento anterior se perdería.

Barrido sobre la watchlist vigente (ciclo 20260726T12z, 39 comunas): 25 con
hidrología propia, 14 transfer (quedan `disponible=false` con su motivo — la
ausencia de fan-apex NO se confunde con "crecida"). **4 corrigen** el centroide
a aluvión con runout creíble: Colbún (0.0 km), Melipilla (2.2), Ovalle (4.1),
Rancagua (4.8). Especificidad intacta: Linares, Longaví, Los Ángeles, Parral,
San Clemente y San Javier quedan correctamente "no alcanza"; Curepto y Empedrado
BAJAN de debris flood (centroide) a crecida/no-alcanza. Costo: ~15 min el primer
llenado del cache, **0.3 s** cualquier ciclo posterior.

En el registro: chip de **fenómeno** por localidad —
`aluvión de abanico` / `crecida, no aluvión`— que es una pregunta DISTINTA de
los tres chips existentes (esos son sobre la calidad del footprint; este dice si
el footprint de flujo detrítico es siquiera el modelo correcto del sitio). En la
ficha, la clase + a qué runout alcanza el abanico más cercano + qué habría dicho
el centroide, para que la corrección se vea en vez de aplicarse en silencio. Las
comunas transfer no llevan chip (no son crecida: son no clasificables). En el
GIS el bloque es plano a propósito → `export_gis.py` lo aplana solo a 20 columnas
QGIS (`fan_apex_*`), sin tocar ese script.

### Fix de frontera + barrido de hermanas
`detect_fan_apex.run_hydrology` llamaba `sys.exit` **dentro de una función de
librería**: `SystemExit` no deriva de `Exception`, así que se colaba por los
`except Exception` de los lotes blandos y una sola comuna sin hidrología habría
matado el barrido de las 39. Ahora levanta `RuntimeError` y el CLI lo traduce a
exit(1). Barrido de hermanas: `footprint_mask` (mismo patrón, mismo módulo);
los tres call-sites del lote (`fanapex_localidades.py`, `melton_localidad.py
--fan-apex`, `sweep_fanapex_watchlist.py`) capturan `(Exception, SystemExit)`
explícito como defensa en profundidad; y `fan_apex_localidad.load_window` ahora
BORRA sus temporales (cada ventana dejaba un DEM + 3 rásters de hidrología en
`/tmp` — 110 directorios y 1.1 GB acumulados de las corridas del 21-23 jul).
Test hostil: `python3 monitor/fanapex_localidades.py --selftest` (12 checks —
SystemExit/RuntimeError del detector, error propagado como motivo y no como
clase, transfer sin clase inventada, invalidación de cache por parámetro,
runout de 14 km que alcanza pero no cuenta como corrección).

## Re-verificación del paso 3 (Vallenar/Ovalle) — LISTA (2026-07-29)

### Lo que se buscaba y lo que apareció
El pendiente era re-correr la cobertura física con el objetivo-polígono real.
Primer hallazgo, por inspección antes de gastar corridas: **el fix del polígono
(#3, 2026-07-23) solo tocó la rama `aldea`** — Vallenar y Ovalle son `urbano`
(`Limite_Urbano_CPV24`), así que nunca usaron el disco de 400 m. La re-corrida de
Vallenar lo confirma **bit a bit**: 10.6 / 10.5 / 10.5 %, footprints 1.9 / 5.8 /
6.3 km², idénticos a la tabla del paso 3. De paso valida que nada derivó con el
bump a surtgis-core 1.0.0.

Lo que sí apareció es un **defecto de la métrica**, no de esas comunas.

### El denominador estaba recortado por la ventana, en silencio
`target_overlap` rasterizaba el objetivo **dentro de la ventana del ABM** y
dividía por las celdas resultantes. Si el objetivo no cabe entero, el
denominador es un pedazo arbitrario del pueblo y nadie se entera:

| Comuna | apex | ventana | objetivo VISTO | cobertura reportada |
|---|---|---|---|---|
| Ovalle | R0.58 | 14 km | **1.8 de 17.8 km² (10 %)** | 0.0 % |
| Ovalle | R0.51 | 18 km | 2.3 km² (13 %) | 0.0 % |
| Ovalle | R0.40 | 21 km | 15.1 km² (85 %) | 2.6 % |
| Vallenar | R0.35 | 14 km | 10.0 km² (99 %) | 10.6 % |

El "cubre 0.0 %" de Ovalle no significaba "el abanico no llega": significaba
**"la ciudad estaba fuera de cuadro"**. Vallenar se salvó por ser un polígono
compacto de 5×4 km.

### La raíz: una comuna no es un objetivo compacto
Ovalle son **6 localidades repartidas sobre 30×10 km** (ciudad 11.9 km² + 5
satélites) y Alto del Carmen **dos aldeas a 21 km** una de otra. Preguntar "qué
fracción del área poblada DE LA COMUNA cubre este abanico" (a) mete en el
denominador localidades que el flujo no puede alcanzar ni en principio, y (b)
obliga a ventanas de ~50 km para un flujo de 4 km. Es la pregunta equivocada: un
abanico amenaza **la localidad de su boca**, que es además lo coherente con la
tesis del paso 2 (la comuna = conjunto de abanicos, cada uno con su objetivo).

### Métrica corregida
1. `target_pieces()`: el objetivo son **localidades separadas** (una por fila
   censal), no un blob. La cobertura principal es la de la **localidad mejor
   cubierta**; se conserva `cover_frac_comuna` (métrica vieja) para comparar.
2. El denominador de cada localidad es su área **geométrica completa**, no la
   rasterizada en la ventana → un recorte solo puede **sub**-reportar cobertura,
   nunca inflarla.
3. `window_km_for()`: la ventana (centrada en el apex, como exige la receta de
   siembra) debe contener **enteras** todas las localidades a ≤ `2×runout+5 km`
   del apex. Las más lejanas quedan fuera y puntúan 0, correctamente.
4. `target_visible_frac` se reporta **siempre**; el recorte nunca es silencioso.

### Tabla del paso 3, corregida (cobertura de la LOCALIDAD)

| Comuna | Apex (Melton · proxy-D8) | Ventana | Footprint | Cubre de la localidad | Banda |
|---|---|---|---|---|---|
| **Alto del Carmen** | R0.84 · 0.6 km | 12 km | 2.9 km² | 3.9 % de 0.26 km² | marginal |
| | **R0.57 · 1.1 km** | 12 km | 4.0 km² | **15.1 %** | **fuerte** |
| | R0.85 · 2.1 km | 12 km | 2.3 km² | 3.9 % | marginal |
| **Vallenar** | R0.35 · 3.8 km | 23 km | 3.8 km² | **10.6 %** de 10.04 km² | débil |
| | R0.49 · 14.7 km | 41 km | 5.8 km² | 10.5 % | débil |
| | R0.48 · 15.2 km | 42 km | 6.3 km² | 10.5 % | débil |
| **Ovalle** | R0.58 · 4.1 km | 40 km | 8.6 km² | 1.7 % de 11.93 km² | marginal |
| | R0.51 · 6.2 km | 40 km | 8.6 km² | 1.7 % | marginal |
| | R0.40 · 7.6 km | 40 km | 8.4 km² | 4.7 % de 2.61 km² | marginal |

Clases resultantes: **Alto del Carmen = debris flood** (1/3 asocia limpio, el
R0.57 con cobertura fuerte) — reproduce el corolario del 23-jul, que resultó
accidentalmente correcto porque el recorte eliminó justo la aldea que no era el
objetivo. **Vallenar = debris flood (cobertura débil)**, 3/3 asocian. **Ovalle =
crecida fluvial / sin footprint que alcance**, 0/3.

### Los dos resultados que importan
**Ovalle es la contraparte de Vallenar.** El proxy D8 del paso 2 lo daba "debris
flood, alcanzable a 4.1 km"; el footprint físico lo **baja** a crecida: los
abanicos existen y sus footprints son de 8.4–8.6 km², pero **rozan la ciudad
(1.7 %), no la cubren**. El paso 3 no solo separa fuerte de débil (Alto del
Carmen vs Vallenar): también **desmiente** al proxy. Es la primera vez que la
física contradice al proxy en dirección conservadora, y es exactamente el
control que hacía falta para no rotular aluvión de más.

**La medida es robusta donde el objetivo es compacto.** Vallenar mantuvo
10.6 / 10.5 / 10.5 % con ventanas casi el doble de grandes (14→23, 35→41,
36→42 km) y con el footprint del apex 1 duplicándose (1.9→3.8 km²). Que la
cobertura del pueblo no se moviera mientras el footprint cambiaba es la
evidencia de que se está midiendo el solape real y no un artefacto de encuadre.

### Caveat honesto que el fix deja al descubierto
El flag **difuso** (`delta_frac`, elevación media del footprint vs. la del resto
de la ventana) **depende del tamaño de la ventana**: al agrandarla entra más
terreno alto y `delta_frac` se mueve (Ovalle 0.32/0.35/0.27 → 0.22/0.24/0.24;
Vallenar +0.02). Aquí ninguno se acerca al umbral de 0.08, así que no cambia
ningún veredicto, pero ahora que la ventana la fija la extensión del objetivo y
no el runout, el guard difuso ya no es comparable entre corridas de distinto
encuadre. Queda anotado, no resuelto.

### Barrido de hermanas (recorte silencioso de objetivo)
`fan_apex_localidad.classify` (paso 2) rasteriza el objetivo en **su** ventana de
hidrología (26 km) y tenía el mismo recorte mudo. Ahora reporta
`n_localidades` / `target_km2` / `target_visible_frac`, que `fanapex_localidades.py`
persiste en el bloque `fan_apex` (cache **v2**). La asimetría es benigna y hay
que decirla: `distance_transform_edt` mide a la celda-objetivo **visible** más
cercana, así que un recorte solo puede **agrandar** el runout reportado —puede
perder un abanico, nunca inventarlo. Medido sobre datos reales: Ovalle 97 % del
objetivo visible, Vallenar 100 %. Tests hostiles añadidos al
`--selftest` (14 checks).

## Próximo paso
1. (Opcional) Llevar el bloque `fan_apex` también a los reportes por cuenca
   (`report/build_cuenca_huasco.py` / `_coquimbo.py`), que leen el mismo
   `resumen.json` pero aún no lo muestran.
