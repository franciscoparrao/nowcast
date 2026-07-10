# Auditoría del motor nowcast — qué cambiar para perfeccionarlo

**Fecha**: 2026-07-01
**Scope**: workspace completo — `nowcast-core` (2 864 LOC, leído íntegro) + 7 adapters
(snowmelt, rainflow, hydroflux, surtgis, swarm, insar, firespread) + `nowcast-cli` +
`nowcast-python` (~5 300 LOC total). Tests 100 % verdes, clippy limpio, cero TODOs.

## Resumen ejecutivo

| Severidad | Core | Adapters | CLI/Python | Total |
|-----------|------|----------|------------|-------|
| CRITICAL  | 0    | 0        | 0          | **0** |
| HIGH      | 3    | 2        | 3          | **8** |
| MEDIUM    | 10   | 6        | 8          | **24** |
| LOW       | 9    | 12       | 9          | **30** |

**Calidad general: Excelente.** El diseño (susceptibilidad × gatillo, trait `Forcing`
de acceso aleatorio, prefix-sums, paridad bit-idéntica batch/streaming) es sólido y
la matemática verificada es correcta: conversiones de unidades de los 7 adapters
correctas y testeadas, PAV isotónico correcto, Wilson correcto, ROC-AUC por
Mann-Whitney con empates bien manejados, layout step-major consistente en todo el
stack, sin y-flip en el bridge GeoTIFF.

Los hallazgos se agrupan en cuatro temas, no en bugs dispersos:

1. **Fronteras sin blindar**: pánicos alcanzables desde input de usuario (`explain`
   OOB, NaN en `push`, prob>1 en `refined_hazard`) — lo primero que golpea un
   operador.
2. **Escalabilidad**: memoria O(celdas×pasos) del batch y complejidad del backtest
   espacial bloquean los dos próximos objetivos declarados (grillas 30 M celdas y
   skill score COOLR×IMERG catálogo-completo).
3. **Divergencia entre caminos paralelos**: el kernel I-D existe 4 veces, el parsing
   CSV 2 veces con semánticas distintas, `refined_hazard` 3 veces con 3 firmas,
   4 estrategias de error handling entre adapters.
4. **Brecha operacional**: sin salida JSON, sin exit codes, sin persistencia del
   calibrador ni del estado live, GIL retenido, listas Python en vez de numpy —
   el motor es científicamente completo pero aún no *operable* por terceros.

---

## HIGH — arreglar antes de que alguien externo lo use

### H1. `Nowcast::explain` / `hazard_at` panican con `cell`/`step` fuera de rango
- **Archivo**: `crates/nowcast-core/src/nowcast.rs:186-203`, `grid.rs:84-86`;
  expuesto en `nowcast-cli/src/main.rs:340` y `nowcast-python/src/lib.rs:126`.
- **Dimensión**: D5 Error handling.
- **Impacto**: `nowcast explain --cell 999999` → `index out of bounds` con stack
  trace; en Python es `PanicException` (no capturable como `ValueError`). Es el verbo
  cuyo propósito es que un humano inspeccione celdas arbitrarias.
- **Fix**: `explain(&self, cell, step) -> Result<Explanation>` validando
  `cell < dims.len()` y `step < forcing.n_steps()` (mismo patrón que
  `LiveNowcast::push`). Propagar a CLI (`?` con contexto) y PyO3 (`map_err`).

### H2. `LiveNowcast::push` acepta NaN/∞/negativos — un NaN envenena el motor para siempre
- **Archivo**: `crates/nowcast-core/src/live.rs:101-141`.
- **Dimensión**: D5 / correctness numérica.
- **Impacto**: `push` solo valida longitud. Un `NaN` (sentinela común en feeds
  meteorológicos) entra a `cum[c]` y al ring de prefix-sums → **todo hazard futuro de
  esa celda es NaN**, silenciosamente, en el motor diseñado para operación en vivo.
  Agravante: la CLI `watch` parsea con `parse::<f64>().ok()` que acepta `"NaN"`/`"inf"`
  (`main.rs:491-496`), mientras `run` usa `from_csv` que los filtra — el mismo CSV se
  comporta distinto según el verbo.
- **Fix**: en `push`, rechazar `!d.is_finite() || d < 0.0` con `Error::InvalidParameter`
  (la frontera operacional real). En la CLI, eliminar `column_values` y reutilizar el
  parser del core (ver M-CLI2).

### H3. El backtest espacial no escala al plan COOLR×IMERG
- **Archivo**: `crates/nowcast-core/src/backtest.rs:216-221, 238-244, 322-350`.
- **Dimensión**: D1 / rendimiento.
- **Impacto**: `alert_near_event` es un scan lineal O(|eventos| × |alertados|); con
  catálogo COOLR (~10⁴ eventos) × alertas celda-día a escala continental (10⁶–10⁸)
  son 10¹²+ comparaciones. El loop de correct-negatives es O(días × celdas) con
  lookups BTreeSet (log n) — 10⁹+ operaciones. Es exactamente el cómputo que el
  riesgo 1 del paper necesita correr.
- **Fix**: (a) para hits, enumerar la ventana espacio-temporal del evento y consultar
  membresía en `alerted` — O(|eventos| × radio² × tol), el mismo truco que ya usa el
  footprint; (b) correct-negatives por aritmética de conjuntos:
  `CN = n_unidades − |alerted ∪ footprint|` (restringidos al período), sin doble loop;
  (c) considerar `HashSet`/bitset en vez de `BTreeSet` (no se usa el orden).

### H4. `Inundation::integrate` trunca silenciosamente (hydroflux)
- **Archivo**: `crates/nowcast-hydroflux/src/lib.rs:127`.
- **Impacto**: si el cap de 200 000 pasos se alcanza (CFL pequeño), devuelve una
  inundación **parcial presentada como completa**. En un sistema de alerta, resultado
  incorrecto sin señal.
- **Fix**: devolver `IntegrationStats { t_reached, steps, truncated }` junto al campo,
  y builders `with_max_steps`/`with_dry_tol` + validación de `duration_s`/`cfl`.

### H5. `Runout::refined_hazard` panica con prob fuera de [0,1] (swarm)
- **Archivo**: `crates/nowcast-swarm/src/lib.rs:92`.
- **Impacto**: `expect` sobre input del caller sin validar — `prob = 1.0001` (salida
  típica de una calibración) → panic de librería. El mismo concepto tiene 3 firmas
  distintas en hydroflux/swarm/firespread (ver T3).
- **Fix**: `nowcast_core::mask_hazard(step, dims, mask, prob) -> Result<HazardField>`
  compartido; los tres adapters lo consumen y el panic desaparece.

### H6. CLI: `--susc raster.tif --rain-csv serie.csv` no funciona sin `--ncols/--nrows` manuales
- **Archivo**: `crates/nowcast-cli/src/main.rs:215-218, 245-251`.
- **Impacto**: la invocación operacional más natural falla con un mensaje que culpa a
  una grilla 1×1 que el usuario nunca pidió; el comentario del código promete un
  emparejamiento que nunca se implementó.
- **Fix**: resolver primero la susceptibilidad → derivar dims del raster → construir
  el `UniformRain` con esas dims.

### H7. Python: las grillas cruzan la frontera como listas — inviable a escala real
- **Archivo**: `crates/nowcast-python/src/lib.rs:46-49, 107-113, 180-183`.
- **Impacto**: el raster RF del Maipo (30 M celdas) ⇒ ~30 M objetos `PyFloat` por paso
  de salida. El binding funciona para juguetes, no para el pipeline Python que lo
  motiva (physics-guided-ml).
- **Fix**: `rust-numpy` (compatible abi3): `PyReadonlyArray1<f64>` de entrada,
  `PyArray2` (steps × cells) de salida, y `py.allow_threads` alrededor de
  `run()`/`push()` (hoy retienen el GIL durante todo el cómputo).

### H8. Dependencias por path a 6 workspaces hermanos — el repo público no compila solo
- **Archivo**: `Cargo.toml` de los 7 adapters (`../../../snowmelt-rs/...`, etc.).
- **Impacto**: un checkout limpio del repo (el que descargará quien siga el DOI de
  Zenodo) no compila ningún adapter. Afecta directamente el claim de reproducibilidad
  del paper.
