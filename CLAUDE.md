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
`python3`) y ABORTAN si falta. **El monitor tiene el suyo, `~/nowcast-monitor/venv`**
(`monitor/requirements-monitor.txt`, 2026-09-27): cierre completo de dependencias
de earthaccess/xarray/netCDF4/rasterio/numpy/cftime con las versiones con que
funcionaba ese día (numpy 2.5.3 — reproducir lo que anda, no volver a 1.26).
Hasta entonces corría con el python del sistema, cuyo `h5py` ya estaba roto por
numpy 2.5.3: IMERG se salvaba solo porque xarray abre con netCDF4. Sin
h5py/h5netcdf a propósito (segunda copia de HDF5 = segfault de teardown).
`monitor.sh` valida el venv en el bloque 0 (CONFIG INVÁLIDA si falta) y lo
antepone al PATH; `selftest --exit-contract` prueba los fetchers con ESE python.

**Vigía de servicios detenidos (`monitor/watchdog.sh`, timer propio :25,
2026-09-26)** — la lección de fondo de los 23 días no fue `target/` ni numpy,
fue que NADIE SE ENTERÓ: el monitor avisó "CONFIG INVÁLIDA" cada 6 h y se leyó
como ruido; el pronóstico solo escribía al journal. Ahora: (1) `notify()` vive
en `monitor/lib_notify.sh`, UNA copia compartida por monitor, pronóstico y
vigía (con el contrato «histéresis solo tras entrega confirmada»); (2)
`forecast_all.sh`/`analysis_all.sh` avisan `high` en cualquier salida ≠ 0 vía
`trap EXIT`, con la última línea de log como motivo; (3) monitor y
`analysis_all` estampan `state/last_ok` al COMPLETAR un ciclo (`mark_ok`); (4)
el vigía descubre los timers HABILITADOS y avisa `urgent` "DETENIDO" si
`last_ok` pasa de `WATCHDOG_MONITOR_HOURS`=3 / `WATCHDOG_FORECAST_HOURS`=14
(insiste cada 6 h) y "RECUPERADO" una vez al volver; sin `last_ok` cuenta desde
la primera vez que lo vio. Cubre el caso que un aviso desde el ciclo no puede:
el ciclo que ni parte. Autotest `selftest.py --watchdog`. OJO nodos: `monitor.sh`
ya NO funciona sin `lib_notify.sh` al lado (README paso 2).

**`~/nowcast-forecast` y el GeoJSON comunal (2026-09-28)**: el mayor volumen del
directorio (14.7 GB de 37) era `comuna_impact.geojson`, 78 MB/ciclo de geometría
del censo repetida; la poda lo declaraba intocable porque la medición de agosto
contó `*.json` y no `*.geojson`. Ahora (1) `prune_forecast.py` lo reemplaza pasado
`PRUNE_DIAS_GEOJSON`=7 por `comuna_impact_attrs.csv` con TODAS sus columnas,
verificado fila a fila antes de borrar (primera pasada: 12.93 GB, 177 archivos);
(2) `comuna_impact.py` lo escribe con `shapely.coverage_simplify` 0.001° + 6
decimales → 3.3 MB, SOLO al escribir: los picos se calculan con la geometría
completa y el `comuna_impact.json` sale idéntico. Los consumidores leen `latest/`
(`select_watchlist` cruza por CUT, no por geometría).

**Cruces de 30 m en uint8 y poda activa (2026-10-25/27)**: `hazard_x_susc_30m_*.tif`
era el 98 % de cada ciclo (1.09 GB en float32, ~5.5 GB/día) y la poda corría en
modo REPORT, sin borrar. Ahora (1) drop-in local
`~/.config/systemd/user/nowcast-forecast.service.d/prune.conf` con
`PRUNE_MODE=apply` y `PRUNE_DIAS_30M=2`; (2) `susc_overlay.py` los escribe en
uint8 (×250, 255 = sin dato, scale 0.004 en el GeoTIFF) vía
`lib_raster.write_hazard30`: Atacama 230 → 8 MB en un día seco, ~7-17× en
cuencas con señal. Error ≤0.002: los umbrales no se mueven, pero las
estadísticas de un día SECO pierden resolución (Huasco: mediana 0.0008 → 0).
**Todo lector pasa por `lib_raster.decode_hazard30`** (acepta también los
float32 viejos); un `src.read(1)` crudo leería 0-250 como probabilidad.

