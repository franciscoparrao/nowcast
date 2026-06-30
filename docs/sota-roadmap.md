# nowcast — SOTA-informed improvement roadmap

*Grounded in an OpenAlex scan (2021–2026) of rainfall-triggered geohazard
nowcasting. Compiled 2026-06-29. DOIs are verifiable.*

## Where nowcast sits vs the state of the art

The SOTA is mostly **point models** (one deep-learning or one physically-based
model) or **product-specific operational chains** (e.g. NASA LHASA). nowcast is
neither: it is a forcing-agnostic *substrate* with a sharp methods result (forcing
resolution, not the model, sets the intensity–duration skill ceiling). Almost
every SOTA advance below is exactly the kind of component that would **plug into
nowcast's `Forcing`/`Trigger` interfaces without touching the hazard logic** — so
the roadmap is to *enchufar* the SOTA, not to rewrite the engine.

---

## 1 · Forecast (not just observed) forcing, with ensembles — top lever

- **SOTA:** DGMR — *Skilful precipitation nowcasting using deep generative models
  of radar*, Ravuri et al., **Nature 2021** (`10.1038/s41586-021-03854-z`).
  *Deep learning forecast of rainfall-induced shallow landslides*, **Nat. Commun.
  2023** (`10.1038/s41467-023-38135-y`). Hybrid physics-AI nowcasting, **npj Clim.
  Atmos. Sci. 2024** (`10.1038/s41612-024-00834-8`).
- **nowcast today:** observed / hindcast forcing only.
- **The move:** a `Forcing` that wraps a 0–6 h sub-hourly *ensemble* rainfall
  nowcast (DGMR / pySTEPS). Turns "threshold crossing detected" into "alert with a
  genuine forecast lead time"; the ensemble feeds the calibration module → a
  predictive probability.
- **Fit / effort:** direct (one `Forcing` impl + an external model via PyO3/ONNX).
  Standalone paper. **This is the natural continuation of the resolution thesis.**
- **Status:** the *engine side* is prototyped — `nowcast_core::ensemble_hazard`
  runs the engine over an ensemble of forcing members and returns a probabilistic
  hazard (exceedance probability, mean, spread); the exceedance probability feeds
  the calibration tools (example `ensemble_nowcast`). What remains is wiring a real
  ensemble QPF model (pySTEPS / DGMR) as a `Forcing` and validating on a case study.

## 2 · Explicit antecedent state (soil moisture)

- **SOTA:** *Implementation of hydrometeorological thresholds for regional
  landslide warning in Catalonia*, **Landslides 2023**
  (`10.1007/s10346-023-02094-8`). *Using PCA to incorporate multi-layer soil
  moisture information…*, **NHESS 2023** (`10.5194/nhess-23-279-2023`).
  Conceptual basis: Bogaard & Greco, hydro-meteorological thresholds.
- **nowcast today:** antecedence only implicit, via the rolling I–D window
  (limitation iv).
- **The move:** a state `Trigger` that assimilates SMAP/ASCAT soil moisture or a
  continuous water balance (the rainflow/snowmelt providers already carry state) →
  a rainfall × wetness threshold. Raises the ceiling exactly where the daily Maipo
  null fails.
- **Fit / effort:** medium; reuses the multi-trigger combination machinery.

## 3 · Physical and data-driven triggers behind the same interface

- **SOTA:** *MAT.TRIGRS v1.0* (open-source), **Nat. Hazards Res. 2021**
  (`10.1016/j.nhres.2021.11.001`). *Spatio-temporal landslide forecasting using
  process-based **and** data-driven approaches*, **Catena 2023**
  (`10.1016/j.catena.2023.106948`) — validates the interchangeable-trigger thesis.
  *FSLAM* regional susceptibility plugin, **Environ. Model. Softw. 2022**
  (`10.1016/j.envsoft.2022.105354`). *iHydroSlide3D v1.0*, **GMD 2023**
  (`10.5194/gmd-16-2915-2023`).
- **nowcast today:** empirical I–D only — the crudest part of the pipeline.
- **The move:** (a) a **physical** trigger (transient infiltration + infinite-slope
  factor of safety, TRIGRS-style) run on demand where the cheap nowcast alerts,
  exactly analogous to the hydroflux flood coupling; (b) a **learned** trigger
  (ONNX/PyO3). Exact attribution covers the closed-form part; SHAP applies upstream
  to the ML component.
- **Fit / effort:** (a) high value, medium effort (on-demand coupling pattern
  already exists); (b) low-medium. Standalone paper potential.

## 4 · Modern verification (spatial + ensemble)

- **SOTA:** *The fractions skill score for ensemble forecast verification*,
  **QJRMS 2024** (`10.1002/qj.4824`).
- **nowcast today:** point ROC-AUC + Brier / Wilson reliability.
- **The move:** add **FSS** (neighbourhood) and **CRPS** to the backtest module.
- **Fit / effort:** cheap; brings verification to current standard and hardens the
  existing paper against reviewers.

## 5 · Operational benchmark + high-resolution inventory (the #1 limitation)

- **SOTA / baseline:** LHASA — *Data-Driven Landslide Nowcasting at the Global
  Scale*, Stanley et al., **Front. Earth Sci. 2021** (`10.3389/feart.2021.640043`).
  *Landslide initiation thresholds in data-sparse regions*, **NHESS 2023**
  (`10.5194/nhess-23-3261-2023`) — directly the Chilean data-sparse case.
  *Bayesian forecasting of triggered landslides*, **2026**
  (`10.5194/egusphere-2026-1624`).
- **The move:** a day/hour-resolution dated inventory (NASA COOLR/GLC or a curated
  Chilean one) + run nowcast **vs LHASA** on the same events. Converts the
  resolution diagnosis into a validated operational claim (limitations i, ii).
- **Fit / effort:** data problem, not software; highest validation value.

## 6 · (Frontier) Impact, not only hazard

- **SOTA:** *Advancing operational flood forecasting, early warning and risk
  management with new emerging…*, **J. Flood Risk Manag. 2023**
  (`10.1111/jfr3.12884`).
- **The move:** overlay exposure (population, OSM roads) via the surtgis bridge →
  impact-based alerts (hazard → expected impact).
- **Fit / effort:** larger scope; longer-horizon.

---

## Synthesis & priority

The SOTA would **not** ask to rewrite nowcast. It would ask to plug in **(1)
forecast + ensemble forcing, (2) antecedent state and (3) a physical/ML trigger**,
and to validate with **(4) modern verification and (5) a benchmark vs LHASA on a
fine inventory**. (1)–(3) are each a paper of their own; (4)–(5) harden the current
manuscript.

**If forced to pick one: axis 1 (forecast forcing).** It is the natural
continuation of the resolution thesis, the largest operational jump, and it reuses
the probability-calibration machinery already in the engine.

*Method note: OpenAlex `title_and_abstract.search` per axis, filtered to articles
from 2021, sorted by citations; canonical methods (DGMR, LHASA, TRIGRS, FSS) also
searched by name. Re-run to refresh.*