- **Fix**: git-dependencies con `rev` pineado, o documentar el layout de hermanos
  requerido en README + verificarlo en CI. Mínimo antes del release Zenodo.

---

## MEDIUM — deuda que conviene pagar pronto

### Core

- **M1. Kernel I-D implementado 4 veces**: `nowcast.rs::max_exceedance_at` +
  `dominant_window`, `multi.rs::IdTrigger::factor` (multi.rs:83-97), y el loop interno
  de `live.rs::push` (live.rs:125-136). El test de paridad protege live↔batch pero
  `IdTrigger` no tiene paridad contra `Nowcast`. Cualquier cambio al esquema de
  ventanas debe tocar 4 sitios en sincronía. **Fix**: un kernel único
  `id_exceedance(threshold, dt, window, prefix_now, prefix_window) -> f64` que los
  cuatro llamen, + test de paridad `IdTrigger` ↔ `Nowcast`.
- **M2. `explain()` recomputa los prefix-sums de TODA la grilla** para explicar UNA
  celda (`nowcast.rs:187`). En una grilla real son GB de trabajo para una consulta
  puntual. **Fix**: prefix de la celda consultada solamente — O(n_steps).
- **M3. `hazard_at()` recomputa los prefix-sums completos en cada llamada**
  (`nowcast.rs:219-222`). Un caller que itere pasos paga O(pasos² × celdas). **Fix**:
  cachear los prefix (lazy `OnceCell`) o exponer un tipo `PreparedNowcast`.
- **M4. Memoria O(celdas × pasos) como `Vec<Vec<f64>>`** (`nowcast.rs:153-163`):
  30 M celdas × 365 pasos ≈ 88 GB — hoy el mitigador es "coarsen primero" (documentado
  solo en CLAUDE.md). Además `run()` materializa todos los `HazardField`. **Fix**:
  (a) prefix plano (una asignación, mejor localidad); (b) API streaming
  `run_with(|field| ...)` que no materializa la serie completa — `alerts()` la usaría
  gratis; (c) documentar el límite de memoria en el doc de `Nowcast`.
- **M5. El contrato del trait `Trigger` ([0,1]) no se puede hacer cumplir**:
  `MultiNowcast::hazard_at` hace `.expect("hazard within [0,1]")` (multi.rs:223) —
  un `Trigger` de terceros que devuelva 1.5 panica la librería. **Fix**: clampear el
  factor combinado o devolver `Result`.
- **M6. `UniformRain`/`GriddedRain` aceptan `+inf`** (`forcing.rs:59, 157`: el chequeo
  es `< 0.0 || is_nan`, `+inf` pasa). Inconsistente con `from_csv`, que filtra
  no-finitos. **Fix**: `!d.is_finite() || *d < 0.0`.
- **M7. `from_csv` salta líneas no parseables en silencio** (`forcing.rs:90-94`):
  tolera headers, pero también **desalinea el eje temporal** si la serie tiene huecos
  — cada línea saltada corre todas las fechas siguientes un paso. Para backtesting
  contra eventos fechados es un riesgo real de correctness. **Fix**: devolver también
  el conteo de líneas saltadas (o un `Vec<usize>` de índices), y que la CLI lo
  advierta; opcionalmente modo estricto.
- **M8. Panics por `assert!` en la API de verificación** (`backtest.rs:102, 365, 419`):
  `monthly_contingency`, `roc_auc` y `pod_at_area` panican por longitud desigual
  mientras el resto del crate devuelve `Result`. `brier_score` es peor: **trunca
  silenciosamente** con `zip` y divide por `preds.len()` (`calibrate.rs:145-157`) —
  score incorrecto sin error, en la función que valida el claim de calibración.
  **Fix**: `Result` en las cuatro, o al menos validar longitud en `brier_score`.
- **M9. `Calibrator` no es persistible** (`calibrate.rs:26-31`: `xs`/`ys` privados,
  sin getters ni serde): no se puede ajustar offline y aplicar en `watch`/producción.
  **Fix**: getters + feature opcional `serde`; habilita además el verbo `calibrate`
  de la CLI (M-CLI4).
- **M10. Paralelismo incompleto**: solo `Nowcast::run` usa Rayon. `ensemble_hazard`
  corre miembros en serie (ensemble.rs:98), `MultiNowcast::run` es serial, el loop
  por celda de `push` es vergonzosamente paralelo. **Fix**: extender el feature
  `parallel` a los tres (miembros del ensemble es el de mayor retorno).

### Adapters (transversales)

- **M11. Error handling con 4 estrategias distintas**: rainflow/surtgis con enum
  propio (bien), snowmelt reusa el del motor, firespread **abusa** de
  `Error::InvalidParameter{name:"firespread"}` para fallas de simulación,
  hydroflux/swarm sin ningún `Result`. **Fix**: variant
  `Error::Engine { engine, source }` en el core o enum propio por adapter.
- **M12. `DeformationForcing` es un `Forcing` semánticamente trampa** (insar:84-97):
  `depth_mm` devuelve una **tasa** (mm/yr). Con `ThresholdTrigger` funciona; nada
  impide enchufarla a `Nowcast`/`IdTrigger`, que acumularía tasas como láminas —
  silenciosamente. **Fix mínimo**: doc-warning; fix real: separar `DepthForcing` /
  `RateForcing` en el sistema de tipos (ver T2).
- **M13. `gridded_rain_from_rasters` no verifica georreferenciación coherente**
  (surtgis:100-121): valida shape pero no `GeoTransform`/CRS — un tile de otra grilla
  con el mismo shape se apila y desalinea lluvia vs susceptibilidad sin error. Es el
  bug con más probabilidad de morder con datos reales mezclados (CR2MET + IMERG).
  **Fix**: comparar transform (tolerancia f64) y CRS contra el raster 0.
- **M14. Contrato fuera-de-rango de `Forcing` sin documentar**: todos los impls
  panican por slice-index si `step >= n_steps` o `cell >= len`. Elección defendible
  pero no escrita. **Fix**: sección `# Panics` en `forcing.rs:23` y en los impls.

### CLI / Python

- **M-CLI1. Sin salida estructurada ni exit codes**: todo es tabla ad-hoc por stdout
  y `main` retorna 0 haya o no alertas — no scriptable (`main.rs:298-330, 394-409`).
  **Fix**: `--format json` (los structs ya son planos; requiere serde opcional en el
  core) + exit code 2 con ≥1 alerta.
- **M-CLI2. Parsing CSV duplicado CLI↔core con semánticas distintas**
  (`main.rs:491-532` vs `forcing.rs:78-102`): causa raíz del agravante de H2. `month_keys`
  y `read_events` (inventario SERNAGEOMIN) son conocimiento de dominio que pertenece
  a `nowcast-core::backtest`, donde sería testeable y accesible desde Python. **Fix**:
  mover los tres helpers al core.
- **M-CLI3. `watch` no es live**: lee el CSV completo y lo reproduce (`main.rs:357`);
  es `run` con motor streaming. El core ya tiene `StepSource`. **Fix**: `--rain-csv -`
  (stdin) y/o `--follow` (tail) — convierte el verbo en lo que su nombre promete y
  cierra parte de la limitación iii del paper.
- **M-CLI4. Falta el verbo `calibrate`**: la calibración isotónica — feature destacada
  del proyecto — no es accesible desde la CLI ni persistible (depende de M9).
- **M-PY1. Paridad Python incompleta**: sin `intensity_to_alert` (la mitad
  contrafactual de la historia XAI), sin alert-info en el retorno de `push`, sin
  `monthly_contingency`/backtest, sin ensemble ni multi-trigger. El pipeline de
  validación científica es Python; hoy backtest y ensemble solo se ejercen desde
  Rust. **Fix**: priorizar `intensity_to_alert` (trivial), alertas en `push`, y
  `monthly_contingency`.

---

## LOW — lista breve (detalle en los reportes de origen)

