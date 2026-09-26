# nowcast — Motor de nowcasting de geohazards (dinámico) en Rust

> **Estado:** v0.1 EN CURSO. Núcleo `nowcast-core` funcional (susceptibilidad×trigger
> + umbrales I-D + alertas), `std`+`thiserror`, build/test offline. Creado 2026-06-10
> desde revisión de estado del arte; núcleo iniciado 2026-06-16.
> Familia de motores Rust del autor: SurtGIS, Hydroflux, Smelt, Anvil, Cantus, Criterium.
> Doc madre: `~/proyectos/ideas-motores-rust.md` (idea N3, Parte 5).

## REGLA: fixes de frontera → barrido de hermanas (OBLIGATORIO)

Al arreglar cualquier bug de frontera (validación de entrada, panic OOB,
NaN/±inf, rangos [0,1]), **NO cerrar el fix a nivel de función**: aplicar el
barrido completo de `docs/checklist-hermanas.md` (mismo struct → mismo módulo →
espejos en otros crates → río arriba/abajo → simetría de verbos/superficies →
un test hostil por hermana) y dejar la línea "Barrido de hermanas: …" en el
cuerpo del commit. Razón: el patrón "se blindó la función nombrada y quedó la
hermana" reincidió 6 veces en 3 rondas de auditoría (tabla en el checklist).

## Qué es
Motor de **nowcasting** de peligros (deslizamientos, crecidas) gatillado por
forzantes dinámicas (lluvia, deshielo) en tiempo casi real, no susceptibilidad
estática.

## El gap que llena
La susceptibilidad hoy es **estática** (factores fijos + ML). El SOTA señala que
el nowcasting con triggers dinámicos (precipitación, snowmelt) es el gap
explícito. Une tus motores hidro con tu ML y umbrales empíricos.

## Dependencias y secuenciación (IMPORTANTE)
`nowcast` es un **integrador downstream**. Sus proveedores naturales ya están
**avanzados** (act. 2026-06-16): `rainflow` v0.1 (GR4J + HBV validados vs airGR,
salida caudal mm/día, lumped/semi-distribuido) y `snowmelt-rs` v0.9 (degree-day
+ ETI + balance de energía, validado MODIS F1 0.83 / CAMELS-CL NSE 0.66, salida
runoff rain+melt mm/día **por celda** sobre raster). Aun así el MVP v0.1 se
mantiene **desacoplado**: consume series observadas vía el trait `Forcing`; los
motores se enchufan como proveedores nativos en v0.2.

> Nota de API: `snowmelt-core` ya expone un tipo `Forcing` (enum meteorológico
> Uniform/Distributed, capa inferior). El `Forcing` de `nowcast` es la capa
> forzante→peligro; los adapters v0.2 aliasarán el de snowmelt para evitar choque.

- **No es proyecto de arranque temprano.** Si se quiere empezar la cadena hidro,
  partir por `rainflow` (tiene valor propio y caso de uso BNA).
- **Vía rápida sin esperar:** `nowcast` v0.1 con forzantes observadas ya permite
  validar la lógica susceptibilidad×trigger y publicar.

## Alcance MVP (v0.1) — desacoplado, sin dependencias internas
- [x] Susceptibilidad base (estática) como mapa de fondo — `SusceptibilityMap`
      acepta cualquier raster en [0,1] (Smelt o externo).
- [x] **Interfaz de forzante** (trait `Forcing`): series **observadas** de lluvia
      (CR2/DGA) vía `UniformRain` + lector CSV en `std`. (Pendiente: raster
      distribuido / deshielo MODIS como forzante de entrada directa.)
- [x] Umbrales intensidad-duración (I-D, `IdThreshold` con preset Caine) y
      modulación temporal del peligro (`TriggerModel` logístico).
- [x] Salida: mapa de probabilidad de peligro por paso (`HazardField`) + alertas
      (`Alert`). Ver `examples/quickstart.rs`.
- [x] Backtesting contra inventario fechado (SERNAGEOMIN) — módulo `backtest`
      (contingencia POD/FAR/CSI/bias, matching mensual event-céntrico con
      tolerancia) + `examples/backtest.rs` sobre Río Maipo (CR2MET diario
      1979-2016 × 157 eventos de lluvia). Hallazgos: Caine global a=14.82 → POD 0
      (curva demasiado alta); intercepto regional a*≈5.5 mm/h robusto (valida
      split-sample años pares POD 0.50); FAR ~0.9 estructural (base rate ~4% +
      una sola estación); ruido de fecha del inventario cuesta ~0.3 de POD.
      Datos derivados vía `scripts/extract_maipo_cr2met.py` (no versionados).
- [x] Backtest **distribuido** (v0.2): `GriddedRain` (forzante por celda) +
      `spatial_monthly_contingency` + `examples/backtest_distributed.rs` sobre
      subgrilla CR2MET 15×18 del Maipo con susceptibilidad **real** RandomForest
      (reproyectada). Métricas EWS apropiadas (ROC-AUC, POD@área) porque
      CSI/FAR son inservibles con inventario espacial disperso/incompleto.
      **Hallazgo (negativo, honesto):** AUC≈0.48 en las tres configuraciones
      (lumped, distribuida susc=1, distribuida×susc) — a resolución CR2MET
      5km/diaria la lluvia grillada NO discrimina las celdas-mes de evento (su
      lluvia media no es mayor que el promedio) y la susceptibilidad 30m
      promediada a 5km pierde su filo. El cuello de botella es la **resolución**
      de forzante/susceptibilidad (y el mes ruidoso del inventario), no el
      lumping → motiva forzante de alta resolución (sub-cuenca rainflow/snowmelt,
      QPE radar/satélite) que el trait `Forcing` hace intercambiable. Verificado
      independientemente en Python (AUC 0.488). `scripts/extract_maipo_distributed.py`.
