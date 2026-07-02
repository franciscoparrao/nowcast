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