**Core**: `fit_isotonic` panica con NaN (`partial_cmp().unwrap()`, calibrate.rs:51);
`run_live` compara `dt_hours` con `!=` exacto de f64 (live.rs:207); `GridDims::new`
acepta 0 (grid.rs:18); `intensity_to_alert(level=0)` → `Some(-inf)` (nowcast.rs:208);
tol de `Driver::classify` hardcodeada en 0.15 (explain.rs:85); `HazardField.step`
público mientras el resto es privado; `roc_auc` trata NaN como empate silencioso;
`LiveNowcast` sin `explain()` (paridad XAI batch/live incompleta); `EnsembleField`
sin `alert()` análogo a `HazardField`.

**Adapters**: `FloodThreshold::quantile` acepta `p=0.0` contra su doc (rainflow:142);
`discharge_to_alert(0.0)` → `Some(-inf)` (rainflow:267); validación de descarga
copy-pasteada (rainflow:65 vs 178); `+∞` pasa los guards de firespread (86, 144);
`burned_mask()` realoca en cada llamada (firespread:74); `mean_depth` de grid vacío
→ NaN (hydroflux:71); `dt` CFL pre-fuente (hydroflux:128); `run()` consume el
`SnowModel` (snowmelt:82); `run_runout` descarta los pasos realmente ejecutados
(swarm:101); constructores insar sin validar `n_steps`/`dt`; `ndarray` repetido en
4 manifests en vez de `workspace.dependencies`.

**CLI/Python**: doc dice "three verbs", hay cuatro; `parse_sweep` acepta `"nan"`;
`--cell` exige índice plano (aceptar `--row/--col` o `--lon/--lat`); `--ncols 0` pasa;
módulo Python sin `__doc__`/`__version__`; `reliability` sin default `n_bins=10`;
pyo3 0.23 fosilizándose.

---

## Arquitectura — evaluación y refactors transversales

**Lo que está bien y no hay que tocar**: separación core/adapters impecable (flujo
unidireccional, el core no conoce a nadie); trait `Forcing` en la frontera correcta
— quedó demostrado con 7 proveedores intercambiables; core dep-free (`std` +
`thiserror`) con el gate offline; paridad bit-idéntica batch/streaming *testeada a
nivel de bits*; validación en frontera consistente en constructores; naming coherente;
cero deuda declarada (sin TODOs, clippy limpio); 21+21 tests verdes con casos de
borde reales.

**T1. Unificar el kernel I-D** (M1): una función, cuatro consumidores, dos tests de
paridad. Es el refactor con mejor razón beneficio/costo del workspace: elimina la
clase entera de bugs "cambié la ventana en 3 de 4 sitios".

**T2. Tipar la semántica de la señal**: hoy `depth_mm` transporta láminas (mm),
tasas (mm/yr) y caudales (mm/día) según el adapter, y solo la disciplina del usuario
evita mezclas sin sentido (M12). Opciones en orden de costo: doc-warnings → traits
separados (`DepthForcing`/`RateForcing`) → newtype de unidad. Para un motor que
aspira a que terceros escriban proveedores, el sistema de tipos es la documentación
que no se puede ignorar.

**T3. Subir al core los patrones triplicados de los adapters**:
`mask_hazard` (3 firmas de `refined_hazard` → 1, resuelve H5), saneo de campo crudo
(3 copias con políticas ligeramente distintas), `Forcing` bufferizado step-major
(`SnowmeltForcing` es estructuralmente `GriddedRain`; un `BufferedForcing` core
serviría también a los futuros QPE/ensemble), y `flatten_row_major` (3 copias).
Cada nuevo proveedor hoy re-implementa la indexación a mano — que es exactamente
donde nacería el próximo off-by-one.

**T4. Serde opcional en el core** (`Alert`, `Contingency`, `Explanation`,
`Calibrator`, `Reliability`): habilita de una vez la salida JSON de la CLI (M-CLI1),
la persistencia del calibrador (M9) y checkpoints del estado live. Feature-gated
para no romper el gate offline.

---

## Hacia "el mejor motor de predicción" — mejoras científicas

Ordenadas por retorno sobre el estado del arte (complementa `docs/sota-roadmap.md`):

1. **Humedad antecedente como señal de primera clase.** Las ventanas I-D ≤ 7 días
   capturan la tormenta, no el estado del suelo. El SOTA (LHASA 2.0 con antecedente;
   Bogaard & Greco 2018, "recipe" causa-gatillo; CTRL-T dual-threshold ya citado vía
   melillo2018) usa umbral dual: antecedente + evento. La infraestructura ya existe
   — un `AntecedentTrigger` (media exponencial/API sobre el mismo prefix-sum) fusionado
   por `Combine::Product` con el I-D sería ~100 LOC y ataca directamente el FAR ~0.9
   estructural del Maipo.
2. **Umbrales espacialmente variables.** `IdThreshold` es un `(a,b)` global; la
   literatura regional muestra variación fuerte por litología/clima. Un
   `IdThresholdMap` (a,b por celda, mismo patrón que `SusceptibilityMap`) permitiría
   ingerir los umbrales regionalizados sin tocar el kernel.
3. **Calibración integrada al pipeline, no colgada al lado.** Hoy índice → (manual)
   → `Calibrator`. Un `Nowcast::with_calibrator(cal)` que emita probabilidad calibrada
   directamente cerraría el loop y haría que TODOS los consumidores (CLI, Python,
   ensemble) hablen probabilidad real. Requiere M9.
4. **Incertidumbre de parámetros, no solo de forzante.** El ensemble muestrea la
   lluvia; `(a, b, k)` son puntuales. Correr el ensemble sobre el producto
   forzante × parámetros (perturbación simple de a,b) daría bandas de confianza del
   umbral mismo — barato con la maquinaria existente.
5. **Métricas para base rate bajo**: con eventos a ~4 %, ROC-AUC es optimista
   (ya lo viste: 0.48 "informativo"). Añadir **PR-AUC** y **lead-time medio**
   (distancia alerta→evento en pasos, ya casi computable desde
   `spatial_daily_contingency`) al módulo backtest. Es además lo que los reviewers
   de NHESS/C&G esperan en verificación EWS.
6. **Persistencia y recuperación del estado live** (checkpoint del ring de
   prefix-sums): hoy un reinicio del proceso operacional pierde la historia de
   ventanas — con `max_window=7 días` son 7 días ciegos. Con T4 es serialización
   directa.
7. **Pesos por miembro del ensemble** (`ensemble_hazard` trata los miembros como
   equiprobables): los QPF reales (pySTEPS) traen verosimilitudes; un
   `Vec<(F, f64)>` generaliza sin costo.

---

## Plan de acción priorizado

