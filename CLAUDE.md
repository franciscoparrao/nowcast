# nowcast — Motor de nowcasting de geohazards (dinámico) en Rust

> **Estado:** v0.1 EN CURSO. Núcleo `nowcast-core` funcional (susceptibilidad×trigger
> + umbrales I-D + alertas), `std`+`thiserror`, build/test offline. Creado 2026-06-10
> desde revisión de estado del arte; núcleo iniciado 2026-06-16.
> Familia de motores Rust del autor: SurtGIS, Hydroflux, Smelt, Anvil, Cantus, Criterium.
> Doc madre: `~/proyectos/ideas-motores-rust.md` (idea N3, Parte 5).

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
      resoluble 0.66 mm/h (=total/24h) → E máx 0.62 → **NO gatilla**. IMERG ve
      108.5 mm, peak 40 mm/h → **ALERTA 24-mar 04:30, E≈12**. Demuestra
      cuantitativamente que la resolución diaria *estructuralmente* no puede
      gatillar I-D (la intensidad máxima resoluble es total/24h), y que subir la
      resolución de la forzante supera el límite sin tocar el motor. Es la
      respuesta a "superar el límite de CR2MET".
- [x] **Generalización multi-evento** (`examples/multi_event_leadtime.rs` +
      `scripts/extract_event_imerg.py` con registro de eventos): IMERG semihorario
      sobre 3 aluviones fechados de climas opuestos — Atacama/Copiapó 25-mar-2015
      (árido, peak 40 mm/h), Cajón del Maipo 25-feb-2017 (convectivo verano, 5 mm/h)
      y Villa Santa Lucía 16-dic-2017 (frontal sur, 17 mm/h). El cruce I-D cae sobre
      o justo antes del día documentado en los tres; y en el Cajón del Maipo (ráfaga
      convectiva breve) el producto **diario NO gatilla** — solo lo sub-diario lo
      detecta. Cierra el caso de la forzante de alta resolución.
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
- [ ] (v0.2) Acople con Hydroflux (modelo físico zonas críticas).

## Arquitectura tentativa
- `nowcast-core`: motor de reglas/umbrales + combinación susceptibilidad×trigger.
- Targets: native (Rayon) + Python (PyO3) + CLI; posible servicio en loop.
- Orquesta rásters de SurtGIS + salidas de rainflow/snowmelt + modelo Smelt.

## Validación
Backtesting contra inventario de eventos fechados (SERNAGEOMIN) — hit rate,
falsas alarmas, lead time.

## Venue objetivo
**NHESS**, **Landslides** o **Natural Hazards**.

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