**Bitácora: `~/nowcast-forecast/informes/index.html`** (2026-09-28,
`monitor/report/build_index.py`, paso 6b de `analysis_all.sh`, <1 s): una página
autónoma (fuentes y tokens importados de `build_registro`, sin copias) con el
estado de servicios, el último ciclo por dominio y modelo, pequeños múltiplos de
30 días (pico I-D regional por GFS/ECMWF/ICON, huecos >24 h rotulados "sin
ciclos" para que un apagón no se lea como calma) y la bitácora por día con el
registro de cada ciclo. Paleta validada con el validador de dataviz en claro y
oscuro. **`build_registro.py` corregido (2026-09-28)**: su título, bajada y narrativa
del resumen estuvieron FIJOS en el evento de julio ("Evento 15–20 jul 2026", "río
atmosférico extremo", ICON en el Huasco, footprints "a menos de 20 m", "81
corridas, ningún miembro tranquilo") en cada registro desde julio, y su "Hora
Chile" usaba UTC−4 fijo (corrida 1 h desde el 6-sep). Ahora todo sale del ciclo
(`resumen_modelos`, `resumen_runouts`, `rango_ventana`, `hora_chile` con
zoneinfo). Los registros viejos NO se reescriben: son constancia, con su hash.

**ECMWF por espejos (`monitor/ecmwf_mirrors.py`, 2026-09-28)**: desde el 26-sep
ningún ciclo tuvo ECMWF utilizable. El bucket de AWS respondía `503 Slow Down` a
~90% de los pedidos, y `fetch_ecmwf.py` lo usaba como única fuente y leía CUALQUIER
error del chequeo como "ciclo no publicado": retrocedía a ciclos de 25-56 h o se
rendía. Además aceptaba corridas con pasos perdidos (16/41 el 26-sep), que
concentran la lluvia en el paso siguiente y deforman la intensidad I-D. Ahora:
Google Cloud → data.ecmwf.int → AWS con respaldo (`ECMWF_MIRRORS`); "no publicado"
solo si TODOS dan 404; un error de servidor NO hace retroceder de ciclo; ciclo
de más de `ECMWF_MAX_AGE_H`=30 h no se usa; un paso perdido hace fallar la corrida.
`ecmwf_ensemble_check.py` usa el mismo módulo.

**Visor web — etapa 1: paquete por ciclo (`monitor/web_package.py`, 2026-09-29)**.
Decisión: visor ESTÁTICO (Svelte + WASM, stack del visor de SurtGIS), **privado
por ahora** (nada se publica; publicar marcaría comunas en "ALTO" con un sistema
en modo sombra). El paquete es el contrato: `~/nowcast-forecast/web/<ciclo>/`
con `manifest.json` (ventana, modelos por dominio, hora de cada paso, escala,
atribución CC BY de ECMWF/DWD, aviso), COG multibanda uint8 por dominio×modelo
(prob×250, 255 = sin dato, INTERLEAVE=BAND para leer un paso), máximo por
modelo, peor, `desacuerdo` (máx−mín entre modelos) y lluvia; `comunas.geojson`
(0.005°) y `localidades.geojson`. ~2 MB/ciclo, `WEB_KEEP`=12, `web/latest` y
`web/ciclos.json`. Paso 6c de `analysis_all.sh` (blando). Validado: 18/18 COG,
cuantización ≤0.002, NaN preservados, máximo = pico reportado (±0.0005), y
leído con geotiff.js (un paso sin cargar los 120).

**Visor — etapa 2 (`web/visor/`, 2026-09-30)**: Svelte 5 + Vite + geotiff.js,
mapa en canvas propio (sin tiles; mapa base = límites comunales), ráster
recortado a la silueta comunal, servidor solo en 127.0.0.1 con bloqueo de
recorrido de rutas. Modelo/peor/desacuerdo, peligro/lluvia, máximo 120 h u hora a
hora, tooltip con los 3 modelos, ficha de comuna. Probado por CDP (Chrome
headless), sin errores de JS. Cada archivo del ciclo se pide con `?g=<generado>`:
un ciclo SÍ puede regenerarse (la caché de 1 h escondió el arreglo de ICON).
Siguiente: `nowcast-core` → WASM (umbral movible, `explain` por celda).

**Umbral por modelo en UN solo lugar: `monitor/variantes.py`** (2026-09-30). GFS y
ECMWF → `regional-a5.5`; ICON → `caine-global` (su vecino-más-cercano desde 13 km
no diluye picos y con el regional se satura: 272/275 comunas en ALTO). La regla
existía desde el 16-jul en `susc_overlay` y `comuna_impact`, pero el índice, el
registro (tabla por dominio y resumen) y el paquete web usaban el regional también
para ICON: ICON aparecía ~0.95 donde el cruce comunal decía ~0.2 (en Linares una
celda 0.83 con pico comunal 0.17). Verificado tras unificar: 0 celdas sobre el pico
comunal en los 3 modelos (ECMWF comparado con el dominio de cada comuna, como hace
`comuna_impact`: Atacama para la región III).

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
**Desde el 2026-10-11 archiva las CUATRO EMAs del dominio RM** (`--code` acepta
lista separada por comas; default 330077,330112,330122,330019 = `DGA_STATIONS`
de `monitor@rm`): la primera alerta de `monitor@rm` (chubascos del 10-oct,
22:30Z, gatillada por GOES solo con 7.6 mm/h) quedó SIN verificar contra
Tobalaba, La Florida y San José de Guayacán porque solo se archivaba El
Colorado y, al ir a mirar, la ventana de la API empezaba 2 h después de la
alerta. Una estación caída se reporta y no bloquea a las demás (salida 1 si
alguna falló); solo las credenciales rechazadas abortan. Costo: ~1.3 MB/día
por estación en `monitor/data/ema/` (gitignored). Si se agrega una estación a
`DGA_STATIONS`, agregarla también al `ExecStart` de la unit.
**Cruce de PP VALIDADO (2026-09-25)** desde el archivo: chubasco del 11-sep 1.6 vs
1.6 mm hora a hora, septiembre 4.2 vs 4.2 mm en 474 h. Llevaba días "sin lluvia en
la ventana común" por un BUG del cruce, no por falta de lluvia: `--horas` (24 h)
recortaba también el modo archivo y botaba el evento. Huecos de agromet en
EXT-108: 97.2% 2018-2026 (100% 2023-25); los grandes son caídas de la ESTACIÓN
(la DMC tampoco los tiene, p.ej. 6-9 sep), salvo el 12-sep, caída de DNS local
que agromet no recupera y el archivador sí → agromet respalda ante API/token
caído, no ante un corte de red de este equipo de más de ~12 h.

**Cascada post-incendio EN OPERACIÓN (2026-10-09, caso Las Condes)**: el espejo
de `nowcast-firespread::post_fire_susceptibility` corre sobre cicatrices REALES.
`monitor/postfire_scar.py` calcula dNBR Landsat C2 L2 (B5/B7, QA_PIXEL) leyendo
solo la ventana por `vsicurl` + firma SAS pública de Planetary Computer (no baja
escenas; el disco va al 99 %), umbral 0.27 (Key & Benson) + mayoría 3×3, y
registra la máscara con vigencia en `monitor/data/postfire/scars.json`
(versionado: es evidencia). `susc_overlay.py` la aplica en la grilla NATIVA de
la cuenca (×factor, clamp [0,1], como el motor) ANTES de agregar a 0.1°, y en el
producto de 30 m; `comuna_impact`/watchlist la heredan sin cambios. Autotest:
`selftest.py --susc-overlay` (cicatriz vigente amplifica, vencida no).
Cicatriz registrada: San Carlos de Apoquindo, incendio 29-dic-2025 → 2-ene-2026
(1.012 ha oficiales; dNBR ≥0.27 da 670 ha en un solo cuerpo, LC09 1-dic-2025 →
18-ene-2026), factor 2.0 (el de `couple_fire.rs`), vigente hasta 2027-12-29.
GOTCHAS medidos: (i) la prensa municipal decía "29 de septiembre" — era el 29 de
DICIEMBRE (lunes); con escenas de sep/oct-2026 el dNBR dio 0 ha porque la post
era pura nube (QA 50 %); verificar la fecha del incendio contra dos fuentes antes
de elegir escenas. (ii) En Sentinel-2/Landsat, una cicatriz oscura cae en los
bits de sombra del QA: mirar el desglose de bits si "no hay quema". (iii) A 0.1°
la cicatriz (≈7 km²) es menor que una celda (≈93 km²): por vecino más cercano
NO aparece; donde se ve es en el máx agregado y en el producto de 30 m. (iv)
Persistencia: 9 meses después (LC09 15-sep-2026) la mediana del dNBR en la
cicatriz bajó 0.346 → 0.153 y el 69 % sigue ≥0.1 — sigue activa. (v) Resultado
operacional: con la forzante GFS del 8-oct la cascada NO cruza 0.5 en la
cicatriz (mediana 0.13 → 0.26) porque el GFS daba 2.4 mm/h; con la ráfaga
OBSERVADA en Tobalaba (8.2 mm/h, a*=5.5 → gatillo 0.88) la cicatriz pasa de
0.30 a 0.60 (mediana), y con Caine global (0.14) no cruza nada. La cascada
corrige el terreno; el cuello sigue siendo la forzante sub-horaria.

**Corroboración entre feeds (2026-10-12, `monitor/corroborate.py`)**: la
fusión es noisy-OR y un solo feed basta para cruzar; medido en monitor@rm que
"solo un feed" no vale lo mismo según cuál sea: el 10-oct GOES QPE solo
(3.8 mm/30 min, estaciones en ~0) dio una falsa alarma, y el 12-oct el primer
cruce real lo sostuvo SOLO el feed de estaciones (Tobalaba 6.4 mm/h con IMERG
y GOES en 0). La regla, en el ramo de alerta de `monitor.sh` antes de la
histéresis: sobre la racha más reciente, en las celdas que cruzan el nivel
(rásters `out/hazard`, o todo el dominio si `WRITE_HAZARD_TIFS=0`), suma la
lluvia de cada feed en las últimas `CORROB_HOURS`=3 h; un feed sostiene si
acumula ≥`CORROB_MIN_MM`=1 y, salvo DGA, ≥`CORROB_FRACTION`=0.5 del feed
mayor (la llovizna de IMERG, 1.2 mm, no respalda una ráfaga de 36 mm que solo
trae GOES: lo midió el autotest). Corroborada si sostiene DGA o IMERG, o GOES
con rayos GLM (≥`CORROB_GLM_FLASHES`=3 en el dominio) o con IMERG; si solo
sostiene GOES el aviso sale "NO corroborada (solo GOES QPE)" en prioridad
default con histéresis propia (`state/alert_uncorrob`) y NO estampa la racha,
así que si otra fuente la respalda después sale como cruce nuevo urgente.
`CORROB_ENABLED=0` apaga la regla. Autotests `corroborate.py --selftest` (11
casos) y `selftest.py --corrob` (solo-GOES → default sin racha; GOES+DGA,
solo-DGA → urgente con racha; apagada → como antes). OJO en tests: los casos
se construyen antes del bucle, así que los archivos sintéticos necesitan
nombre único por caso (dos rásters con el mismo nombre se pisaron).

**Evidencia congelada por alerta (2026-10-12, `monitor/freeze_evidence.py`)**:
la alerta solo-GOES del 10-oct quedó SIN verificar: al ir a mirar, la API DMC
(~12 h) ya empezaba 2 h después del cruce, los pasos de la ventana habían
rotado y los `hazard_%04d.tif` tenían otros índices. Ahora el ramo de alerta
de `monitor.sh` (bloque 3c; corroborada O no) congela en
`WORK_DIR/evidence/<EP_FIRST>/` la ventana `[primer paso en alerta −
FREEZE_PRE_H=6, último + FREEZE_POST_H=3]`: pasos de cada feed de
`fusion.json` (`feeds/<feed>/step_<clave>.tif`, gapmarks; los zerofill del
primario se anotan en el manifiesto, no se copian), peligro renombrado POR
CLAVE DE TIEMPO (`hazard/hazard_<clave>.tif`: el índice cambia con cada
rotación), `glm/` (pasos, counts filtrado, status por ciclo), `ema/<code>.jsonl`
(registros CRUDOS de `archive_ema` en la ventana, `EMA_ARCHIVE_DIR`, default
`monitor/data/ema`) + `ema/resumen.json` horario, `runs/run_<ciclo>.json` y
`fusion_<ciclo>.json`, `config.env` con credenciales tachadas, `ciclos.jsonl`
(una línea por ciclo: racha, veredicto de corroboración, título, entregado,
GLM reciente, mm de EMAs) y `manifest.json` (commit del monitor, sha256 por
archivo). Mientras la racha persista cada ciclo AÑADE y NUNCA pisa (el motor
reescribe `out/hazard` cada ciclo; lo congelado es lo que vio el ciclo de la
alerta). La clave de ciclo lleva SEGUNDOS: dos ciclos en el mismo minuto
(pasa en los tests) colisionaban en `run_<ciclo>.json`. Nunca tumba el ciclo
(`skip …` en el log); se omite con <`FREEZE_MIN_FREE_MB`=500 libres;
`FREEZE_EVIDENCE=0` lo apaga. Autotests `freeze_evidence.py --selftest` (17
casos) y `selftest.py --freeze` (ciclo real: congela; 2º ciclo añade línea sin
pisar; solo-GOES congela con veredicto 0; apagado no crea nada). Nadie poda
`evidence/`; por alerta ~0.3 MB de rásters + ~0.5 MB por EMA y día de ventana.

**Feed de RAYOS GLM en el monitor (2026-10-12, `monitor/fetch_glm.py`)**: el
8-oct los dos QPE satelitales no vieron la ráfaga de Las Condes y el sensor de
rayos del mismo GOES-19 sí: 233 descargas sobre el piedemonte, pico de 82 en
10 min a las 19:30Z (16:30 Chile), 30 min ANTES del aluvión (barrido de 1.080
archivos GLM-L2-LCFA; evidencia en `docs/informes/frente-2026-10/`). Lo que
falla es la estimación de lluvia, no el satélite. El feed cuenta descargas por
paso semihorario en la grilla 0.1° (`feeds/glm/steps`, acumulado entre ciclos)
y dentro de cada cicatriz vigente de `scars.json` que toque el dominio
(+`GLM_SCAR_BUFFER_KM`=5, por el error de geolocalización), incremental por
`state.json`, ~90 archivos de 20 s por ciclo en memoria con 8 descargas en
paralelo (medido: 359 archivos en 66 s). **NO entra a la fusión ni toca el
peligro**: es diagnóstico. `monitor.sh` (bloque 2c) pone el resumen de los
últimos `GLM_LOOKBACK_MIN`=60 min en el cuerpo de avisos y en el log, y manda
un aviso propio "CONVECCIÓN sobre cicatriz" (urgent, ≥`GLM_ALERT_FLASHES`=10
sobre una cicatriz) o "convección eléctrica en el dominio" (high,
≥`GLM_ALERT_FLASHES_DOMAIN`=50), con histéresis `GLM_REALERT_HOURS`=2 que se
reinicia al quedar en cero. Validado en UN evento: los umbrales son
provisionales y hay que medir en más ráfagas si todas tienen rayos. Activo en
`monitor@rm` (config del dominio); el template lo trae en 0. Autotests:
`fetch_glm.py --selftest` (binning, paso, buffer, resumen) y `selftest.py --glm`
(aviso sobre umbral, silencio bajo umbral, histéresis, reinicio en cero, aviso
de dominio; el peligro I-D sigue quiet).

**Histéresis por CRUCE en el monitor (2026-10-12)**: `monitor.sh` avisaba el
ESTADO del dominio (marca `state/alert` re-estampada cada `REALERT_HOURS`=6
mientras hubiera algún paso en alerta en la ventana), aunque el comentario
decía "cruces, no estados". Lo destapó monitor@rm: la falsa alarma solo-GOES
del 10-oct (22:30-23:30Z) quedó 54 h en la ventana y, con el frente del 12-oct
entrando esa noche, un cruce REAL habría salido fundido con el aviso repetido y
hasta 6 h tarde. Ahora el ciclo calcula la RACHA más reciente de pasos
consecutivos en alerta (claves de tiempo vía `fusion.json`); si empieza más de
`ALERT_EPISODE_GAP_H`=2 h después del fin de la última racha avisada (o no hay
racha avisada) es un **cruce nuevo** y se avisa de inmediato (histéresis 0,
título "CRUCE NUEVO", cuerpo con la hora Chile del cruce); si es la misma
racha, "persiste" cada `REALERT_HOURS`. La racha avisada vive en
`state/alert_episode` y se escribe SOLO tras entrega confirmada
(`NOTIFY_DELIVERED`, nuevo en `lib_notify.sh`); se borra al volver a quiet.
Autotest `selftest.py --alert-episode` (racha vieja avisa; misma racha se
suprime; racha nueva 15 h después avisa con la marca fresca; quiet olvida).
OJO al diseñar tests: una ráfaga de 150 mm deja la I-D excedida ~40 h, así que
"otra ráfaga" es la misma racha; con 36 mm la alerta dura ~4 h. `selftest.py`
usa `~/nowcast-monitor/bin/nowcast` si no hay `target/`. **Las claves de
tiempo de la racha salen de `span[0]` + 30 min × índice**, no de la lista del
primario: la unión puede tener más pasos que IMERG (cola DGA/GOES) y justo ahí
caen los cruces en vivo; el 12-oct el primer cruce real (pasos 103-106,
solo-DGA, Tobalaba 6.4 mm/h con IMERG y GOES en 0) salió con claves vacías y
`state/alert_episode` quedó corrupto hasta el arreglo.

**Cicatrices del megaincendio de enero-2026 registradas (2026-10-12)**: tres
entradas más en `scars.json`, todas con fecha 2026-01-14, factor 2.0 y vigencia
hasta 2028-01-14: Concepción–Penco (Trinitarias + Rancho Chico, 18.533 ha, un
cuerpo de 17.058 ha; LC09 path 001/085 24-dic → 26-feb; cuenca
`17_costa_transfer`), Florida–Ránquil–Trehuaco–Bulnes (Monte Negro y vecinos,
8.065 ha; path 233/086 10-ene → 19-feb; `17_costa_transfer`) y Laja–Los Ángeles
(Rucahue Sur, 4.040 ha; `12_rio_biobio`). Coherentes con CONAF (22.772 / ~1.500+ /
2.999–4.123 ha). En la grilla nativa la mediana de susceptibilidad en la cicatriz
pasa 0.45→0.91, 0.41→0.81 y 0.37→0.75. Informe trazable del frente del 12-oct y
de estas cicatrices: `docs/informes/frente-2026-10/informe.html`.
GOTCHAS nuevos: (vi) **el agua hay que enmascararla** (QA_PIXEL bit 7, ya en
`QA_BAD`): el mar cambia de NBR entre fechas y con un bbox costero la máscara de
Concepción salió con 41.153 ha, de las cuales 16.237 eran un cuerpo 100 % océano
frente a Tomé–Dichato y el 26 % del cuerpo de Penco era la bahía. (vii) Los bbox
de cicatrices distintas **no deben solaparse**: `aplicar_cascada` multiplica por
cada cicatriz en secuencia, y una celda en dos máscaras recibiría ×4. (viii) Las
escenas Landsat son paralelogramos: un bbox en el borde de un path queda en
parte sin dato (fill), y el QA lo reporta como "enmascarado" sin distinguirlo de
nube; probar el path vecino (001/085 vs 001/086 para Concepción–Tomé). (ix) Un
corte de plantación entre las dos fechas también da dNBR alto: las hectáreas por
comuna son estimaciones, no cifra oficial.

**Escritura ATÓMICA y purga de archivos corruptos en los fetchers (2026-10-13,
`monitor/lib_raster.py`)**: el 9-oct a las 22:15 Chile, con el disco al 99 %,
los fetchers de `monitor@atacama` dejaron cinco GeoTIFF de 0 bytes (un gránulo
y un paso GOES en la ventana, tres copias en `archive/`). `rasterio.open(path,
"w")` crea el archivo antes de escribir, así que un disco lleno deja un nombre
válido sin firma TIFF; desde entonces `fetch_goes_qpe` moría al abrir el
gránulo y el motor al leer el paso ("TIFF signature not found") en CADA ciclo,
**73 horas en FATAL** que el vigía avisó cada 6 h y nadie leyó como «dominio
caído» (se destapó al correr el vigía a mano al instanciar araucania). Ahora:
`write_geotiff`/`write_json` escriben en `<path>.part-<pid>` y `os.replace`
(nada queda con el nombre final si falla); `purge_invalid` corre al inicio de
cada fetcher (IMERG `steps/`, GOES `granules/` y `steps/`, DGA, GLM) y borra
con aviso lo que no abre, antes de decidir qué falta → el gránulo se vuelve a
bajar si la fuente aún lo sirve, o el paso vuelve como hueco MARCADO (cero);
`read_json` tolera `state.json`/`counts.json` vacíos (GLM); `prepare_fusion`
es la última defensa: un paso corrupto entra como hueco, nunca al motor; el
archivo de `monitor.sh` copia por `.part` + `mv`. `fetch_glm` además sale con
`os._exit` como las otras hermanas HDF5+GDAL (le faltaba desde que nació; lo
acusaba `--exit-contract`). Autotests: `lib_raster.py --selftest` (5 casos) y
`selftest.py --corrupt` (paso IMERG de 0 bytes, paso GOES con basura, gránulo
vacío y temporal huérfano: el ciclo termina, la ráfaga sigue alertando, lo
corrupto se purga o vuelve como hueco marcado, la bitácora dice "corrupto" y
no "TIFF signature", los status son JSON válidos). Verificado en vivo con un
ciclo de atacama (exit 0). Los dos pasos IMERG archivados del 9-oct se
perdieron sin remedio. Sigue sin haber guardia de espacio libre en los
fetchers (solo en freeze_evidence): con el disco lleno la escritura falla y el
paso queda como hueco, que es lo correcto, pero conviene vigilar el 96 %.

**`monitor@araucania` EN OPERACIÓN (2026-10-12, modo sombra, con GLM)**: tercer
dominio, decidido tras dos eventos convectivos en cuatro días sin monitor NRT
en la región (Las Condes 8-oct, Curacautín 12-oct: granizada de 20 min, 21
viviendas inundadas; informe `docs/informes/frente-2026-10` A24–A29) en los
que el único sensor con hora y lugar correctos fue el GLM, una hora antes.
Config `~/nowcast-monitor/domains/araucania/config.env`, `WORK_DIR`
`~/nowcast-monitor-araucania`, bbox −73.25..−71.05 / −39.55..−37.65 (23×20
celdas, Malleco y Cautín de costa a cordillera). Susceptibilidad: p90 del
mosaico `16_araucania_transfer` + `12_rio_biobio` + `17_costa_transfer`
(submuestreo bajado solo a 56 m por el tamaño de la grilla fina; mediana 0.782
→ `ALERT_LEVEL=0.391`); las capas de transferencia NO están validadas en
destino. GOTCHAS: (i) con el borde oeste en la costa (−73.45) tres celdas
tenían <5 % de terreno y `make_susc_grid` abortaba → bbox recortado a −73.25;
(ii) tres celdas del borde NE (lon −71.05, lat −37.65..−37.85, borde del
ráster del Biobío, susc 0.36) quedan MUDAS a propósito (`--allow-mute`).
`ID_A=5.5` en sombra como rm y como el pronóstico comunal del dominio, pero
NO está calibrado acá y la región recibe lluvia frontal de 5+ mm/h por horas:
medir la tasa de cruces antes de confiar. `GLM_ENABLED=1` sin cicatrices en el
bbox → solo el aviso de dominio (≥50/60 min; el 12-oct hubo 160/10 min).
Feed DGA con las 8 EMAs DMC de la región que entregan minutario (Victoria,
Lonquimay, Rucamanque, Maquehue ×2, La Araucanía Ad., Villarrica, Pucón;
Angol 370067 no entrega datos desde julio), todas de valle salvo Lonquimay;
no hay EMA en Curacautín. `archive_ema` (unit) archiva también esas ocho.

**`monitor@ohiggins` EN OPERACIÓN (2026-10-20, modo sombra, con GLM)**: cuarto
dominio, instanciado de madrugada por las alertas de remoción en masa para la
RM y la VI Región de ese día (el pronóstico 12z del 19-oct ponía 17 comunas de
O'Higgins en ALTO y la región no tenía monitor ni EMAs archivadas). Config
`~/nowcast-monitor/domains/ohiggins/config.env`, `WORK_DIR`
`~/nowcast-monitor-ohiggins`, bbox −71.95..−70.05 / −34.95..−33.85 (20×12
celdas, Pichilemu a la cordillera). Susceptibilidad: p90 de `10_rio_rapel` +
`17_costa_transfer` (la costa al oeste de −71.86 queda fuera del ráster del
Rapel) + `09_rio_maipo` + `11_rio_maule`; mediana 0.637 → `ALERT_LEVEL=0.318`.
GOTCHA: con solo Rapel+costa quedaban 52/240 celdas MUDAS, todas de borde:
leían solo los píxeles de la divisoria (≈0.22, el piso del RandomForest).
Sumar las cuencas vecinas las bajó a 25 (`--allow-mute`): valle plano y Lago
Rapel (correcto), la fila sur −34.95 cuyo terreno es del Mataquito (sin modelo)
y la alta cordillera del Tinguiririca (RF ≈0.22). Feed DGA con las 5 EMAs DMC
de la región (`getEstacionesRedEma`, `region=6`: Rancagua 340045, Quimávida
340048, Nilahue 340066, Portezuelo-Codelco 340093 a 1185 m, Pichilemu 340115),
también en `archive_ema`. `ID_A=5.5` en sombra, sin calibrar acá. Primer ciclo
(04:45Z): "CRUCE NUEVO" en 3 celdas de la cordillera de la costa (−71.6,
−34.1) a las 21:30Z del 19-oct, sostenido SOLO por un paso IMERG aislado
(5.1 mm/30 min con ceros antes y después; GOES 0.2 mm, rayos lejos, sin EMA
cerca): candidata a falsa alarma de IMERG, la regla de corroboración lo acepta
porque IMERG cuenta como respaldo por sí solo.

**Disco lleno tumbó los cuatro monitores (2026-10-23, 23:47 → 00:16)**: otros
proyectos llenaron el disco (territorio-digital +14 GB, volcanes +12 GB en tres
días) y los monitores fallaron con «No space left on device» 1.5 h; el vigía no
avisó porque DETENIDO sale a las 3 h. Recuperados borrando `~/.cache` y los
recortes Sentinel-2 propios. Ahora `watchdog.sh` avisa URGENTE si quedan menos
de `WATCHDOG_DISK_MIN_GB`=10 GB libres (histéresis 6 h, RECUPERADO una vez;
`selftest.py --watchdog` lo cubre), y la descarga IMERG de calibración se detiene
con < 6 GB para no competir con la operación.

**Runout en SOMBRA con gatillo I-D (2026-10-23, `RUNOUT_SOMBRA_DENSIDAD`=0.1)**:
el barrido de densidad de swarm-abm (189 corridas, 56 eventos del catálogo con
lluvia; reporte en `data/calibracion_runout/`, fuera de git) eligió por la regla
declarada (máx lift con recall ≥0.5 a 150 m) 0.05 gotas/km², banda 0.02–0.1
dejando un temporal fuera; la regla quedó decidida por el piso de recall (el lift
cae siempre con la densidad) y por 16 eventos de El Arrayán que ninguna densidad
recupera (El Colorado no vio la lluvia, E máx 0.68). Bajo 0.1 el resultado
depende de la semilla (Cajón 2021: 1–8 aciertos de 10 entre semillas con 0.02);
desde 0.1 el rango es ≤1. **0.1 es decisión de producto, no calibración.** Cada
runout por localidad corre además en modo gatillo (FollowField, volumen 2·a) y se
guarda en `~/nowcast-forecast/runouts_sombra/` sin reemplazar al producto (+~10
s por localidad; si falla solo se cuenta). Regla declarada para la 2ª vuelta:
mínima densidad con recall ≥0.8 sobre ventanas que disparan y rango entre
semillas ≤1 acierto por ventana; pérdida por forzante aparte. **2ª vuelta
(24-oct, 6 semillas, 378 corridas)**: la regla v2 da 0.1 con la serie perfil
(recall 0.97 en ventanas que disparan, lift 1.41) — el valor en sombra — y 0.2
con la cota uniforme, por el rango entre semillas del Cajón 2021 (7–10 de 10);
banda 0.05–0.2 dejando un temporal fuera. La diferencia 0.1/0.2 es la
desagregación horaria TRANSFERIDA de Guayacán: no se puede zanjar sin dato
horario allí. Umbral de inicio: con estaciones NO es identificable (4 mediciones
independientes detrás de 57 eventos); a* operativo no se mueve con esa tabla.

**Runout por localidad: binario INSTALADO, no `cargo run` (2026-10-22)**:
`runout_localidad.py` compilaba `nowcast-swarm` en caliente en cada localidad.
swarm-abm agregó `Layers::trigger` (`8ccad98`) y el struct literal de
`runout_ondemand.rs` dejó de compilar: **40 runouts fallidos en dos ciclos**
(21-oct 06z y 12z) con «could not compile». Mismo patrón que el `target/`
borrado de septiembre: dependencia por path a un repo hermano que cambia sin
avisar. Ahora usa `RUNOUT_BIN` (default `~/nowcast-monitor/bin/runout_ondemand`)
y si falta ABORTA con la instrucción de instalar; tras cada build:
`install -Dm755 target/release/examples/runout_ondemand ~/nowcast-monitor/bin/`.
`trigger: None` en el ejemplo y el test conserva paridad (huellas idénticas
píxel a píxel con chanaral_enhanced y de). `flood_localidad.py` (manual)
prefiere `FLOOD_ONDEMAND_BIN` y compila con aviso si no está.

**El runout ABM NO responde a la lluvia (2026-10-21, `scripts/huellas_2021/`)**:
el runout por localidad del pronóstico (`run_localidades` → `runout_localidad`
→ `runout_ondemand`) está calibrado en UN solo temporal (Atacama 25-mar-2015:
`chanaral_enhanced` en Chañaral, `de` en Copiapó). Medido en El Arrayán con la
lluvia escalada: `chanaral_enhanced` da la MISMA huella (6.417 celdas) de 0.3 a
200 mm/día y `de` es un interruptor (0 a 0.3 mm/día, saturado desde 1).
Causa en `swarm-abm/models/debris-flow/src/model.rs`: agentes de lluvia FIJOS
(orígenes por susceptibilidad) y nace un flujo si la lluvia horaria supera
`rain_threshold`, calibrado en [0.01, 0.3] mm/h (`chanaral_enhanced` = 0.01,
el límite): con un solo evento lluvioso el umbral no es identificable. El largo
no depende del volumen, pero NO porque el flujo se trabe: lo corta el
presupuesto de pasos (`--steps` 300; en chanaral_enhanced semilla 42, 437 de
615 flujos siguen vivos al final — corrección de swarm-abm, 21-oct). Además
`flow_radius` ignora el volumen con física Copiapo y `update_velocity` solo usa
pendiente. **Los runouts de los registros
son mapas de terreno condicionados a "llovió ≥1 mm", no pronóstico.**
Validación contra 86 puntos del catálogo nacional (29–31 ene 2021, RM;
`validar_puntos.py`): lift 1.5–1.8 a 150 m sobre un nulo de puntos al azar
junto a cauces en Cajón y El Arrayán, inconsistente a 0–60 m, 0 aciertos en Las
Vertientes con el preset operacional. Mapear huellas de ese evento con
Sentinel-2 10 m no funcionó (`huellas.py`: depósitos bajo la resolución).
swarm-abm respondió (21-oct): `Layers::trigger` (E por celda y hora; nace flujo
si E ≥ 1, volumen `trigger_volume_scale`·E, parámetro LIBRE a calibrar) ya está
en main (`8ccad98`, paridad bit a bit con `trigger: None`). Mata el interruptor
(0.3–5 mm/día ya no generan flujos), pero la huella sigue saturando: los flujos
nacen en las mismas `n_rain_agents` posiciones fijas, así que el rango dinámico
lo fija el número de gotas, no el forzante. **Punto 2 hecho (swarm-abm
6f07499, 22-oct)**: `TriggerSeeding::FollowField` (default con gatillo) siembra
cada hora `trigger_drops_per_km2`×área excedida gotas entre las celdas con E≥1;
`Fixed` reproduce el modo anterior. API ahora `#[non_exhaustive]`: se construye
con `Layers::new(..).with_trigger(..)` y `Params` partiendo de un preset.
Medido en El Arrayán (`scripts/huellas_2021/sensibilidad_gatillo.py`, La Platina
29–31 ene 2021 ×1/×2/×4): `fixed` 6.417 celdas siempre; `follow` responde a la
lluvia por las HORAS excedidas (12.682 → 15.135 de ×1 a ×2, plano de ×2 a ×4
porque E es uniforme en la ventana) y sobre todo a la densidad de gotas, un
parámetro LIBRE: 0.02 → 12.682 celdas, 0.1 → 31.691, 0.5 → 72.472. El default
0.02 sale de dominios de miles de km²; en una ventana de 14 km (69 km² sobre las
compuertas) da k = 1 gota/h. Con forzante puntual o de 0.25° el área excedida
casi no varía dentro de la ventana, así que el efecto área de FollowField no
aparece en nuestros runouts. Prioridad acordada: (1) gatillo
externo ✔, (2) gotas que sigan al campo de gatillo ✔, (3) calibrar con eventos y
no-eventos (catálogo nacional + lluvia de estaciones), (4) largo según volumen
junto con el presupuesto de pasos. Hasta (2) y (4) los runouts NO son pronóstico
de extensión.

**Radio en el IDW del feed de estaciones (2026-10-20, `DGA_IDW_RADIUS_KM`=15)**:
`fetch_dga.idw_field` interpolaba SIN radio, así que toda celda del dominio era
un promedio de TODAS las EMAs. El 20-oct una sola EMA de precordillera
(Portezuelo-Codelco, 10.8 mm/h, E=2.55) puso en alerta 80 celdas de O'Higgins,
65 a más de 25 km de cualquier pluviómetro (informe `evento-2026-10-20`, A7).
Ahora cada celda interpola solo con las EMAs a ≤15 km (distancia en km corregida
por latitud) y fuera de eso el feed vale 0: sin evidencia, no aporta al noisy-OR
ni corrobora. Repetida la racha: 76 → 16 celdas (7 junto al pluviómetro, 9 de
GOES en San Fernando), ninguna encendida. De paso: `DGA_IDW_POWER` NUNCA llegó
al fetcher (`monitor.sh` exporta solo BBOX/WORK_DIR/WINDOW_HOURS/STALE_HOURS);
ahora se pasan los dos explícitos. Barrido: ninguna otra variable de fetcher
quedaba sin pasar. Vacío o 0 = sin radio (comportamiento viejo).

**`nowcast-forecast` moría por tiempo en los días de evento (2026-10-20)**:
`TimeoutStartSec=90min` mató 3 de 6 ciclos del 18-19 oct (18z, 06z, 18z) en el
paso 5 de `analysis_all.sh`: el runout ABM por localidad tarda 2-3 min c/u y en
un evento la watchlist llega a 30 comunas (~75 min solo ese paso). Los ciclos
que morían eran justo los de lluvia. Subido a 240 min (debajo de las 6 h entre
ciclos). Si vuelve a quedar corto, paralelizar `run_localidades` en vez de
seguir subiendo el límite.

**`monitor@rm` EN OPERACIÓN (2026-10-11, modo sombra)**: el piloto RM de
`config.env` NUNCA tuvo timer propio (solo corría `monitor@atacama`), y por eso
el 8-oct nadie vio en vivo la ráfaga de Las Condes. Instancia:
`~/nowcast-monitor/domains/rm/config.env` (bbox −70.85..−69.75 / −34.25..−33.05,
extendido al norte para cubrir Lo Barnechea entero; 12×13 celdas), `WORK_DIR`
`~/nowcast-monitor-rm`, Caine global como atacama, `ALERT_LEVEL=0.267` derivado,
IMERG Early + GOES QPE + 4 EMAs DMC (se suma Tobalaba 330019, la que midió los
8.2 mm/h del 8-oct). **La susceptibilidad del monitor lleva la cascada
post-incendio**: `make_susc_grid.py` carga `scars.json` y aplica ×factor en la
grilla nativa ANTES del p90 (`--no-postfire` para la línea base); medido: la
celda de la cicatriz sube 0.544→0.577, la mediana no se mueve (el umbral sigue
coherente) y solo 3 celdas cambian. Primer ciclo (04:47Z): `quiet`, 108 pasos,
máximo 0.07. Timer habilitado con `systemctl --user enable --now
nowcast-monitor@rm.timer`; el vigía lo descubre solo.
**Bug GOES destapado al instanciar (`fetch_goes_qpe.py`)**: con numpy ≥2.5,
`bincount` con pesos VACÍOS devuelve int64 y `np.divide` no escribía en ese
`out` → un gránulo sin ningún píxel válido en el bbox (DQF≠0 en todo el dominio,
típico de madrugada) fallaba en CADA ciclo mientras siguiera en la ventana
(atacama: 48 reintentos del mismo gránulo del 10-oct; rm: 3). Ahora es un paso
de 0 mm con cobertura 0; el binning vive en `bin_to_grid` y
`fetch_goes_qpe.py --selftest` cubre el caso vacío, el normal y la proyección.
**Retrospectiva del 8-oct (IMERG+GOES, 84 h, susc con cascada) — RESULTADO
NEGATIVO decisivo**: los DOS productos satelitales se perdieron la ráfaga del
piedemonte. En la celda de Tobalaba, 17-21Z: IMERG 0.37 mm y GOES 1.66 mm contra
12.6 mm observados (8.2 en la hora previa al aluvión) → ×34 y ×8 por debajo; en
la cicatriz IMERG 1.0 / GOES 2.6 mm. Peligro en la cicatriz ≤0.04 con Caine y
≤0.05 con a*=5.5: **el monitor con forzante satelital NO habría alertado**. Lo
único que vio la ráfaga fue el pluviómetro a 7 km; simulado el feed DGA (IDW de
las 4 EMAs) sobre la cicatriz: 5.5 mm/h → Caine 0.04 (nada), a*=5.5 0.29
(ALERTA a la hora del evento, sin lead). Por eso `monitor@rm` corre con
**ID_A=5.5 en SOMBRA** (atacama sigue en Caine): costo medido 17/168 pasos con
alguna celda en alerta en la ventana 7-9 oct (cordillera SE, sin verificar).
Conclusión que apunta a las fuentes: para ráfagas convectivas de piedemonte no
hay forzante satelital que sirva; hace falta red densa in situ (EMAs, estaciones
personales, enlaces de microondas). OJO: los fetchers miden la ventana hacia atrás desde AHORA y
GOES escribe en `WORK_DIR/steps` → para una retrospectiva el GOES va con
`WORK_DIR=$S/feeds/goes`, o mezcla sus pasos con los de IMERG (pasó).

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

**OJO: la bibliografía IMPRESA no sale de `refs.bib`** (verify-refs, 2026-10-07). Los
dos `.tex` traen un bloque `thebibliography` escrito a mano (revistas abreviadas,
"et al."), idéntico en EN y ES, y no hay `\bibliography{}`: compilar con BibTeX no lo
regenera. `refs.bib` es la fuente que verifica `/verify-refs`; cualquier corrección va
en LOS TRES (refs.bib + bloque EN + bloque ES). Cruzado entrada a entrada ese día
(año + DOI): coincidían 41/43, y las 2 distintas eran las que se corrigieron.

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