| # | Qué | Resuelve | Esfuerzo |
|---|-----|----------|----------|
| 1 | ✅ (2026-07-02) Blindar fronteras: `explain` → `Result` (+ variant `Error::OutOfRange`), `push` valida no-finitos/negativos, `HazardField::masked` en core consumido por swarm y hydroflux, clamp en `MultiNowcast::hazard_at` | H1, H2, H5, M5 | S |
| 2 | ✅ (2026-07-02) Reproducibilidad: tabla exacta de hermanos en README (corrigió el claim falso "same parent" para hydroflux) + `scripts/check_siblings.sh` + `ndarray` en `workspace.dependencies` | H8 | S — **antes del DOI Zenodo** |
| 3 | ✅ (2026-07-02) CLI: `resolve_inputs` (el raster de susceptibilidad fija la grilla del gauge CSV; `--ncols/--nrows` contradictorios fallan claro) + parsers únicos en core (`csv_column` filtra no-finitos, `csv_month_keys`, `csv_events`) — `watch` ya no acepta `NaN` | H6, M-CLI2, parte de H2 | S |
| 4 | ✅ (2026-07-02) Kernel I-D único `IdThreshold::worst_window` (4 sitios → 1) + test de paridad bit-idéntica `IdTrigger`↔`Nowcast` + test de trigger fuera de contrato | M1 | S |
| 5 | ✅ (2026-07-02) Prefijos: buffer plano `n_cells×(n_steps+1)` cacheado con `OnceLock` (compartido por `run`/`hazard_at`/`explain`); `explain` frío prefija SOLO la celda consultada (O(n_steps)); `IdTrigger` con el mismo layout plano. Paridad bit-idéntica re-verificada + test frío/caliente | M2, M3, M4a | M |
| 6 | ✅ (2026-07-02) Backtest espacial escalable: hits por enumeración de la ventana del evento (`chebyshev_window`), FA en una pasada, CN por aritmética de conjuntos; `Contingency` a `u64` (overflow a escala catálogo, no lo marcó la auditoría). Test de equivalencia vs referencia naive + `examples/bench_backtest.rs`: 200k celdas × 3650 días × 1M alertas × 5k eventos → **0.94 s** (r=1,±1d) / 1.79 s (r=2,±3d) | H3 — **prerequisito del paper 2 (COOLR×IMERG)** | M |
| 7 | ✅ (2026-07-02) Feature `serde` en core (Alert/Contingency/Explanation/Driver/Calibrator/Reliability) + CLI: `--format json` en los 5 verbos (`watch` emite JSON Lines), exit code 2 con ≥1 alerta en run/watch, verbo `calibrate` (fit isotónico desde CSV → JSON persistible) y `--calibrator` en run/watch. También M8: `brier_score` → `Result` (el truncamiento silencioso murió) | T4, M-CLI1, M-CLI4, M9, M8 | M |
| 8 | ✅ (2026-07-02) Python: numpy end-to-end (susceptibilidad/lluvia 1-D, gridded 2-D steps×cells, `run()` → ndarray), `allow_threads` en run/push, `intensity_to_alert`, alert-info opcional en `push`, y el toolbox de verificación completo (`monthly_contingency`, `spatial_daily_contingency`, `roc_auc`, `pr_auc`, `pod_at_area`, `lead_times`) — el pipeline COOLR puede correr desde Python. Wheel verificado con round-trip real | H7, M-PY1 | M |
| 9 | ✅ (2026-07-02) hydroflux: `IntegrationStats{t_reached,steps,truncated}` (una corrida capada ya no miente) + builders `with_max_steps`/`with_dry_tol` + validación en `new`/`with_cfl`. surtgis: check de geotransform/CRS en `gridded_rain_from_rasters`. Core: variant `Error::Engine`; firespread deja de abusar `InvalidParameter` y sus guards rechazan `+∞`. swarm: `run_runout` valida `pixel_size` | H4, M6, M13, M11 | M |
| 10 | ✅ (2026-07-02) `AntecedentTrigger` (API con decaimiento exponencial, excluye el paso actual — dual-threshold Bogaard-Greco vía `Combine::Product`), `IdMapTrigger` ((a,b) por celda, paridad bit-idéntica con `IdTrigger` en mapa uniforme), `pr_auc` (average precision con empates — la métrica honesta a base rate 4%) y `lead_times` (lead por evento, coherente con el matcher espacial). Example `dual_threshold.rs`: FA 111→58 (−48%) con POD 12/12 y lead intactos | SOTA 1, 2, 5 | L — **material del paper 2** |

Esfuerzo: S < ½ día, M ≈ 1-2 días, L ≈ semana.

Los ítems 1-4 son pre-submission razonables (pequeños, suben la calidad percibida del
artefacto que el reviewer va a clonar). Los ítems 5-9 son la v0.3. El ítem 10 es la
agenda científica del segundo paper.

---

## Ronda 2 — auditoría independiente de verificación (2026-07-05)

**Motivación**: no hubo commits de código en `crates/` entre `0a7554e` (el fix de
la ronda 1) y hoy — esta no es una auditoría de código nuevo, sino una segunda
pasada adversarial e independiente para (a) verificar que los 10 ítems marcados
✅ arriba realmente sostienen bajo re-lectura escéptica del código actual, no solo
de los nombres de los tests, y (b) encontrar lo que la primera pasada pudo haber
dejado pasar. Ejecutada con 3 agentes en paralelo (core / 7 adapters / CLI+Python),
cada uno re-derivando la matemática y re-probando los casos límite a mano, más
verificación manual propia de una muestra de los hallazgos de mayor severidad.
`cargo test --workspace --exclude nowcast-python` y
`cargo clippy --workspace --exclude nowcast-python -- -D warnings`: 100% verde.

### Resumen ejecutivo

| Severidad | Hallazgos nuevos |
|-----------|------------------|
| CRITICAL  | **0** |
| HIGH      | **4** |
| MEDIUM    | **7** |
| LOW       | **5** |

**Veredicto sobre la ronda 1**: honesto y en su mayoría sólido. H3 (backtest
escalable), H4 (truncamiento hydroflux), H5 (panic swarm, además correctamente
*unificado* vía `HazardField::masked` compartido con hydroflux — cierra T3), H6
(CLI raster+CSV) y la migración a numpy están **genuinamente arreglados**, no solo
declarados. La matemática (parity live/batch, PAV isotónico, Wilson, ROC-AUC,
ensemble, `Combine`) fue re-derivada a mano y es correcta.

Pero aparece un patrón que se repite tres veces: **se blindó la función nombrada
en el hallazgo y se dejó sin blindar a su hermana igual de alcanzable** — mismo
espíritu que el "sibling drift" de `swarm-abm` que ya rompió el build una vez.

### HIGH — nuevos

**N-H1. `Nowcast::hazard_at` / `MultiNowcast::hazard_at` panican con `step` fuera
de rango.** `crates/nowcast-core/src/nowcast.rs:297`, `multi.rs:375`. H1 blindó
`explain` (mismo archivo, valida `cell`/`step` y devuelve `Result`) pero
`hazard_at` — nombrada en el texto original de H1 — sigue indexando sin chequeo.
Hoy no está expuesta ni por CLI ni por Python (verificado: `grep hazard_at` en
ambos no da resultados), así que es una API pública sin blindar, no un panic
explotable *hoy*. Fix: mismo patrón que `explain`, devolver `Result<HazardField>`.

**N-H2. `Nowcast::intensity_to_alert` panica con `cell` fuera de rango — y sí es
alcanzable desde Python hoy.** `nowcast.rs:284` indexa
`self.susceptibility.get(cell)` sin validar; `nowcast-python/src/lib.rs:194-199`
reenvía un `cell: usize` crudo desde Python sin chequeo propio. Verificado
leyendo ambos archivos: `nowcast.intensity_to_alert(cell=999999999, ...)` desde
Python es un `PanicException` no capturable — exactamente el modo de falla que H1
existía para eliminar, reintroducido por una función (el contrafactual XAI)
añadida después del fix. Adicionalmente, `duration_h` no positivo o `NaN` no se
rechaza: produce silenciosamente `Some(NaN)`/`Some(inf)`. Fix: validar `cell <
n_cells` y `duration_h > 0.0 && duration_h.is_finite()` al entrar, mapear a
`ValueError` en el binding (mismo patrón ya usado en `explain`).

**N-H3. `Calibrator` deserializado sin validar → dos panics reproducibles desde
`--calibrator` malformado.** `crates/nowcast-core/src/calibrate.rs:30` (comentario
propio: *"Deserialized data is trusted to be engine-produced"* — pero
`serde_json::from_str` en `nowcast-cli/src/main.rs:287` acepta cualquier JSON con
esa forma). Reproducido: (a) `{"xs":[0.1,0.9],"ys":[1.5,2.0]}` →
`calibrate_field`'s `.expect("calibrated probabilities stay within [0,1]")`
(`main.rs:299`) panica porque `HazardField::new` rechaza probabilidad fuera de
rango; (b) `{"xs":[],"ys":[]}` → `xs.len() - 1` (`calibrate.rs:93`) resta con
underflow sobre un vector vacío → panic de indexación. Exit code 101 (panic), no
el 1/2 documentado. Fix: `Calibrator::validate()` tras deserializar — `ys`
monótona no-decreciente en [0,1], `xs.len() == ys.len() >= 1`.

**N-H4. `nowcast-rainflow`: `hazard_at`/`explain`/`discharge_to_alert` nunca
recibieron el fix de H1, pese a ser su espejo declarado.** `crates/nowcast-rainflow/src/lib.rs:218,246,267`
indexan `self.discharge_mm_day[step]` y `self.susceptibility.get(cell)` sin
chequeo. Confirmado con `git diff 5142960 0a7554e -- crates/nowcast-rainflow/src/lib.rs`
(vacío): el fix commit no tocó este archivo, aunque el propio doc-comment de
`explain` dice *"el contrapartida de forma cerrada del `Nowcast::explain` de
deslizamientos"* — se construyó explícitamente como espejo de la función que H1
arregló, y quedó fuera. No conectado a CLI/Python hoy (riesgo latente, no
explotable ahora). Fix: mismo patrón `Result<_, Error::OutOfRange>`.