- [x] (v0.2) Proveedor nativo **snowmelt**: crate `nowcast-snowmelt` envuelve
      `snowmelt-core` v0.10 e implementa `Forcing` con runoff (rain+melt) **por
      celda**. Pre-corre el modelo (stateful/secuencial) y bufferiza la forzante
      para el acceso aleatorio que exige el I-D. Demuestra amplificación
      rain-on-snow (+46% de agua) y distribución espacial por lapse-rate. Ver
      `examples/rain_on_snow.rs`.
- [x] (v0.2) Proveedor nativo **rainflow** (caudal lumped → crecidas): crate
      `nowcast-rainflow` envuelve `rainflow-core` (GR4J). `RainflowForcing`
      implementa `Forcing` (caudal broadcast) y `FloodNowcast` usa un gatillo de
      **exceedancia de caudal Q/Q_c** (no I-D: el ruteo ya integró la lluvia),
      reusando `SusceptibilityMap`/`TriggerModel`/`HazardField`/`Alert`.
      `FloodThreshold::quantile` deriva Q_c. Ver `examples/itata_flood.rs`
      (GR4J sobre CAMELS-CL Itata 1979-2016; crecidas en invierno austral).
- [x] (v0.2) Forzante de **alta resolución sub-diaria** GPM IMERG: `examples/
      atacama_subdaily.rs` + `scripts/extract_atacama_imerg.py` (GPM_3IMERGHH v07
      semihorario, evento Atacama mar-2015). Demuestra **lead-time** con datos
      REALES (descargados 1.1 GB, 144 gránulos): el núcleo de tormenta (lon −70.45,
      lat −27.15; total 108.5 mm, peak 40 mm/h) cruza el umbral I-D el 2015-03-24
      05:00 UTC, horas antes de los aluviones; la agregación diaria (107.6 mm el 24)
      solo marca el día, sin timing intra-diario. (Requirió autorizar la app "NASA
      GESDISC DATA ARCHIVE" en Earthdata — antes daba EulaNotAccepted.) El único
      IMERG aterrizable es Atacama 2015 (zona hiperárida → no es test de
      discriminación como Maipo, sí de resolución temporal). Confirma que el trait
      `Forcing` hace la resolución intercambiable.
- [x] **Head-to-head de resolución** (`examples/resolution_headtohead.rs` +
      `scripts/extract_atacama_cr2met.py`): mismo núcleo de tormenta, mismo motor
      I-D, CR2MET diario vs IMERG semihorario. CR2MET ve 30.1 mm, intensidad
      resoluble 0.66 mm/h (=total/24h) → E máx 0.45 → **NO gatilla**. IMERG ve
      108.5 mm, peak 40 mm/h → **ALERTA 24-mar 04:30, E≈9**. Sube la resolución
      de la forzante sin tocar el motor. **CUIDADO — mueve DOS factores a la vez**
      (producto y resolución; los productos discrepan 3.6× en el mismo píxel), así
      que NO se lee solo como resultado de resolución: ver el experimento de un
      solo factor abajo, que lo corrige.
- [x] **Generalización multi-evento** (`examples/multi_event_leadtime.rs` +
      `scripts/extract_event_imerg.py` con registro de eventos): IMERG semihorario
      sobre 3 aluviones fechados de climas opuestos — Atacama/Copiapó 25-mar-2015
      (árido, peak 40 mm/h), Cajón del Maipo 25-feb-2017 (convectivo verano, 5 mm/h)
      y Villa Santa Lucía 16-dic-2017 (frontal sur, 17 mm/h). El cruce I-D cae sobre
      o justo antes del día documentado en los tres; y en el Cajón del Maipo (ráfaga
      convectiva breve) el producto **diario NO gatilla** — solo lo sub-diario lo
      detecta. Cierra el caso de la forzante de alta resolución.
- [x] **Sensibilidad al intercepto** (`examples/intercept_sensitivity.rs`,
      20-ago-2026): mata el parámetro libre `a=4.0` que sostenía los resultados
      sub-diarios. Todo el paper corre ahora con el **a\*=5.5 calibrado** en el
      Maipo (una sola copia en `examples/shared/curves.rs`, compartida con
      `published_thresholds` para que no vuelvan a divergir) y **ninguna conclusión
      cambia**: head-to-head CR2MET E máx 0.45 (sigue sin gatillar) vs IMERG alerta
      04:30 E≈9; multi-evento sí/NO/sí idéntico. Dos argumentos independientes del
      umbral: (i) **intercepto crítico** `a_crit = max I·D^b` — como E escala 1/a,
      cada evento tiene un `a` máximo medible; el rango
      `a_crit(diario) < a ≤ a_crit(semi)` es donde la RESOLUCIÓN decide, y mide
      2.0–3.2× (Atacama 15.5–48.8, Maipo 2.5–6.8, VSL 9.4–19.1); (ii) las **35
      curvas de Guzzetti** sin tocar: la resolución voltea el resultado en 8/35,
      8/35 y 2/35. Tests de invarianza en `threshold.rs`.
- [x] **Experimento de UN SOLO FACTOR** (`examples/single_factor_resolution.rs` +
      `scripts/extract_event_cr2met.py`, 21-ago-2026): desarma el confound del
      head-to-head insertando la celda que faltaba del diseño — la MISMA serie
      IMERG agregada a 24 h. `A CR2MET diario →(producto)→ B IMERG 24h
      →(resolución)→ C IMERG semihorario`, y como E es multiplicativa la razón
      factoriza exacto. **Resultado que corrige la lectura previa**: en Atacama el
      producto (×6.2) supera a la resolución (×3.2) y es EL PRODUCTO el que voltea
      la detección (IMERG a 24 h ya gatilla, E=2.8); en Villa Santa Lucía no
      voltea ninguno (CR2MET diario ya gatilla, E=1.6); **solo en el Cajón del
      Maipo la resolución voltea la detección con producto fijo** (0.45→1.23). Lo
      universal de la resolución es el **timing** intra-diario, no la detección —
      las dos claims van separadas en el paper. Hallazgo de paso: el core IMERG
      cae EXACTO a mitad de camino entre dos celdas CR2MET y el desempate movía el
      día clave 25% (15.93 vs 12.73 mm) → ahora se usa el máx del footprint (la
      lectura más favorable al diario, sesga en contra de nuestra propia tesis).
- [x] **Experimento sintético NO deductivo** (`examples/synthetic_resolution.rs`,
      21-ago-2026): el review objetó —con razón— que etiquetar el evento como
      "ráfaga que cruza la curva I-D" y luego reportar que el motor I-D la detecta
      es una tautología. Ahora la etiqueta la genera un **mecanismo físico
      independiente**: tanque de presión de poros (infiltración capada + drenaje
      τ=4h, falla si θ>12mm), integrado SIEMPRE a resolución nativa (la física pasa
      a resolución fina; lo que degradamos es la OBSERVACIÓN). Auditoría impresa:
      123 de 667 ráfagas NO fallan y 1167 fallos caen en días que no son ráfaga →
      la etiqueta no es la clase. **El resultado clave aparece al estratificar por
      mecanismo**: con la agregación el POD de fallos por RÁFAGA cae 0.46→0.00
      mientras los de ACUMULACIÓN quedan intactos en ~0.42. El AUC agrupado
      (0.99→0.92) parecía "plano" —cargo del review sobre la Fig. 5— justamente
      porque promedia una población que la agregación aniquila con otra que no
      toca. Coherente con el single-factor: la resolución rescata la detección
      solo en la ruta de gatillo sub-diario.
- [x] **Diagnóstico del null distribuido** (`examples/null_diagnosis.rs`,
      21-ago-2026): el review objetó que §5.2 se apoyaba en que las celdas-mes de
      evento son MÁS SECAS que el promedio, lo que acusa al inventario, no a la
      resolución. Se testearon las hipótesis en vez de discutirlas: (a) quitando el
      gradiente espacial (z-score dentro de celda) el null persiste (AUC 0.472,
      z=−0.13); (b) dilatando la ETIQUETA (±2 meses, radio 1) sube apenas a 0.496
      —el registro NO está meramente corrido—; (c) las celdas con registro reciben
      la MISMA lluvia climatológica (−0.2%), no hay sesgo de exposición espacial;
      (d) el evento cae en el **percentil 46 temporal y 45 espacial** de su propia
      distribución: donde lo pondría el azar. Conclusión que ahora dice el paper:
      el null distribuido NO es evidencia limpia sobre resolución temporal, y la
      tesis se apoya en el experimento controlado y el single-factor. OJO al
      construir estos tests: dilatar el PREDICTOR (máx en ventana) suaviza
      positivos y negativos por igual y no responde nada; hay que dilatar la
      ETIQUETA.
- [x] **Posicionamiento vs estándares de acoplamiento** (21-ago-2026): el paper no
      citaba OpenMI / BMI-CSDMS / OMS3. Añadidas las 4 refs (verificadas por DOI en
      Crossref) y un párrafo en §2.2 que dice en qué el trait `Forcing` es
      deliberadamente MÁS ESTRECHO (solo lectura, acceso aleatorio, un campo
      escalar, sin ciclo de vida ni control de reloj ni intercambio bidireccional)
      y que los niveles componen en vez de competir. Alcance de reproducibilidad
      del snapshot Zenodo declarado con precisión: core = §5.1-5.7 sin path-deps;
      flood path y acoples NO reproducen sin los repos hermanos. + bloque Software
      Availability formato EMS.
- [x] (v0.2) **XAI / trazabilidad exacta**: módulo `explain` (`Explanation`,
      `Driver`) + `Nowcast::explain(cell, step)` + contrafactual
      `Nowcast::intensity_to_alert`. Como el peligro es forma cerrada
      (susc × gatillo), la atribución es EXACTA, no muestreada tipo SHAP:
      descompone cada alerta en terreno × clima, expone la ventana I-D dominante
      (duración, intensidad, E) y clasifica el driver (terreno/gatillo/balanceado).
      Inversas añadidas: `TriggerModel::exceedance_for_factor`,
      `IdThreshold::intensity_for_exceedance`. SHAP aplica río arriba al modelo ML
      de susceptibilidad (Smelt/PGML), que entra como input ya interpretable. Ver
      `examples/explain_alert.rs`.
- [x] (v0.2) **Acople físico con Hydroflux**: crate `nowcast-hydroflux` envuelve
      `hydroflux-solver-2d` (shallow-water 2D HLLC + Audusse + Manning). Acople
      unidireccional y bajo demanda: donde el nowcast alerta, corre el solver
      sobre el DEM local y devuelve `DepthField` (profundidad de inundación por
      celda). `Inundation::{run_rain, run_point_sources}`, `discharge_to_inflow_m3s`
      (mm/día×área → m³/s), `DepthField::refined_hazard` (downscaling de la prob
      gruesa al footprint físico). Ejemplo `couple_flood.rs`: 23 m³/s en un valle
      → agua concentrada en el canal, bancas secas, prob 0.7 en 24/264 celdas.
      NOTA: arrastra surtgis-core (gdal opcional OFF, usa tiff puro-Rust) → build
      online una vez (luego cache). El core sigue offline.
- [x] **Bridge geoespacial con SurtGIS** (keystone): crate `nowcast-surtgis`
      convierte `Raster<f32>` ↔ `SusceptibilityMap`/`GriddedRain`/`HazardField`
      (GeoTIFF nativo, sin GDAL). `susceptibility_from_raster`/`read_susceptibility`
      (clamp [0,1], nodata→0), `gridded_rain_from_rasters`, `hazard_to_raster`/
      `write_hazard_geotiff` + `Georef` (lleva transform+crs). Saca al motor de
      grillas sintéticas a rasters reales georreferenciados. Ejemplo
      `geotiff_roundtrip.rs` lee el RandomForest real del Maipo (5149×5855,
      [0.001,0.965]) y escribe un GeoTIFF de peligro. NOTA: bandas 0-indexed;
      el motor usa prefix-sums por celda → coarsen a la resolución de la forzante,
      no correr 30M celdas directo.

### Integración con el ecosistema (roles)
- **SurtGIS** → sustrato (susceptibilidad + I/O GeoTIFF + CRS). Adapter LISTO.
- **rainflow / snowmelt** → forzantes (caudal / rain+melt). Adapters LISTOS.
- **hydroflux** → acople físico de **crecidas** (profundidad 2D). Adapter LISTO.
- **swarm-abm** → **modelado del evento con agentes**: crate `nowcast-swarm`
  envuelve el modelo `debris-flow` (lib `debris_flow` sobre `swarm-abm`, agentes
  lluvia+flujo sobre raster, calibrado a Atacama 2015). Es el **acople físico del
  path de deslizamientos/aluviones** (footprint/runout), análogo a hydroflux que
  es para crecidas. NO es evacuación aguas abajo. `run_runout` →
  `Runout::refined_hazard`. Ejemplo `couple_runout.rs` sobre datos reales de
  Copiapó (123 km² con params default; calibrados DE en data/best_params_de.json).
  Adapter LISTO.
- **insar-rs** → forzante de **deformación** (2º trigger). LISTO: generalicé el
  trigger del core (módulo `multi`: trait `Trigger`, `IdTrigger` lluvia I-D,
  `ThresholdTrigger` valor/umbral, `Combine` Max/NoisyOr/Product, `MultiNowcast`
  que fusiona varios triggers). `nowcast-insar` envuelve `insar-core`:
  `DeformationForcing` (VelocityMap m/yr → tasa |v| mm/yr por celda, broadcast en
  el tiempo) + `deformation_trigger`. Ejemplo `rain_and_creep.rs`: lluvia marginal
  ⊕ deformación por noisy-OR — donde el terreno repta (v≳v_crit) el peligro cruza
  el umbral aunque la lluvia sola no gatille. insar-core: ~5300 LOC, PS-InSAR/SBAS,
  validado Fernandina; `run_sbas` produce el VelocityMap.
- **omori** → forzante de **sismicidad** (3er trigger, 2026-07-12). LISTO: crate
  `nowcast-omori` envuelve `omori-core` (estadística de catálogos + ETAS,
  validado vs SAPP/cluster2000x/SeismoStats): `SeismicityForcing` construye la
  serie **causal** de tasa ETAS esperada por paso (eventos/día M≥m0, 1ª
  generación dada solo la historia previa a cada paso; uniforme en espacio v1)
  y `seismicity_trigger` la monta en un `ThresholdTrigger` (tasa/tasa_crit).
  Caso de uso: debilitamiento post-sísmico de laderas — tras un mainshock la
  secuencia de réplicas caliente baja la lluvia necesaria para fallar
  (Chi-Chi, Wenchuan, Gorkha); fusión `lluvia I-D ⊕ tasa de réplicas` por
  noisy-OR. Tests: decaimiento Omori de la serie, causalidad (evento dentro de
  la ventana no afecta su propio paso), entradas hostiles, alerta sin lluvia.
  Adapter LISTO.
- **agromet-scraper (H0)** → **modulador** de isoterma cero (2026-09-02). Crate
  `nowcast-h0`: la altura de la isoterma decide qué fracción de la cuenca recibe
  LLUVIA en vez de nieve, y es casi ortogonal a la intensidad (r=−0.069, n=356
  días de tormenta) → aporta señal que el pluviómetro no lleva. Serie diaria
  30-36 °S por regresión T-elevación sobre la red INIA, validada vs 3.301 sondeos
  IGRA (hold-out 2023-26: sesgo +248 m, MAE 362 m, r=0.85).
  **Índice de DOS VÍAS** porque el peligro llega por dos rutas físicas distintas
  y un criterio único pierde la mitad: (a) VERANO, isoterma naturalmente alta +
  lluvia inusual (aluvión San José de Maipo 30-ene-2021, H0 4783 m = p75 absoluto
  pero ~p15 para un enero → un percentil mensual NO lo ve); (b) INVIERNO, isoterma
  anómala (temporal 21-26-jun-2023 → un umbral absoluto NO lo ve). Dispara con
  CUALQUIERA de las dos y `H0Signals` las reporta POR SEPARADO (`H0Via`), porque
  son mecanismos distintos y ameritan mensajes distintos.
  **NO VOTA, y tampoco modula todavía — va en SOMBRA** (2026-09-25): con umbral
  absoluto 4000 m el 100% de los días de dic-ene-feb lo cruzan (89% a 4500 m) →
  por noisy-OR daría alerta casi diaria en verano. Se había escrito que por
  `Combine::Product` «solo amplifica»: **FALSO** — dos factores en [0,1]
  multiplicados solo pueden BAJAR. Medido en los 356 días de tormenta: factor
  mediano 0.32 y en may-ago el 88% bajo 0.5 → Product hundía casi todas las
  alertas de invierno (incluidos los 5 temporales, que son tibios). Por eso
  `nowcast run --h0-csv --start <UTC paso 0>` REPORTA `H0Signals` por paso
  (JSON `steps[].h0` + cabecera `h0` con `series_last_date`) sin tocar el peligro,
  y el monitor lo pasa con `H0_CSV` (vacío por defecto) detrás de
  `monitor/h0_guard.py`: serie vieja (>`H0_STALE_DAYS`), dominio fuera de
  30-36 °S o archivo ausente → H0 se apaga y avisa 1×/día, la lluvia sigue igual
  (un diagnóstico nunca tumba el ciclo). Autotest `selftest.py --h0` verifica
  peligro idéntico con/sin/viejo. `with_start_hour` existe porque el trigger
  asumía paso 0 a medianoche y con ventanas semihorarias desde la tarde asignaba
  mal el día. **La serie NO se regenera a diario** (termina 2026-08-31): hasta
  que agromet-scraper la produzca, activarlo solo da el aviso de serie vieja.
  `MissingPolicy::Neutral` (hueco = 1.0) queda para experimentos con Product.
  Umbrales absolutos SOBRE LA ESCALA DE LA SERIE (sesgo +250/+450 m), no de
  radiosonda. Solo 30-36 °S.
  `MonthlyStormClimatology::from_series` recalcula el p90 mensual desde la serie
  y el ejemplo `h0_two_way.rs` verifica que **reproduce los 12 meses del reporte
  de río arriba** (bloque B de `h0_vs_eventos.txt`) — divergencia = fallo impreso,
  no desacuerdo silencioso. Datos: `scripts/extract_h0_transecta.py`.
  **La vía anomalía tiene DOS SUB-RUTAS porque la altura sola no bastaba.** Fijar
  la anomalía en un percentil alto NO ve su propio caso testigo: el temporal de
  junio-2023 está en p66-p84 de las tormentas de junio (medido: 65.9, 72.7, 78.4,
  80.7, 83.0, 84.1) y el p90 de junio es 3802 m, POR ENCIMA de los seis días. Lo
  que distingue al evento es que se SOSTIENE. Entonces la vía dispara con
  cualquiera de: **pico** (un día sobre el p90) o **sostenido** (`Persistence`:
  N días consecutivos sobre un p75). AGREGA una ruta en vez de bajar el listón
  del pico — un solo día a p80 sigue sin ser alerta. Verificado sobre la serie
  real: con p75/3 días el temporal se marca. **Costo honesto**: la racha tiene
  que acumularse, así que dispara el 25-jun, dos días dentro de un temporal
  documentado desde el 21 — rachas más largas compran especificidad y pagan
  lead-time. Además la racha parte el 23 y no el 21 porque los días 21 (3446 m) y
  22 (3314 m) están BAJO el p75 de junio (3466 m).
  Invariantes de la racha, testeadas: **un hueco la ROMPE** (no se puede afirmar
  persistencia sobre días no observados; romper es la dirección conservadora),
  un día bajo la barra la rompe, y **solo mira hacia atrás** (nunca lee el futuro
  — mismo invariante de causalidad que `nowcast-omori`).
  **BARRIDO de `min_days` — RESULTADO NEGATIVO** (`examples/h0_persistence_sweep.rs`):
  se barrió p70/p75/p80 × 2/3/4 días contra los 6 eventos fechados y **ninguna
  configuración justifica elegirse**. La vía anomalía sola detecta 0-2/6 (el pico
  p90 solo: 0/6) y el índice completo 1-3/6, siempre marcando 11-22% de los días
  de tormenta de toda la serie. NO es artefacto de huecos: la serie cubre 23 de
  los 25 días de evento. **La causa es que el catálogo casi no contiene el
  mecanismo**: los cinco temporales de invierno tienen su H0 máximo ENTRE la
  mediana y el p90 de su propio mes (son TIBIOS, ninguno cruza p90), y el único
  claramente cálido es el aluvión de verano, cuyo mes ni siquiera tiene
  climatología → lo cubre la vía absoluta. Bajar el umbral hasta alcanzarlos
  marcaría por construcción una fracción enorme de los días de tormenta.
  **Lección: H0 no es un detector de eventos mayores, es un discriminador de
  MECANISMO**; calibrarlo contra un catálogo no estratificado por mecanismo es
  ajustar ruido. Se mantiene p75×3d por especificidad (marca 14.9%, cerca del
  11.2% del pico solo) y NO p70×2d (22.2% por un evento más, dentro del ruido de
  n=6). Para elegirlo con evidencia hace falta un inventario que distinga aluvión
  de crecida fría.
- **firespread** → peligro paralelo (fuego): crate `nowcast-firespread` envuelve
  `firespread-core` (Rothermel + minimum-travel-time). `run_fire`→`FireField`
  (arrival/ROS/intensidad por celda, `fire_hazard` normalizado) y la **cascada
  post-incendio** `post_fire_susceptibility` que amplifica la susceptibilidad en
  la cicatriz quemada → realimenta el nowcast de lluvia (debris flow post-fuego).
  Ejemplo `couple_fire.rs`: misma tormenta deja 0 celdas en alerta pre-incendio y
  317 post-incendio. Adapter LISTO.

## Arquitectura tentativa
- `nowcast-core`: motor de reglas/umbrales + combinación susceptibilidad×trigger.
- Targets: native (Rayon) + CLI (`nowcast-cli`: run/backtest/explain/watch) +
  **Python (PyO3) LISTOS**. `nowcast-python` (cdylib, abi3-py39, maturin; fuera de
  `default-members` para que el gate offline no requiera Python) expone `Nowcast`
  (uniform/gridded, run/alerts/explain), `LiveNowcast` (push, bit-idéntico) y
  `Calibrator`/`reliability`/`brier_score`. Verificado: import + round-trip Python
  coincide con el motor Rust. Build: `cd crates/nowcast-python && maturin develop`.
- **Loop tiempo real LISTO**: módulo `live` (`LiveNowcast` streaming push-based,
  ring-buffer de prefix-sums por celda, **bit-idéntico** al batch; trait
  `StepSource` + `ReplaySource` + driver `run_live`). Ejemplo `live_loop.rs` (con
  aserción de paridad) y verbo CLI `nowcast watch`. Falta solo el feed de datos en
  vivo + validación operacional (limitación iii del paper, ahora parcial).
- **Probabilidad calibrada LISTA**: módulo `calibrate` (`Calibrator::fit_isotonic`
  PAV índice→probabilidad monótona; `reliability` con diagrama de fiabilidad,
  intervalos de Wilson, Brier/skill, ECE). Ejemplo `calibrated_probability.rs`
  (índice crudo skill −0.04 → calibrado +0.24, ECE 20× menor). Falta solo ajuste
  sobre eventos reales held-out (limitación i del paper, ahora parcial).
- **Nowcasting por ensemble (prototipo eje 1 SOTA)**: módulo `ensemble`
  (`ensemble_hazard`) corre el motor sobre un ensemble de forzantes (miembros vía
  trait `Forcing`) → `EnsembleField` con probabilidad de excedencia, media y spread.
  Es la maquinaria engine-side de la forzante pronosticada; el QPF real (pySTEPS/
  DGMR) enchufa por el mismo `Forcing`. Ejemplo `ensemble_nowcast.rs` (prob cruda
  vence al determinístico, lleva incertidumbre, y la calibración isotónica la vuelve
  reliable). Ver `docs/sota-roadmap.md` eje 1.
- Orquesta rásters de SurtGIS + salidas de rainflow/snowmelt + modelo Smelt.

## Monitor operacional (`monitor/`)
Monitor de eventos LISTO y verificado end-to-end (2026-07-10): `monitor/` trae
fetcher incremental de **IMERG Early NRT** (GPM_3IMERGHHE v07 vía earthaccess,
~4-5 h latencia real; recorta bbox→GeoTIFF/paso, borra HDF5, rellena huecos con
0 MARCADOS, rota ventana), `monitor.sh` (ciclo sin estado: re-corre la ventana
rodante de 48 h con `nowcast run --format json` — legítimo porque batch≡live es
bit-idéntico; dead-man switch del feed, notificación ntfy con histéresis de
cruces) y units systemd (timer :12/:42). Piloto default: cordillera RM/Cajón
del Maipo con **susceptibilidad REAL** desde 2026-07-29 (`make_susc_grid.py`
agrega el RandomForest del Maipo 30 m → grilla 0.1° del monitor con **p90** por
celda; mean aplana el filo de las laderas y max satura con un píxel). El raster
nunca se perdió (R3-M11 resuelto el 13-jul, vive en KINGSTON): faltaba
agregarlo. **`SUSC_TIF` y `ALERT_LEVEL` van ATADOS**: con uniforme 1.0 el 0.5
significaba "alerta al cruzar Caine", con un campo <1 el mismo 0.5 sube el
listón en silencio y pierde eventos marginales (medido: 12 mm/h se pierde
entero) → `make_susc_grid.py` deriva e imprime el coherente (mediana×0.5 =
0.272 en el piloto) y `monitor.sh` ABORTA si `SUSC_TIF` no existe en vez de
caer a uniforme. Autotest sin red: `python3 monitor/selftest.py`
(`--fusion`, `--susc`). Es monitor de eventos, NO alerta temprana; requiere
temporada en modo sombra antes de confiar.

**El binario operacional vive en `~/nowcast-monitor/bin/nowcast`, NUNCA en
`target/`** (2026-09-25). Las units y la config de atacama apuntaban a
`target/release/nowcast`; alguien borró `target/` para liberar disco y
`monitor@atacama` + `nowcast-forecast` quedaron caídos **23 días** (3-sep →
25-sep; 1144 ciclos en FATAL config, 90 pronósticos fallidos). El aviso ntfy
salió cada 6 h y nadie lo leyó como «el monitor no existe». Tras compilar:
`install -Dm755 target/release/nowcast ~/nowcast-monitor/bin/nowcast` (copia,
sobrevive a `cargo clean`; hay que reinstalar tras cada build release).
**Y el Python del pronóstico vive en `~/nowcast-monitor/venv-forecast`**
(`monitor/requirements-forecast.txt`, versiones del último análisis sano del
3-sep: numpy 1.26.4, geopandas 0.14.3, fiona 1.9.5…). El 7-sep el wheel de
geostat-rs subió numpy a 2.5.3 en `~/.local` y el geopandas del sistema rompió
`comuna_impact` (`np.array(copy=False)`); quedó oculto porque el pronóstico ya
moría antes por el binario. Mismo patrón que `target/`: dependencia COMPARTIDA
que otro proyecto cambia sin avisar. `forecast_all.sh`/`analysis_all.sh`
anteponen el venv al PATH (no una variable: `run_localidades` lanza hijos como
`python3`) y ABORTAN si falta. NO es el `~/nowcast-monitor/venv` del README de
nodos (earthaccess/h5py). El monitor en esta máquina sigue con el python del
sistema: `h5py` del sistema también está roto por numpy 2.5.3, IMERG se salva
porque xarray abre con netCDF4.

**Respaldo in-situ verificado (2026-08-29, corregido 2026-09-02)**:
`monitor/crosscheck_elcolorado.py` cruza El Colorado 2750 m —la ÚNICA estación de
cordillera del dominio piloto— entre la API DMC (330077) que el monitor usa hoy
con token y el acumulador local `~/proyectos/agromet-scraper`. **CORRECCIÓN: NO
son dos fuentes independientes.** Ahí la estación figura como `EXT-108` con
`institucion='DMC'`, y `EXT-` es como ese acumulador marca lo REPUBLICADO de
terceros (DMC 112, SERVIMET 42, CEAZA 34…) frente a su red propia `INIA-`. Es el
mismo sensor y la misma cadena, por dos rutas de entrega — que es justo lo que
mide el cruce: 6 de 7 horas de TA coinciden en el DECIMAL EXACTO (dif media
0.03 °C), firma de republicación, no de dos instrumentos. Por lo tanto el cruce
**valida la ruta** (agromet sirve si la API cae o el token vence) y **NO valida
el sensor**: un pluviómetro tapado se ve igual por los dos caminos. Para eso hace
falta IMERG/GOES o una estación vecina de otra institución.
**agromet va en UTC−4 FIJO y la DMC en UTC** — desfase constante +4 h.
**CORREGIDO 2026-09-04**: el 29-ago se concluyó «agromet guarda hora LOCAL» y que
el desfase se rompería el 2026-09-06 al pasar Chile a UTC−3. Era falso, y falso
de la manera más fácil de creer: el hallazgo se hizo en agosto, cuando Chile está
en UTC−4 y el offset fijo COINCIDE con el local. Dos evidencias sobre la base
histórica, no sobre la teoría: (1) 24 registros/día en las **18 transiciones de
huso 2018-2026** — con hora local habría un día de 23 h en cada adelanto y uno de
25 h (o colapso de PK) en cada atraso; (2) el **máximo diario de TA cae a las
13:00 todos los meses**, sin corrimiento en los meses con horario de verano — el
sol no cambia de horario. Consecuencia: **en los cambios de huso no hay que hacer
nada**, y el pendiente «revisar la base el 6-7-sep por la hora repetida» queda
CERRADO sin acción (además el de septiembre ADELANTA el reloj: la hora local
00:00 del 6-sep no existe, así que nunca hubo hora repetida — esa es la del
cambio de abril). `check_dst()` ahora verifica el offset fijo y solo avisa si
agromet CAMBIA de convención.
**El id NO se hardcodea**: era `108` hasta el 2026-08-31, cuando el scraper
descubrió que los ids numéricos colisionan entre instituciones (INIA-78 Visviri
vs EXT-78 Portezuelo) y los re-prefijó; el script pedía `108`, recibía cero filas
y moría. Ahora resuelve la estación por COORDENADAS (±1 km) y falla ruidoso. Un
id ajeno es un contrato que el otro repo puede renegociar.
Límite honesto de agromet para nowcast: no sirve de feed en vivo (corre 2×/día vs
30 min) ni cubre el dominio (8 estaciones, 1 en cordillera).

**`monitor/archive_ema.py` — el minutario deja de perderse (2026-09-02)**: la API
DMC sirve ~12 h y nada más, así que "repetir el cruce tras un evento" era
irrealizable: cuando uno va a mirar la lluvia, la ventana ya avanzó (pasó con la
lluvia del 31-ago). El archivador guarda el registro CRUDO por estación y mes en
`monitor/data/ema/<code>-<YYYY-MM>.jsonl` (idempotente, escritura atómica,
gitignored), con timer horario `:05` — la API da 12× redundancia, se pueden
perder once corridas seguidas sin perder un dato. El crosscheck acepta
`--desde/--hasta` y lee del archivo en vez de la API: verificado que el modo
archivo reproduce el modo API en el decimal. Un evento NO archivado está perdido
para siempre, así que el timer debe estar puesto ANTES de la lluvia. Autotest sin
red: `python3 monitor/archive_ema.py --selftest`.
**Cruce de PP VALIDADO (2026-09-25)** desde el archivo: chubasco del 11-sep 1.6 vs
1.6 mm hora a hora, septiembre 4.2 vs 4.2 mm en 474 h. Llevaba días "sin lluvia en
la ventana común" por un BUG del cruce, no por falta de lluvia: `--horas` (24 h)
recortaba también el modo archivo y botaba el evento. Huecos de agromet en
EXT-108: 97.2% 2018-2026 (100% 2023-25); los grandes son caídas de la ESTACIÓN
(la DMC tampoco los tiene, p.ej. 6-9 sep), salvo el 12-sep, caída de DNS local
que agromet no recupera y el archivador sí → agromet respalda ante API/token
caído, no ante un corte de red de este equipo de más de ~12 h.

**Fase A del plan EWS (2026-07-11) LISTA**: fusión multi-forzante de punta a
punta. (1) CLI: `nowcast run --fuse-rasters <stack>` repetible + `--combine
noisy-or|max|product` — cada stack extra es su propio `IdTrigger` vía
`MultiNowcast`; chequeo de alineación con `same_grid` nuevo en nowcast-surtgis
(y su hermana: `resolve_inputs` ahora valida susc↔lluvia, no solo dims). Los
errores de uso de clap ahora salen 1, no 2 (colisionaban con el contrato "exit
2 = alerta" — un binario viejo + flag nuevo se disfrazaba de alerta). (2)
`monitor/fetch_goes_qpe.py`: GOES-East QPE (ABI-L2-RRQPEF, 10 min, ~1.5 MB/
gránulo, latencia MINUTOS — medida real: gránulo 14:10 disponible 14:20) desde
AWS anónimo; reproyección geoestacionaria→0.1° con fórmulas GOES-R PUG (sin
pyproj), 3 gránulos = paso semihorario en la MISMA grilla IMERG (verificado:
transform idéntico y fusión aceptada por el motor con datos reales). (3)
`monitor/fetch_dga.py`: pluviómetros in-situ vía API DMC — **verificado contra
la API real (2026-07-11)** con el token del usuario: esquema real
`aguaCaidaDelMinuto` (minutario, newest-first, unidades embebidas tipo
"0.4 mm", UTC), 3/3 estaciones en modo interval. HALLAZGO: las estaciones DGA
republicadas (El Yeso 330149 etc.) responden "Información no disponible" — la
DMC publica su metadata, no sus datos; los defaults son EMAs DMC del dominio
(El Colorado 2750m / San José Guayacán / La Florida; Río Clarillo no tiene
pluviómetro). Credenciales en `monitor/config.local.env` (gitignored,
monitor.sh lo sourcea encima de config.env — NUNCA en config.env, que es
público); registro: /application/usuario/registroUsuario. Sin token se
deshabilita solo. Gauges DGA cordilleranos = mejora futura vía HIDROlínea
(snia/sat, JSF; su mapa inline solo trae el último valor). (4)
`monitor/prepare_fusion.py` + monitor.sh: fusión sobre la UNIÓN de ventanas
(cola rezagada de IMERG entra como cero contado — GOES/DGA cubren esas horas:
ahí vive la ganancia ~5 h → ~15-60 min), salud por feed (secundario caído
degrada y avisa, no mata el ciclo). Selftest `--fusion`: ráfaga solo-GOES
alerta; sin GOES queda quiet. Fase B (lead positivo): ensemble pySTEPS sobre
GOES QPE → `ensemble_hazard`.

## Validación
Backtesting contra inventario de eventos fechados (SERNAGEOMIN) — hit rate,
falsas alarmas, lead time.

## Venue objetivo
**Environmental Modelling & Software** (Elsevier, JIF 5.2, Q1 26/132 Water
Resources, híbrido) — DECIDIDO 2026-08-26 tras barrer los 41 journals del vault
más el universo JCR 2025 (3.568) con filtro híbrido + Q1/Q2. Backup: Computers &
Geosciences. El manuscrito es `docs/paper/nowcast-ems.tex`.

**NHESS quedó descartado** y con él casi todo el género: la restricción del autor
es *journal híbrido, nunca fully OA con APC obligatorio*, y los venues nativos de
los model description papers (NHESS, GMD, HESS, ESurf, JOSS, SoftwareX) son todos
OA puro. Esa restricción decide el venue más que el fit; si aparece
financiamiento de APC, GMD (Q1 39/259) vuelve al tablero. Razonamiento completo
en `~/vault/journals/_matches/2026-08-26_14-30_nowcast-cageo.md`.

**Bloqueante de rendición, no editorial:** las bases DICYT (10.3.1) exigen que el
IR **Mauricio Marín** sea coautor. Hoy el manuscrito tiene un solo autor. Cuando
entre, hay que repartir la sección CRediT. Verificar siempre con
`/firma-postdoc check docs/paper/nowcast-ems.tex` (hoy pasa 6/7).

### Traducción al español (`nowcast-ems-ES.tex`) — ARTEFACTO DERIVADO

Existe para **auditar el contenido** en la lengua del autor, no para someter. El
manuscrito de record es siempre el `.tex` en inglés; las correcciones van ahí y
la traducción se regenera. Lleva el sha del fuente en `\sourcehash` y un recuadro
de aviso en la primera página, para que no se lea como el paper.

Se creó justo después de borrar `nowcast-cageo.md`, que era una hermana
divergente con números falsos, así que **tiene dos defensas contra repetir esa
historia**: (1) `python3 scripts/check_translation_numbers.py` compara los
multiconjuntos de cifras del cuerpo de ambos .tex y falla si una sola difiere
(hoy: 584 cifras idénticas); (2) la notación numérica se preserva con punto
decimal, no coma, precisamente para que esa comparación sea mecánica. La
estructura también se mantiene 1:1 (8 secciones, 19 subsecciones, 9 tablas, 6
figuras, 5 ecuaciones, 27 labels, 43 citas). Si el inglés cambia, el sha queda
obsoleto y el chequeo de cifras avisa.

## Conexiones con tu ecosistema
- **rainflow** / **snowmelt-rs**: proveen las forzantes dinámicas.
- **Smelt**: modelo de susceptibilidad base + XAI.
- **physics-guided-ml** (`application_susceptibility`): fuente de los métodos
  PGML; nowcast es la versión **operacional Rust** que los consume (vía PyO3).
- **Hydroflux**: acople físico para zonas críticas; **insar-rs**: deformación
  pre-falla como señal adicional.
- Datos: 15 cuencas BNA + inventarios SERNAGEOMIN (paths en physics-guided-ml).

## Próximos pasos al retomar
1. Definir el trait `Forcing` y el esquema susceptibilidad×trigger + umbrales I-D.
2. Conectar una **serie de lluvia observada** (CR2/DGA, CSV) a un mapa de
   susceptibilidad de prueba — sin depender de rainflow/snowmelt-rs.
3. Backtesting sobre un evento real fechado del inventario (SERNAGEOMIN).
4. (Más adelante) Implementar `rainflow`/`snowmelt-rs` como proveedores `Forcing`.