### MEDIUM — nuevos

**N-M1. `UniformRain::new`/`GriddedRain::new` rechazan `NaN` pero no `+Infinity`.**
`crates/nowcast-core/src/forcing.rs:80,167`: guarda `d < 0.0 || d.is_nan()`, sin
`is_finite()`. Verificado: `UniformRain::new(dims, 24.0, vec![f64::INFINITY])`
compila y corre. La CLI está protegida porque su único camino a estos
constructores pasa por `csv_column` (que sí filtra no-finitos), pero
`nowcast-python/src/lib.rs:91,130` llama a estos constructores directo con
floats crudos de numpy, sin ese filtro — mismo problema de fondo que H2 cerró en
`LiveNowcast::push`, un nivel más abajo de donde miró la ronda 1.

**N-M2. El mismo hueco de `+Infinity` es alcanzable desde un GeoTIFF real vía
`nowcast-surtgis::gridded_rain_from_rasters`.** `crates/nowcast-surtgis/src/lib.rs:146-152`:
`v.is_nan() ... else { (v as f64).max(0.0) }` — no chequea `is_infinite()` antes
de pasar a `GriddedRain::new` (que tampoco lo hace, ver N-M1). Un solo píxel
corrupto (`+inf`) en un CR2MET/IMERG real satura la excedencia I-D de esa celda a
`+inf` → alerta de "certeza máxima" silenciosa, no un crash. Fix: tratar
no-finito igual que NaN/nodata en ambos sitios.

**N-M3. `Calibrator::fit_isotonic` panica con `NaN` en los scores.**
`calibrate.rs:56`: `scores[a].partial_cmp(&scores[b]).unwrap()`. Alcanzable desde
Python con un `Vec<f64>` crudo (`lib.rs:275`). Fix: rechazar no-finitos o usar
`total_cmp`.

**N-M4. `brier_score`/`reliability` validan longitud pero no finitud** — un solo
`NaN` (alcanzable vía Python) envenena silenciosamente el score completo, en el
módulo cuyo propósito es *verificar* la calibración.

**N-M5. `pr_auc` (nueva esta ronda) reintroduce el patrón `assert_eq!` que M8
buscaba eliminar** — junto con `roc_auc`/`pod_at_area`/`monthly_contingency`, que
M8 nunca cubrió (su alcance declarado era solo `brier_score`).

**N-M6. `DepthField::from_states` (hydroflux) disfraza `NaN` de "seco".**
`crates/nowcast-hydroflux/src/lib.rs:48`: `states[[i,j]].h.max(0.0)` — en Rust
`NaN.max(0.0) == 0.0` (verificado empíricamente), así que una celda donde el
solver shallow-water se vuelve inestable numéricamente se reporta como
profundidad 0 en vez de error, en la ruta cuyo propósito es refinar alertas.
`+Inf` no sufre este problema (`Inf.max(0.0) == Inf`). Fix: chequear
`!s.h.is_finite()` tras integrar y exponerlo en `IntegrationStats`.

**N-M7. Deriva de versión `surtgis-core` — el mismo tipo `GeoTransform` es dos
tipos distintos para el compilador. CORRECCIÓN 2026-07-05: la atribución
original (abajo) señalaba a `insar-core`; verificado con
`cargo tree -i surtgis-core@0.16.3` / `@0.17.0`, el culprit real es
`hydroflux-solver-2d`.** `insar-core` en realidad resuelve correctamente a la
misma v0.17.0 local (su Cargo.toml raíz declara
`surtgis-core = { version = "0.17", path = "../surtgis/crates/core", ... }`,
apuntando al mismo checkout que usa `nowcast`) — no hay drift ahí. El drift real
es que `/home/franciscoparrao/proyectos/postdoc/hydroflux/Cargo.toml:41` fija
`surtgis-core` a un rev de git antiguo (v0.16.3) con un comentario del propio
autor reconociendo el atajo ("si testeas contra un checkout local de surtgis,
cambia temporalmente a path=..."). `hydroflux-solver-2d::io` (no expuesto hoy por
`nowcast-hydroflux`, que solo re-exporta `Mesh2D`/`Boundary`/`Conserved2D`/
`PointSource`/`H_DRY`) usa `surtgis_core::{Raster, GeoTransform}` de esa versión
vieja para su propio bridge GeoTIFF. Hoy esto es enteramente latente — ningún
código de `nowcast` toca ese módulo `io`, así que no hay error de compilación
actual — pero es la misma familia de mina que el rename de `swarm-abm` que ya
rompió la resolución del workspace una vez: el día que `nowcast-hydroflux` quiera
cargar un DEM real vía el helper de `hydroflux_solver_2d::io` y combinarlo con un
`Georef`/`Raster` de `nowcast-surtgis` (v0.17.0), sería exactamente el mismo error
de tipos confuso ("`GeoTransform` esperado, `GeoTransform` encontrado"). Fix:
actualizar el pin de `hydroflux`'s propio `Cargo.toml` (fuera del repo de
`nowcast` — requiere tocar `~/proyectos/postdoc/hydroflux/`) a la v0.17 local o
a un rev de git más reciente que coincida.

### LOW — nuevos o aún abiertos

- `--ncols 0`/`--nrows 0` aceptados en silencio (`GridDims::new` sin validar) —
  ya estaba en la lista LOW de la ronda 1, sin cambios.
- `spatial_monthly_contingency`/`spatial_daily_contingency`: el loop de falsas
  alarmas no filtra `ac < n_cells`, así que un índice de celda fuera de grilla en
  el set `alerted` infla el FAR silenciosamente (`backtest.rs:276,369`).
- `chebyshev_window` (`backtest.rs:193`): sin cota superior en `cell_radius` —
  `(2r+1)²` puede desbordar o pedir una asignación enorme.
- Los `Trigger` (`IdTrigger`, `IdMapTrigger`, `AntecedentTrigger`,
  `ThresholdTrigger`) no validan bounds en `factor()` — mismo contrato de panic
  no documentado que M14 señaló para `Forcing`, pero en un trait añadido después
  de la ronda 1.
- `discharge_to_inflow_m3s` (hydroflux) no valida `area_km2` — negativo/cero/NaN
  produce un inflow sin sentido pasado directo a un `PointSource`.

### Qué se confirmó sólido (sin regresión)

Conversiones de unidades de los 7 adapters (re-verificadas con números a mano);
round-trip GeoTIFF de surtgis (bandas, y-flip, nodata, clamp); guards `+∞` de
firespread; validación de `pixel_size` en `run_runout`; `post_fire_susceptibility`
acotado correctamente; paridad bit-idéntica live/batch (re-derivada, no solo
asertada); álgebra de `Combine` en los bordes; PAV isotónico y Wilson.

### Plan de acción — ronda 2

| # | Qué | Resuelve | Esfuerzo |
|---|-----|----------|----------|
| 1 | ✅ (2026-07-05) Blindar `hazard_at` (core + multi), `intensity_to_alert` (+ `duration_h`), y el trío de `nowcast-rainflow` (`hazard_at`/`explain`/`discharge_to_alert`) con el mismo patrón `Result<_, Error::OutOfRange>` que `explain` | N-H1, N-H2, N-H4 | S |
| 2 | ✅ (2026-07-05) `Calibrator::validate()` post-deserialize (monotonía, rango [0,1], no vacío) + wire en CLI (Python no deserializa calibradores hoy, no hay path que blindar ahí) | N-H3 | S |
| 3 | ✅ (2026-07-05) `is_finite()` en el guard de `UniformRain::new`/`GriddedRain::new` + no-finito tratado como nodata en `gridded_rain_from_rasters` | N-M1, N-M2 | S |
| 4 | ✅ (2026-07-05) `fit_isotonic`/`brier_score`/`reliability`: validan finitud de scores/preds | N-M3, N-M4 | S |
| 5 | ✅ (2026-07-05) `pr_auc`/`roc_auc`/`pod_at_area`/`monthly_contingency`: `Result` en vez de `assert_eq!` (completa lo que M8 dejó a medias); Python ya validaba longitud por su cuenta, ahora delega en el core y se simplifica | N-M5 | S |
| 6 | ✅ (2026-07-05) `DepthField::from_states` detecta `NaN`/`inf` post-integración y lo expone en `IntegrationStats::unstable` en vez de disfrazarlo de "seco" | N-M6 | S |
| 7 | **Documentado, no aplicado.** Corregida la atribución: el culprit real es `hydroflux-solver-2d` (no `insar-core`, que ya resuelve a v0.17 correctamente). El fix vive en `~/proyectos/postdoc/hydroflux/Cargo.toml` — un repo hermano fuera de `nowcast` cuyo pin a un rev de git antiguo parece deliberado (comentario del propio autor). Requiere confirmación del usuario antes de tocarlo — no se hizo en esta ronda. | N-M7 | S — repo externo |

Esfuerzo: S < ½ día, M ≈ 1-2 días. Ítems 1-6 verificados: `cargo test --workspace
--exclude nowcast-python` y `cargo clippy --workspace --exclude nowcast-python
--all-targets -- -D warnings` 100% verde tras cada cambio; `nowcast-python` build
+ clippy también verificados aparte. Los dos panics de `--calibrator` malformado
(N-H3) se reprodujeron contra el binario real antes y después del fix. Ningún
ítem era CRITICAL ni bloqueaba el paper.

---

## Ronda 3 — auditoría independiente (2026-07-09)

**Motivación**: tercera pasada adversarial tras los fixes de la ronda 2
(`d4669d8`). Mismo protocolo: 3 agentes en paralelo (core / 7 adapters /
CLI+Python), cada uno leyendo este documento y los commits de fixes antes de
auditar para no re-reportar ítems resueltos. Todos los hallazgos fueron
**reproducidos en ejecución** (tests mínimos en scratchpad, binario CLI real
con inputs hostiles, wheel Python construido con maturin y ejercitado con
numpy real), no solo señalados por lectura. Verificación cruzada contra el
código fuente de los 6 motores hermanos (solo lectura). Línea base: tests de
las 10 crates + Python 100% verdes; clippy `--all-targets -- -D warnings`
verde; `scripts/check_siblings.sh` pasa.

### Resumen ejecutivo

| Severidad | Hallazgos únicos |
|-----------|------------------|
| CRITICAL  | **0** |
| HIGH      | **3** |
| MEDIUM    | **11** |
| LOW       | **11** |

(27 reportados por los agentes; 2 duplicados entre reportes — el panic de
`Calibrator::probability(NaN)` y el `alert_level` NaN — se cuentan una vez.)

**Veredicto sobre la ronda 2**: los 6 fixes sostienen bajo re-lectura y
re-ejecución (trío rainflow con tests OOB, calibrador malformado → exit 1
limpio, ±inf rechazado en `gridded_rain_from_rasters`, `from_states` expone
NaN, validación de métricas delegada al core). La paridad triple sigue
bit-idéntica: `run` ↔ `watch`, CLI ↔ Python, y `Nowcast.run()` ↔
`LiveNowcast.push()` (verificado con `np.array_equal` sobre el wheel real).

**El patrón "hermana sin blindar" reincide por tercera ronda consecutiva**
(R3-H2: se blindó `fit_isotonic`, quedó `probability` del mismo struct;
R3-M3: se blindó `hazard_at`, quedó `trigger_factors` del mismo struct;
R3-M8: se blindó `gridded_rain_from_rasters`, quedó
`susceptibility_from_raster` del mismo archivo; R3-M10: se blindó
`run_runout`, quedó `from_footprint`). La lección transversal: **cada fix de
frontera debe auditarse a nivel de struct/módulo completo, no de función
nombrada** — ver "Plan de acción" ítem 8.

**Lo nuevo de esta ronda**: por primera vez hay un hallazgo de corrección
*matemática* en la feature insignia (R3-H1, PAV con empates) — las rondas 1-2
habían re-derivado el PAV como correcto sobre scores continuos, pero el caso
degenerado (scores empatados, que es el caso *típico* de los índices de
peligro reales: todas las celdas-paso secas comparten `susc·factor(0)`) no
se había probado.

### HIGH — nuevos

**R3-H1. PAV isotónico incorrecto con scores empatados: el fit depende del
ORDEN de las muestras.** `crates/nowcast-core/src/calibrate.rs:62-93`. El PAV
procesa las muestras ordenadas con sort estable pero nunca agrupa los scores
empatados en un bloque antes de poolear: valores de x iguales pueden recibir
valores ajustados distintos (la regresión isotónica es función de x — scikit-
learn hace el pooling secundario; aquí falta). Reproducido:
`fit_isotonic([0.5,0.5],[false,true])` → `p(0.5)=0.0` pero con outcomes en
orden inverso → `p(0.5)=0.5` — mismo multiconjunto, resultado distinto según
orden de llegada. Caso interior: scores `[0.1,.5,.5,.5,.5,.9]`, outcomes
`[F,F,F,T,T,T]` → `p(0.5)=1.0` cuando el valor pooled correcto es 0.5 (error
factor 2). El calibrador resultante tiene `xs` duplicados con `ys` distintos
y **pasa `validate()`**. No es caso patológico: los índices de peligro reales
están masivamente empatados, así que golpea al verbo `calibrate` del CLI y a
`Calibrator.fit` en Python. Los tests no lo pillan porque usan scores
continuos aleatorios. Fix: pre-agrupar por score único (promediar outcomes
por x empatado) antes del loop PAV + test de invarianza al orden.

**R3-H2. `Calibrator::probability(NaN)` / `calibrate([NaN])` panican —
alcanzable desde Python hoy.** `calibrate.rs:107` +
`nowcast-python/src/lib.rs:288-295`. Con score NaN los guards de borde son
falsos y `binary_search_by(|v| v.partial_cmp(&score).unwrap())` hace unwrap
de `None`. El binding reenvía el f64 crudo → `PanicException` no capturable
(reproducido desde Python con el wheel real). Tercera reincidencia textual
del patrón sibling: la ronda 2 blindó `fit_isotonic` contra NaN y dejó el
lado de *aplicación* del mismo struct. El CLI está a salvo (`calibrate_field`
solo pasa hazard del motor, finito). Fix: rechazar no-finitos en
`probability`/`calibrate` (o `total_cmp`), mapear a `ValueError` en el
binding — mismo patrón que `explain`.

**R3-H3. `Inundation::run_point_sources` panica con un `PointSource` fuera de
la malla (hydroflux).** `crates/nowcast-hydroflux/src/lib.rs:245`.
`PointSource { row, col }` es público y re-exportado; ni el adapter ni el
solver validan bounds — `apply_point_sources` indexa directo
(`hydroflux/solver-2d/src/source.rs:62`). El caso de uso real (colocar la
celda de entrada del caudal desde una grilla gruesa desalineada) con un
off-by-one produce panic de librería — la clase exacta que H1/H5/N-H1
eliminaron en el resto del stack. Reproducido: `PointSource{row:99,col:0}`
sobre malla 4×4 → panic ndarray OOB. Fix: validar `row < nr && col < nc` (y
`q_mass` finito) en `run_point_sources` devolviendo `Result`, patrón
`Error::OutOfRange` — frontera del adapter, sin tocar el repo hermano.

### MEDIUM — nuevos

#### Core

**R3-M1. `ensemble_hazard` no valida que los miembros compartan `dt_hours`.**
`ensemble.rs:73-117`. Valida grilla y `n_steps` pero no el eje temporal.
Reproducido: un miembro horario (20 mm en 1 h → gatilla) y uno diario (20 mm
en 24 h → no gatilla) con el mismo `n_steps` se agregan sin error →
`p_exceed = 0.5` sobre un "paso" que significa 1 h para uno y 24 h para el
otro. Rompe la simetría con `run_live`, que sí rechaza el mismo desacuerdo
(`live.rs:220`). Fix: exigir `dt_hours` uniforme, mismo patrón de error que
`run_live`. (Nota: `MultiNowcast` tiene el mismo hueco pero no es validable
hoy — el trait `Trigger` no expone dt; cerrar requeriría `dt_hours()` en el
trait.)

**R3-M2. `alert_level` NaN desactiva TODAS las alertas en silencio — y es
alcanzable desde la CLI real.** `nowcast.rs:115-123` + CLI + Python. `p >=
NaN` es siempre falso; clap parsea `"NaN"` como f64 válido. Reproducido
contra el binario: `nowcast run ... --alert-level NaN` imprime "0 with an
alert at level NaN" y sale con **exit 0** — un typo (o variable de entorno
vacía interpolada en un script operacional) apaga el sistema de alertas
entero, indistinguible de la calma real, rompiendo el contrato de exit-code 2.
Igual desde Python (`alerts(nan)` → `[]`) y en `ensemble_hazard`
(`p_exceed = 0`). Fix: validar `level` finito y en [0,1] en
`HazardField::alert()`/`alerts()`/`ensemble_hazard` — protege CLI y Python de
una vez.

**R3-M3. `MultiNowcast::trigger_factors` y `AntecedentTrigger::api` panican
con cell/step fuera de rango.** `multi.rs:365-367` y `multi.rs:239-242`. La
ronda 2 blindó `MultiNowcast::hazard_at`; `trigger_factors` — método público
del MISMO struct, y precisamente la API de trazabilidad que se llama con
celdas arbitrarias — sigue indexando sin chequeo (reproducido: panic OOB).
Fix: `Result` validando contra `dims`/`n_steps`, como `hazard_at`.

#### CLI / Python

**R3-M4. `cmd_calibrate`: desalineación silenciosa de pares score/outcome con
fallas de parseo cruzadas.** `nowcast-cli/src/main.rs:726-740`. Las dos
columnas se parsean con llamadas independientes a `csv_column`, que salta
líneas no parseables *por columna*: si la fila A tiene score válido/outcome
inválido y la B lo contrario, los conteos coinciden, el guard pasa, y el
calibrador se ajusta sobre pares tomados de **filas distintas** — sin
advertencia, en el verbo que produce el artefacto que `run`/`watch
--calibrator` usan. Reproducido: CSV con fallas cruzadas reporta "2 pairs" y
produce el mismo calibrador que el archivo limpio con pares recombinados.
Es el único hallazgo de la ronda que puede producir resultado científico
silenciosamente incorrecto por vía CLI. Fix: parsear fila a fila exigiendo
ambas columnas en la misma línea. Mismo riesgo latente en `backtest`
(`csv_month_keys` vs `csv_column`).

**R3-M5. `watch` ignora `--ncols/--nrows` contradictorios con `--susc`,
mientras `run` los rechaza.** `main.rs:529-542`. `cmd_watch` re-implementa la
resolución de susceptibilidad a mano y descarta las dims explícitas en
silencio cuando hay raster; `resolve_inputs` (run/explain) las valida.
Reproducido: mismos argumentos contradictorios → `run` exit 1, `watch` corre.
Fix: compartir `resolve_inputs` con `cmd_watch`.

**R3-M6. Python: dtype float32/int64 rechazado con mensaje críptico que no
menciona float64.** `nowcast-python/src/lib.rs:79-83,108-112`.
`PyReadonlyArray1<f64>` exige dtype exacto y el TypeError de pyo3/numpy no
dice qué se espera ni sugiere `.astype()`. Golpea al pipeline declarado: los
rasters de susceptibilidad RF reales (physics-guided-ml) son típicamente
float32 — el input más natural del usuario objetivo falla con el peor mensaje
del módulo. Fix: `PyArrayLike1<f64, AllowTypeChange>` (convierte con copia) o
re-emitir el error con instrucción explícita.

#### Adapters

**R3-M7. `q_mass = NaN` se lava a "seco" con `unstable = false` — el fix
N-M6 es correcto pero un caso lo rodea río arriba.** hydroflux, vía solver
`source.rs:63`: `cell.h = (cell.h + dh).max(0.0)` y `(h + NaN).max(0.0) ==
0.0` en Rust, así que el NaN se laundera *en cada paso*, antes de que el
detector post-integración de la ronda 2 pueda verlo. Reproducido: fuente NaN
60 s → `max_depth = 0, unstable = false`: inundación "completamente seca"
presentada como corrida sana. (`run_rain` NO sufre esto: `apply_rain` usa
`if new_h <= 0.0`, que deja sobrevivir el NaN hasta el flag.) Fix: rechazar
`q_mass` no finito en `run_point_sources` (frontera del adapter) — se
resuelve junto con R3-H3.

**R3-M8. `susceptibility_from_raster` deja pasar ±inf al clamp: un píxel
corrupto se vuelve susceptibilidad 1.0 permanente y silenciosa.**
`nowcast-surtgis/src/lib.rs:80-87`. El guard es solo `is_nan() || nodata`; el
fix N-M2 se aplicó a `gridded_rain_from_rasters` pero no a su hermana en el
mismo archivo. Reproducido: `+inf` → 1.0, `−inf` → 0.0. Fix: `!v.is_finite()`
como nodata, idéntico al de gridded_rain.

**R3-M9. `RainflowForcing::gr4j` acepta `dt_days` arbitrario, pero GR4J es
estrictamente diario.** `nowcast-rainflow/src/lib.rs:87-99`. `Gr4j::run` no
tiene parámetro de paso y sus unidades internas (x2 mm/día, x4 en días) son
diarias; con `dt_days = 1/24` el motor igual trata cada paso como un día y el
adapter re-etiqueta (`depth_mm = q·dt_days`, `dt_hours = 24·dt_days`) →
hidrograma e intensidades silenciosamente mal escalados. Fix: exigir
`dt_days == 1.0` en `gr4j()`; `RainflowForcing::new` genérico puede seguir
aceptando cualquier dt para series externas.

**R3-M10. `Runout::from_footprint` es un bypass público del guard de
`pixel_size` que la ronda 1 puso en `run_runout`.** `nowcast-swarm/src/lib.rs:49`.
Reproducido: `pixel_size = NaN` → `affected_km2 = NaN`; `pixel_size = −30` →
área positiva con apariencia sana (el signo se cancela al cuadrado). Fix:
`from_footprint → Result` con el mismo guard; `run_runout` delega.

**R3-M11. Reproducibilidad: el raster RandomForest del Maipo desapareció de
la máquina.** `geotiff_roundtrip.rs:22` apunta a
`~/proyectos/postdoc/papers/paper1_susceptibilidad/.../susceptibility_RandomForest.tif`,
directorio que ya no existe (`find $HOME` sin resultado). El example degrada
con gracia a lo sintético, pero la cifra citada en CLAUDE.md ("5149×5855,
[0.001,0.965]") ya no es reproducible, y `scripts/extract_maipo_cr2met.py` /
`extract_maipo_distributed.py` apuntan al mismo path muerto (inventario +
raster) — la cadena de datos del backtest del paper. **Importa antes del DOI
Zenodo.** Fix: restaurar/re-hospedar el dataset o actualizar paths y
documentar su origen.

### LOW — nuevos

- **R3-L1.** `brier_score`/`reliability` aceptan "probabilidades" finitas
  fuera de [0,1] con semántica inconsistente: `reliability` binnea con el
  valor clampeado pero calcula Brier con el crudo (`calibrate.rs:212-319`).
- **R3-L2.** `Combine::apply(&[])` devuelve 1.0 para `Product` (identidad del
  producto = "peligro máximo con cero triggers") y 0.0 para `Max`/`NoisyOr`
  (`multi.rs:305-311`). Inalcanzable vía `MultiNowcast`; documentar o
  uniformar a 0.0.
- **R3-L3.** `pod_at_area` corta bloques de scores empatados en el borde del
  área por orden de input — determinista pero arbitrario, y con los empates
  masivos del índice puede mover el número (`backtest.rs:563-586`).
- **R3-L4.** Python: contigüidad inconsistente — arrays 1-D con stride se
  rechazan (`to_vec()`), el rain 2-D acepta cualquier layout
  (`lib.rs:90` vs `lib.rs:129`).
- **R3-L5.** `csv_events` hace `skip(1)` incondicional: un inventario sin
  header pierde su primer evento en silencio y cambia POD/FAR
  (`backtest.rs:98-111`); los otros dos parsers CSV toleran el header
  intentando parsear.
- **R3-L6.** `--sweep 0:16:0.5` aborta el barrido completo en la primera
  iteración (a=0 inválido) en vez de saltar/reportar; sin cota de
  iteraciones (`--sweep 0.5:1e8:0.001` cuelga) (`main.rs:651-682`).
- **R3-L7.** `Calibrator` de Python no puede cargar ni persistir el JSON del
  CLI (sin `to_json`/`from_json`/`validate`): el pipeline Python — donde se
  ajustaría sobre backtests reales (limitación i del paper) — no puede
  producir el artefacto que `watch --calibrator` consume.
- **R3-L8.** `Boundaries2D`/`Side` no están en el re-export de
  nowcast-hydroflux pese a que `Inundation::new` los exige — hasta los
  propios examples importan `hydroflux_solver_2d` directo (`lib.rs:27`).
- **R3-L9.** `gridded_rain_from_rasters` descarta el `Georef` del stack (el
  caller no puede verificar alineación lluvia↔susceptibilidad sin releer
  archivos) y el check de CRS es vacuo si el raster 0 no declara CRS
  (`nowcast-surtgis/src/lib.rs:122-155`).
- **R3-L10.** `SnowmeltForcing::runoff_at(step)` panica OOB sin doc
  `# Panics` — quedó fuera del contrato que M14 documentó para
  `Forcing::depth_mm` (`nowcast-snowmelt/src/lib.rs:111`).
- **R3-L11.** `run_fire` no valida `horizon_min`: con NaN o negativo el
  Dijkstra no propaga y el fuego es silenciosamente nulo (ni la ignición "se
  quema") → cascada post-fuego no-op (`nowcast-firespread/src/lib.rs:111-123`).

### Qué se confirmó sólido (re-derivado sobre el código actual)

- Paridad **bit-idéntica** en las tres superficies: `run` ↔ `watch` (16
  decimales sobre GeoTIFF real), CLI ↔ Python, `Nowcast.run()` ↔
  `LiveNowcast.push()` desde Python. Defaults Caine coherentes
  (14.82/0.39/4.0/7) en las tres.
- Kernel I-D sin off-by-one (batch, live, IdTrigger, IdMapTrigger); `push`
  valida antes de mutar (atomicidad correcta).
- ROC-AUC (Mann-Whitney con rangos promedio) y PR-AUC (bloques de empates):
  matemática correcta. Wilson, ECE, Brier skill correctos.
- Conversiones de unidades de los 7 adapters re-derivadas contra el código
  hermano (86.4 mm/día·km² = 1 m³/s; m/yr→mm/yr ×1000; runoff snowmelt en mm
  por paso; `depth = q·dt` rainflow). Row-major coherente en los 7, sin
  y-flip.
- Los 6 fixes de la ronda 2 sostienen (re-ejecutados, no solo releídos).
- Exit codes del CLI correctos en todos los casos probados; CSV hostiles
  (CRLF, BOM, vacío, semicolon, columnas fuera de rango) → error limpio;
  JSON válido y determinista en los 5 verbos; `allow_threads` presente en
  los cómputos largos de Python.
- Examples de los 7 adapters compilan y sus cifras de CLAUDE.md se
  reprodujeron (+46% rain-on-snow, 24/264 celdas, 0→317 post-fuego,
  123.2 km²) — salvo la parte real de `geotiff_roundtrip` (R3-M11).
- LOWs abiertos de rondas anteriores confirmados sin cambios (FAR con celdas
  fuera de grilla, `chebyshev_window` sin cota, `GridDims::new(0,·)`, dt
  comparado con `!=` exacto, NaN como empate en AUC, N-M7 pin de
  hydroflux-solver-2d).

### Plan de acción — ronda 3

| # | Qué | Resuelve | Esfuerzo |
|---|-----|----------|----------|
| 1 | ✅ (2026-07-10) PAV con pooling secundario de scores empatados (los empates colapsan a un bloque inicial antes del loop PAV; el fit vuelve a ser función de x) + tests: invarianza al orden en el borde y en bloque interior (el caso factor-2 del hallazgo), multiset barajado fitea idéntico | R3-H1 | S |
| 2 | ✅ parcial (2026-07-10) `Calibrator::probability`/`calibrate` → `Result` (rechazan no-finito; interpolación interna `probability_finite` con `total_cmp`; binding mapea a `ValueError`; CLI con `expect` justificado — hazard finito por construcción). `alert_level` validado finito ∈ [0,1] en las fronteras: run/watch/backtest del CLI y `alerts`/`push` de Python. **Pendiente**: la variante core-side (`HazardField::alert`/`ensemble_hazard`) — `ensemble_hazard` no está expuesto por CLI/Python hoy | R3-H2, R3-M2 | S |
| 3 | `run_point_sources`: validar bounds del `PointSource` y `q_mass` finito → `Result` | R3-H3, R3-M7 | S |
| 4 | Cerrar las hermanas restantes: `trigger_factors`/`api` → `Result`; `susceptibility_from_raster` no-finito = nodata; `from_footprint` → `Result` | R3-M3, R3-M8, R3-M10 | S |
| 5 | `ensemble_hazard` exige `dt_hours` uniforme; `gr4j()` exige `dt_days == 1.0` | R3-M1, R3-M9 | S |
| 6 | ✅ (2026-07-10) `csv_pairs` en el core (parser fila-a-fila: header tolerado, falla cruzada = error duro con número de línea) consumido por `cmd_calibrate`; `cmd_watch` resuelve inputs vía `resolve_inputs` (misma validación, mismo default 1×1, mismo parser que `run`) | R3-M4, R3-M5 | S-M |
| 7 | ✅ (2026-07-10) Binding migrado a `PyArrayLike1/2<f64, AllowTypeChange>` en los 7 puntos de entrada (acepta float32/int/bool, listas Python, vistas con stride y transpuestas; verificado bit-idéntico al path float64 — cierra también R3-L4) + `to_json`/`from_json` en `PyCalibrator` (con `validate()` post-deserialización; round-trip verificado en ambos sentidos con `run --calibrator`) | R3-M6, R3-L7, R3-L4 | S |
| 8 | **Proceso**: al aplicar cada fix de frontera, barrer el struct/módulo completo buscando hermanas con el mismo contrato (el patrón reincide 4 veces en esta ronda) — checklist en el PR/commit | (preventivo) | — |
| 9 | Resolver R3-M11 (dataset Maipo desaparecido) **antes del DOI Zenodo**: restaurar o re-hospedar + actualizar paths de scripts | R3-M11 | M — decisión del usuario |
| 10 | LOWs R3-L1..L11: batch oportunista al tocar cada archivo. ✅ (2026-07-10) L4 (con ítem 7), L5 (`csv_events` sin `skip(1)`: header se salta porque no parsea + test headerless), L6 (`parse_sweep` exige finitos — cierra también el LOW de ronda 1 —, MIN > 0 y cota de 100 000 corridas), L7 (con ítem 7). Abiertos: L1, L2, L3, L8, L9, L10, L11 | LOWs | S c/u |

**Ejecución 2026-07-10 (superficies CLI + Python, ítems 1, 2*, 6, 7, L4-L7)**:
verificado con `cargo test --workspace --exclude nowcast-python` (61 tests en
core, 4 nuevos) y `cargo clippy --workspace --all-targets -- -D warnings` (con
y sin `nowcast-python`) 100% verde; wheel reconstruido con maturin y batería
Python re-ejecutada (NaN → `ValueError`, float32/int64/listas/strided aceptados
bit-idénticos, `alerts(nan)`/`push(alert_level=nan)` → `ValueError`, JSON
round-trip Python↔CLI en ambos sentidos); **todos los repros de la auditoría
re-corridos contra el binario nuevo** (calibrate cruzado → error con línea
exacta; `watch --ncols 99` rechaza; `--alert-level NaN` exit 1 en los tres
verbos; inventario headerless conserva sus 2 eventos; sweep con 0/nan/10¹¹
corridas falla rápido en vez de colgar). Regresión de equivalencia: `run` y
`watch` sobre el GeoTIFF real producen JSON **bit-idéntico al pre-fix**, y la
paridad triple run↔watch↔Python sigue exacta.
