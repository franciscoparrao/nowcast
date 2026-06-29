# nowcast: a forcing-agnostic Rust engine for dynamic geohazard nowcasting, and why forcing resolution, not the model, sets the skill ceiling

**Francisco Parra-O.**¹

¹ Universidad de Santiago de Chile (USACH), Santiago, Chile. Correspondence: francisco.parra.o@usach.cl

*Target journal: Computers & Geosciences (Elsevier), Research/Software paper.*
*Draft. References are a starting set and must be verified before submission (see Code and data availability).*

---

## Abstract

Landslide and flood susceptibility maps are static: fixed predisposing factors combined by physical or machine-learning models. Turning susceptibility into an operational *nowcast* requires modulating it in time with the dynamic forcing that triggers failure, rainfall, snowmelt, routed discharge, ground deformation. We present **nowcast**, a dependency-light Rust engine that expresses time-varying hazard as `hazard(cell, t) = susceptibility(cell) × trigger(exceedance, t)` behind a single interchangeable forcing interface, so the hazard logic is decoupled from the data source and from the trigger family. The engine ships native providers that wrap sibling Rust models for rainfall–runoff, snowmelt, 2-D shallow-water inundation, an agent-based debris-flow model, a geospatial raster substrate, and PS-InSAR/SBAS deformation; triggers are composable (rainfall intensity–duration, discharge, deformation rate) and physical refinement runs on demand only where the cheap nowcast alerts. Using the engine as an experimental instrument over Chilean basins, we hold the model fixed and vary the resolution of the forcing. A backtest of 157 dated rainfall-triggered landslides in the Río Maipo basin shows that the global Caine (1980) intensity–duration threshold never fires on daily CR2MET forcing (probability of detection, POD = 0), while a calibrated regional intercept transfers split-sample (validation POD ≈ 0.50). Distributing that daily forcing over the basin and weighting by a real machine-learning susceptibility raster does **not** improve discrimination. The area under the ROC curve is indistinguishable from random (AUC ≈ 0.48; month-block bootstrap 95 % CI spans 0.5): at 5 km daily resolution the gridded rainfall at event cell-months is no higher than average, and a supervised baseline trained on the same daily features fares no better (cross-validated AUC ≤ 0.56). A controlled experiment with known ground truth illustrates the mechanism: the same engine discriminates planted sub-daily bursts almost perfectly (AUC ≈ 1), but aggregating the identical field to daily resolution collapses the operational catch rate to effectively zero (over 20 random realisations), with the model and terrain held fixed. The bottleneck is therefore the *resolution* of the forcing rather than the model: at half-hourly GPM IMERG resolution the same intensity–duration trigger pins the threshold crossing to a timestamp hours ahead of the documented flows, whereas the same rain aggregated to daily structurally cannot trigger (the finest resolvable mean intensity is total/24 h, smeared below the curve). We reproduce this across three dated events spanning opposite Chilean climates. A complementary flood path, validated against observed daily streamflow on the Río Itata, discriminates observed floods out of sample (ROC-AUC 0.90), because discharge routing integrates the rainfall that a sub-daily landslide trigger must instead resolve directly. The engine, its backtesting framework, and all examples are open and reproducible.

**Keywords:** nowcasting; landslides; floods; intensity–duration threshold; rainfall resolution; Rust; early warning; Chile.

---

## 1. Introduction

Operational forecasting of rainfall-triggered geohazards sits between two well-developed but disjoint bodies of work. On one side, *susceptibility* mapping combines static predisposing factors, slope, lithology, land cover, terrain indices, through statistical, physically-based or, increasingly, machine-learning models, to estimate *where* failures are possible (Reichenbach et al., 2018). On the other, empirical *rainfall thresholds*, most famously the intensity–duration (I–D) power law of Caine (1980) and its many regional successors (Guzzetti et al., 2007; Segoni et al., 2018), estimate *when* triggering rainfall has occurred. Operational early-warning, however, needs *where* and *when* together, as a hazard field that evolves through an event (Bogaard and Greco, 2018). This coupling is already realised operationally: NASA's LHASA gates a static landslide susceptibility map with GPM IMERG satellite-rainfall thresholds at the global scale (Kirschbaum and Stanley, 2018), its successor adds a data-driven model (Stanley et al., 2021), and geographical landslide early-warning systems are now widespread (Guzzetti et al., 2020). What these share is a *product-specific* design: the forcing, the trigger and the susceptibility model are welded into one operational chain, so swapping the rainfall product, adding a second trigger (snowmelt, discharge, deformation), or attaching a physical-refinement model means rebuilding the pipeline. The open question is less *whether* to couple susceptibility and forcing than *how much resolution the forcing must carry* for the coupling to have skill — a question a fixed operational chain cannot easily ask.

Closing that gap raises three software problems that recur across hazards and regions. First, the *forcing* is heterogeneous: a rain gauge, a gridded reanalysis product, a satellite quantitative precipitation estimate, a rainfall–runoff model's hydrograph, or an InSAR deformation field, each with its own grid, units and time step. Second, the *trigger* family is hazard-dependent: landslides respond to rainfall I–D, floods to discharge over a threshold, slow slopes to deformation rate, yet all share the structure "exceedance of a critical level mapped to a hazard factor". Third, validation against dated event inventories is itself a methods problem with non-obvious metric choices for rare, spatially sparse, incomplete records.

We present **nowcast**, a Rust engine that addresses these three problems with one abstraction, an interchangeable *forcing* interface and a composable *trigger*, and that we then use as an experimental instrument to ask a question rarely posed directly: holding the model fixed, how much does the *resolution of the forcing* determine attainable skill? The contributions are:

1. **A forcing-agnostic, dependency-light engine** (Section 2). The core depends only on the Rust standard library and an error-handling crate, builds and tests offline, and exposes hazard as `susceptibility × trigger`. Native providers wrap sibling engines for rainfall–runoff, snowmelt, 2-D shallow-water inundation, agent-based debris-flow runout, a geospatial raster substrate, and PS-InSAR deformation, through the same interface.
2. **A composable multi-trigger** (Section 2.4): rainfall I–D and any duration-independent value/threshold signal (discharge, deformation rate) combine through noisy-OR, maximum or product, with exact, closed-form attribution of every alert.
3. **A backtesting framework with metrics appropriate for sparse dated inventories** (Section 4): monthly and spatial event-centred contingency, ROC-AUC and probability-of-detection-at-area, regional threshold calibration and split-sample validation.
4. **A resolution diagnosis** (Sections 5–6). On real Chilean data, distributing daily forcing and adding real susceptibility does not improve discrimination, an honest null that, together with a sub-daily head-to-head, locates the binding constraint on the *forcing resolution* rather than the model, and quantifies the lead time that sub-daily forcing unlocks. A flood path validated against observed streamflow shows the converse: where routing integrates the forcing, daily resolution suffices (out-of-sample ROC-AUC 0.90).

All code, examples and data-extraction scripts are open (Section *Code and data availability*).

## 2. The nowcast engine

### 2.1 Hazard formulation

The engine represents hazard on a row-major raster grid. For every cell and time step,

    hazard(cell, t) = susceptibility(cell) × trigger_factor(exceedance, t),                     (1)

where `susceptibility ∈ [0, 1]` is a static background field (a terrain index, a physically-based factor of safety proxy, or a machine-learning susceptibility raster) and `trigger_factor ∈ [0, 1]` is a dimensionless modulation derived from the dynamic forcing. Both factors lie in `[0, 1]`, so the product is a bounded hazard index. We deliberately call it an *index*, not a calibrated probability (Section 7).

The trigger factor is a smooth function of an *exceedance ratio* `E`, how far the forcing has crossed a critical level. A hard threshold (`E ≥ 1` fires, else nothing) discards how far past the curve the forcing reached and yields all-or-nothing maps; instead we use a logistic,

    trigger_factor(E) = 1 / (1 + exp(−k (E − 1))),                                               (2)

with `trigger_factor(1) = 0.5` exactly on the curve and steepness `k` (default 4). The *source* of `E` is what varies between hazards.

### 2.2 The forcing interface

A single trait abstracts the dynamic forcing as a per-cell, per-step scalar field with a known time step:

```rust
pub trait Forcing {
    fn dims(&self) -> GridDims;
    fn n_steps(&self) -> usize;
    fn dt_hours(&self) -> f64;
    fn depth_mm(&self, cell: usize, step: usize) -> f64;
}
```

Random access (rather than a pull/iterator model) is deliberate: the I–D logic accumulates rolling windows, and backtesting replays whole dated series. Shipped implementations are `UniformRain` (a single observed gauge broadcast over a grid, the decoupled entry point), `GriddedRain` (an independent series per cell, e.g. a gridded precipitation product) and, through the provider crates, snowmelt runoff and InSAR deformation. The same trait therefore admits a gauge, a reanalysis grid, a satellite product or a model output without touching the hazard logic.

### 2.3 The rainfall intensity–duration trigger

For rainfall-triggered landslides the exceedance is computed against a power-law I–D curve,

    I_crit(D) = a · D^(−b)            (I in mm h⁻¹, D in hours),                                 (3)

with the Caine (1980) global intercept `a = 14.82`, `b = 0.39` as a default, replaceable by a regional curve. For each cell and step, the engine accumulates water input over rolling windows of length `m = 1 … m_max` ending at that step, forms the mean intensity `I = depth/(m·dt)` and duration `D = m·dt`, evaluates `E_m = I / I_crit(D)`, and takes the worst (maximum) exceedance over all windows; per-cell prefix sums make each window sum O(1), so a run is O(cells · steps · m_max). The resulting exceedance feeds Eq. (2).

### 2.4 Composable triggers

Beyond a single rainfall trigger, the engine generalises the trigger to *composable exceedance sources*. A `Trigger` yields a factor per cell and step; an `IdTrigger` implements the rolling-window I–D logic above; a `ThresholdTrigger` implements a duration-independent exceedance `E = value / value_crit` suitable for any signal already expressed as a rate (routed discharge `Q/Q_c`, InSAR velocity `|v|/v_crit`). A `MultiNowcast` fuses several triggers through a combination rule before modulating susceptibility:

    hazard = susceptibility × combine(f₁, …, f_k),    combine ∈ {max, noisy-OR, product}.       (4)

Noisy-OR (`1 − ∏(1 − fᵢ)`) is the natural rule for independent triggers where any one may fire, for example *rain I–D* OR *deformation rate*. The per-trigger factors are exposed for traceability.

### 2.5 Physical refinement on demand

The susceptibility × trigger product is a fast, coarse screen. Where it alerts, the engine couples, one-way and on demand, to a process model that refines the alert into a physical field, only over the alerted area. Two couplings are implemented. For **floods**, a 2-D shallow-water solver (HLLC fluxes, Audusse well-balanced reconstruction, semi-implicit Manning friction) routes the alerting discharge over the local digital elevation model and returns an inundation depth per cell; the coarse probability is downscaled onto the physical footprint (probability where depth ≥ a threshold, else zero). For **landslides/debris flows**, an agent-based model (rain and flow agents over the terrain raster, calibrated on the 2015 Atacama event) returns the runout footprint, downscaled the same way; that calibration feeds only this footprint demonstration and is independent of the I–D lead-time analysis (Section 5.4), which uses the rainfall trigger alone. The two couplings are symmetric: the expensive, physical model runs only where the cheap nowcast already alerted. The saving is quantifiable: because the shallow-water solver integrates to a fixed physical time with CFL-limited steps, its cost is ~O(cells), so confining it to an alerted tile spanning 31 % of a coarse domain runs 3.9× faster than solving the full domain (1.19 → 0.31 s) while returning **bit-identical** depths in the inundated region (the flood is contained within the tile). The on-demand restriction is therefore free in accuracy.

### 2.6 Exact attribution

Because Eq. (1) is closed-form, every alert is *exactly* attributable, there is no surrogate model to approximate, as post-hoc methods such as SHAP (Lundberg and Lee, 2017) require for black boxes. For any cell and step the engine reports the two factors, the rolling I–D window that drove the trigger (its duration, mean intensity, critical intensity and exceedance), which side, terrain or forcing, is the binding constraint, and a counterfactual: the rainfall intensity, at a given duration, that would lift the cell to the alert level (or `None` where susceptibility alone caps it below the level). SHAP remains relevant one layer upstream, explaining the machine-learning *susceptibility* that enters Eq. (1) as an already-interpretable input.

### 2.7 Implementation

The engine is written in Rust (edition 2024) as a Cargo workspace of ten crates. The core (`nowcast-core`) depends only on `std` and `thiserror`, so it builds and tests offline with no system dependencies; native providers are separate crates that wrap sibling engines through path dependencies: rainfall–runoff (GR4J/HBV; Perrin et al., 2003; Seibert and Vis, 2012), distributed snowmelt, 2-D shallow water (Audusse et al., 2004; Toro et al., 1994), the agent-based debris-flow model, the geospatial raster substrate (native GeoTIFF I/O, no GDAL dependency), PS-InSAR/SBAS deformation, and a wildfire-spread model with a post-fire susceptibility cascade; a command-line runner exposes the engine (run, backtest, explain) and PyO3 bindings expose it to Python. The build is reproducible, the codebase is covered by ~58 unit and documentation tests, and the linter runs clean under `-D warnings`. Twenty-four runnable examples reproduce every result below.

An optional Rayon backend (`--features parallel`) parallelises the independent per-step loop; the closure captures only the read-only prefix buffer, so the output is bit-identical to the serial run. The speed-up is modest and bandwidth-bound: on a 16-core machine over a 50 M cell-step grid it reaches 1.8× on 2 threads (88 % efficiency) but saturates at 2.8× on 16 threads, because each step does little arithmetic per byte read and the shared prefix buffer makes the loop memory-bandwidth-bound. The O(cells × steps) prefix buffer (~1.4 GB at the largest tested size) is the practical memory limit, mitigated by chunking the time axis; that same memory pressure, not a lack of parallel work, caps the multi-thread speed-up.

## 3. Data and study area

All experiments use openly documented Chilean datasets (Table 1). Susceptibility is a RandomForest probability raster at 30 m resolution (values 0.001–0.965 over the Río Maipo basin, 5149 × 5855 cells), taken from a companion susceptibility study: the model is trained on landslide presence points from the SERNAGEOMIN catalogue (augmented with the Patagonian-Andes and 2010 Maule co-seismic inventories) against spatially-interspersed absence points and terrain, geology and climate predictors, under spatial cross-validation. This raster is therefore *not independent* of the validation inventory: it was trained on a superset of the same dated events the backtest scores against. The dependence is, however, *conservative* for our purpose — leakage can only *inflate* the susceptibility's apparent agreement with event locations, so an inventory-informed susceptibility that still fails to discriminate at 5 km daily resolution (Section 5.2) strengthens the resolution-ceiling conclusion rather than weakening it; we flag the single place it could matter (the weak single-feature susceptibility signal) where it arises. Daily precipitation is CR2MET v2.5 (0.05°, ~5 km; Boisier et al., 2018). Sub-daily precipitation is GPM IMERG Final v07 half-hourly (~0.1°; Huffman et al., 2020). Catchment forcing for the flood path uses CAMELS-CL (Alvarez-Garreton et al., 2018). The dated event inventory is the SERNAGEOMIN landslide/flow catalogue; events are dated to the year and month from the record identifier (the year column is unreliable), and the month itself is only approximate, the 25 March 2015 Quebrada de Macul-type flows of the Atacama event, for instance, are filed under March though the extreme rainfall fell over 24–26 March.

**Table 1.** Datasets.

| Dataset | Variable | Resolution | Use |
|---|---|---|---|
| RandomForest susceptibility | landslide susceptibility | 30 m | static background |
| CR2MET v2.5 | daily precipitation | 0.05°, daily | lumped & distributed forcing |
| GPM IMERG Final v07 | precipitation rate | ~0.1°, 30 min | sub-daily forcing |
| CAMELS-CL | precip, PET, streamflow | catchment, daily | flood path (GR4J/HBV) |
| SERNAGEOMIN inventory | dated events | point, ~month | validation target |

Study sites are the Río Maipo basin (central Andes, landslide backtest), the Río Itata catchment (humid south-central, flood path), and three dated debris-flow events spanning opposite climates: Atacama/Copiapó (25 March 2015, arid, convective), Cajón del Maipo (25 February 2017, central Andes, summer convective) and Villa Santa Lucía (16 December 2017, humid south, frontal). They span roughly 27°S to 43°S, from the hyper-arid Atacama to the humid Patagonian Andes (Fig. 1; `docs/paper/figs/fig1_studyarea.pdf`).

## 4. Methods

### 4.1 Backtesting metrics

Validation of a nowcast is binary forecast verification: on each unit the model either alerts or not, and an event either occurred or not, giving a 2×2 contingency table and the standard categorical scores, probability of detection (POD), false-alarm ratio (FAR), critical success index (CSI), frequency bias. Because the inventory is dated only to ~month, the matching unit is the calendar month, and matching is **event-centred** with a ±tolerance window (an event is a hit if an alert falls within the window; a false alarm is an alerted month far from any event) to avoid inflating misses when one event spans a tolerance window.

For **spatial** verification on a grid, per-cell month matching against a spatially sparse, *incomplete* inventory makes CSI/FAR meaningless, almost every "false alarm" is a susceptible, wet cell with no *recorded* event. We therefore score discrimination, as the susceptibility and early-warning literatures do (Reichenbach et al., 2018): ROC-AUC (does the hazard rank event cell-months above quiet ones?) and POD at a fixed alerted-area fraction (catch rate for a given alert budget). Uncertainty on the AUC is estimated by a **month-block** bootstrap (resampling calendar months with replacement, keeping each month's cells together) so within-month spatial autocorrelation is not mistaken for independent information. As a model-agnostic check we also train supervised baselines (logistic regression and gradient boosting) on per-cell-month susceptibility and rainfall features, evaluated by year-blocked `GroupKFold` cross-validation.

We distinguish two notions used throughout the results. *Triggering* asks whether the forcing can cross the I–D curve at all; it is a property of the intensity at the finest resolvable duration, and a daily product can fail it structurally (Section 5.4). *Discrimination* (or skill) asks whether the resulting hazard *ranks* event locations above non-event ones (ROC-AUC, POD). A forcing can trigger frequently yet discriminate poorly; the two are not interchangeable.

### 4.2 Threshold calibration and split-sample validation

The I–D intercept `a` is calibrated by sweeping it (fixed `b = 0.39`) and selecting the value that maximises CSI against the dated events; the trigger steepness `k` and the maximum rolling window are fixed (`k = 4`, `m_max = 7` days for the daily landslide path, I–D durations of storms, not seasonal accumulation). Validation is split-sample: the intercept is calibrated on odd calendar years and evaluated on even years. The steepness `k` enters only through the trigger factor (Eq. 2), which is monotonic in the exceedance, so the rank-based discrimination scores (ROC-AUC, POD at a fixed area) are *exactly* invariant to `k` for the trigger-only configuration; `k` affects the threshold-based POD/FAR only through the chosen alert level.

## 5. Results

### 5.1 Lumped landslide backtest (Río Maipo)

Over 13 880 daily steps (1979–2016) and 157 dated rainfall-triggered events, the global Caine threshold (`a = 14.82`) **never fires** on CR2MET daily forcing (POD = 0): the daily-mean intensity of even the wettest days sits below the curve at 24 h duration. A calibrated regional intercept `a* ≈ 5.5 mm h⁻¹` is robust, the full-period and odd-year calibrations agree, and transfers split-sample (validation POD ≈ 0.50). The false-alarm ratio is high (FAR ≈ 0.92) but structural: with an event base rate near 4 % and a single representative gauge in a Mediterranean wet-season climate, an I–D trigger alone over-predicts. The inventory's month-level dating caps attainable POD: widening the match tolerance from ±0 to ±3 months raises POD from 0.21 to 0.68, so a large share of "misses" is date error in the inventory, not model error (Table 2).

**Table 2.** Lumped Maipo backtest (monthly contingency, ±1-month match unless noted).

| Configuration | POD | FAR | note |
|---|---|---|---|
| Caine global, a=14.82 | 0.00 | 1.00 | never fires |
| Regional a*=5.5 (full period) | 0.53 | 0.92 | max-CSI calibration |
| Regional a*=5.5 (split-sample validation) | 0.50 | 0.93 | calibrate odd → validate even |
| a*=5.5, tolerance ±0 / ±1 / ±2 / ±3 mo | 0.21 / 0.53 / 0.58 / 0.68 |, | inventory date noise |

**Published thresholds do not rescue daily forcing.** Self-calibration invites the objection that a published threshold, taken as is, might do better. We tested this directly by applying, unchanged, the 35 empirical I–D power laws (`I = a·D⁻ᵇ`, mm h⁻¹) compiled by Guzzetti et al. (2007) (their Table 2, the canonical collation of the literature) to the same CR2MET daily forcing. The outcome is dictated by resolution, not by the curve: 10 of the 35 sit *structurally* above the daily ceiling — on a basin whose wettest day in 38 years is 136 mm, no daily forcing can reach their 24 h intensity, since the maximum resolvable 24 h intensity is the daily total divided by 24 — and 5 never fire at all. Thirteen yield POD = 0, and of the curves that do fire, 30 have FAR ≥ 0.90. The best-scoring published curve attains CSI = 0.084, indistinguishable from the self-calibrated regional intercept (CSI = 0.077): the entire ensemble is pinned near the same low operating point. A curve high enough to be selective sits above the wettest day (POD 0); lowered enough to fire, it fires on most wet months (FAR → 1). The binding constraint is the forcing resolution, not the choice of published threshold (`published_thresholds` example).

### 5.2 Distributed daily backtest, an honest null

Repeating the backtest with **distributed** CR2MET rainfall over a 15 × 18 grid (270 cells, 123 120 cell-months, 884 in the event footprint), the **real** RandomForest susceptibility resampled to that grid, and spatial verification, yields essentially **no discrimination**: ROC-AUC is 0.463 for a lumped basin-mean baseline weighted by susceptibility, 0.477 for distributed rainfall with uniform susceptibility, and 0.485 for distributed rainfall weighted by real susceptibility. A month-block bootstrap (resampling months with replacement, so within-month spatial autocorrelation is preserved; B = 2000) puts all three 95 % confidence intervals across the random 0.5 ([0.40, 0.57] for the best configuration), and the bootstrap probability that AUC exceeds 0.5 is only 0.21–0.34. None of the configurations is distinguishable from random. The mean 7-day rainfall at recorded event cell-months (45 mm) is in fact *lower* than the grid-wide average (49 mm). At 5 km daily resolution the gridded rainfall does not discriminate where and when these landslides were recorded, and 30 m susceptibility averaged to 5 km loses its edge. Distributing the forcing does not help: the limiting factor is the *resolution* of the forcing and susceptibility (and the month-level inventory), not the lumping.

A supervised baseline confirms this is a property of the data, not of our trigger. Logistic regression and gradient boosting trained on per-cell-month features (susceptibility; monthly total; maximum 1-day and 7-day rainfall; previous-month total), evaluated with year-blocked `GroupKFold`, reach a cross-validated AUC of only 0.56 and 0.52. A single-feature analysis locates the little signal that exists: it comes from the static susceptibility (AUC 0.55, itself an optimistic upper bound since this raster was trained on the same inventory, Section 3) and weak antecedent wetness (0.52), whereas every rainfall-intensity feature is at or below random (max 7-day 0.49, max 1-day 0.46, monthly total 0.46). The daily rainfall therefore carries essentially no information about where and when these landslides occurred, and a flexible model cannot recover a signal the daily product does not contain.

### 5.3 Controlled resolution experiment

The distributed null is ambiguous on its own: a near-random AUC could mean the engine has no skill, or that the month-dated, incomplete inventory cannot reveal skill that is present. We resolve the ambiguity with a controlled experiment whose ground truth is known by construction. On a 20 × 20 grid over 60 days we synthesise one half-hourly rainfall field in which the *only* feature that separates an event cell-day from a non-event cell-day is the sub-daily intensity profile, not the daily total: event cell-days deliver a modest total (16–26 mm) as a ~1 h convective burst that crosses the I–D curve, while *confounder* cell-days deliver a similar or larger total (16–46 mm) smeared over 16–23 h, so their intensity stays below the curve. The same active cells host both kinds of day, so the static susceptibility carries no information that distinguishes them. We then aggregate the identical field to coarser resolution (0.5 h to 24 h), holding the I–D engine and the 24 h maximum duration fixed, and score discrimination of the planted events at each resolution, averaged over 20 random realisations.

At half-hourly resolution the engine recovers the planted events almost perfectly (ROC-AUC 1.00 ± 0.00 on the trigger alone; 0.88 ± 0.02 once multiplied by the structured susceptibility) and the operational catch rate is high (POD at a 5 % alerted-area budget 0.37 ± 0.10). As the field is aggregated the maximum resolvable mean intensity falls toward total/24 h, the bursts smear below the curve, the trigger rate drops from 35 % to 18 % of cell-days, the trigger-only AUC decays monotonically (to 0.79 ± 0.01; multiplied by the structured susceptibility it ends at 0.81 ± 0.02 after a shallow, noise-driven plateau), and the operational catch rate **collapses to POD@5 % = 0.002 ± 0.005, i.e. effectively zero,** at daily resolution: with a realistic alert budget a daily product catches essentially *none* of the events a sub-daily product catches. The residual daily AUC above 0.5 is the optimistic case for daily forcing (the synthetic events still carry an above-dry daily total); in the real Maipo data even that vanishes (Section 5.2). The experiment isolates the *aggregation mechanism* under known ground truth — the engine discriminates when the triggering signal is resolved and loses that skill under aggregation, with model and terrain fixed; it is a demonstration of the mechanism, not a claim about real-event base rates, which the field backtests (Sections 5.1–5.2) address.

### 5.4 Resolution head-to-head

The previous null motivates a controlled comparison of forcing resolution on the same storm core (Atacama, 24–26 March 2015; the IMERG event-total maximum at lon −70.45, lat −27.15), with the identical I–D engine (Table 3). The core is selected *conditional on the known event*, so this is a triggering-timing comparison (can each product cross the curve, and when), not a discrimination test; discrimination is the business of the distributed backtest (Section 5.2) and the controlled experiment (Section 5.3). With a daily value the finest resolvable duration is 24 h, so the maximum resolvable mean intensity is (daily total)/24 h: CR2MET sees 30.1 mm spread to 0.66 mm h⁻¹, an exceedance of at most 0.62 (regional `a = 4.0`) or 0.17 (Caine), the daily product **structurally cannot trigger**, regardless of total. The same storm core in half-hourly IMERG peaks at 40 mm h⁻¹; the I–D threshold is crossed at 04:30 UTC on 24 March (`E ≈ 12`), hours before the documented flows. Higher-resolution forcing overcomes the limit with no change to the engine.

**Table 3.** CR2MET daily vs GPM IMERG half-hourly at the Atacama storm core (same I–D engine).

| | CR2MET daily (~5 km) | IMERG ½-hourly (~10 km) |
|---|---|---|
| event total | 30.1 mm | 108.5 mm |
| finest resolvable duration | 24 h | 0.5 h |
| max resolvable intensity | 0.66 mm h⁻¹ | 40.0 mm h⁻¹ |
| I–D (a=4.0) | no alert (E ≤ 0.62) | alert 24-Mar 04:30 UTC (E ≈ 12) |

### 5.5 Multi-event sub-daily lead time

The sub-daily result generalises across three dated debris-flow events in opposite climates (Table 4). In all three the half-hourly I–D crossing lands on or just before the documented day. For the Cajón del Maipo summer convective burst, the same rainfall aggregated to daily **never triggers**, only sub-daily resolution detects it. We emphasise that the inventory provides no event onset *hour*, so these are not formal lead-time skill scores but a demonstration that sub-daily forcing resolves the threshold-crossing time that daily forcing cannot.

As a spatial false-alarm check, we ran the identical per-cell I–D trigger over each event's full IMERG scene. The documented storm core is in every case the most extreme cell (96.7–100th percentile of peak exceedance), but the trigger crosses the curve over a broad footprint — 41 % of the Atacama scene, 16 % at Cajón del Maipo, and the entire (small, frontal) Villa Santa Lucía box — so the crossing is not spatially sparse within these storm-centred scenes. This locates the *peak* correctly but is *not* a low false-alarm rate, and because the box is event-centred by construction a high crossing fraction is expected. A genuine discrimination estimate needs the off-event crossing frequency at these cores — a multi-year sub-daily record on days *without* a documented flow, which the present event-window granules do not provide. The sub-daily results are therefore triggering-timing demonstrations, not skill scores; assembling that multi-year sub-daily record is, with a day-resolution inventory (Section 7), the key remaining validation step.

**Table 4.** Sub-daily I–D crossing across three events (GPM IMERG half-hourly, storm core, `a = 4.0`).

| Event | Climate | Total | Peak 1 h | I–D crossing (UTC) | Documented day | Daily fires? |
|---|---|---|---|---|---|---|
| Atacama / Copiapó | arid, convective | 108 mm | 40 mm h⁻¹ | 24-Mar 04:30 | 25-Mar-2015 | yes |
| Cajón del Maipo | central Andes, summer | 28 mm | 5 mm h⁻¹ | 25-Feb 16:30 | 25-Feb-2017 | **no** |
| Villa Santa Lucía | humid south, frontal | 83 mm | 17 mm h⁻¹ | 15-Dec 15:00 | 16-Dec-2017 | yes |

### 5.6 Flood path: a validated discharge nowcast

To exercise the discharge trigger we run GR4J (Perrin et al., 2003) on the CAMELS-CL Río Itata catchment (gauge 8123001, 1979–2016) and validate it against *observed* daily streamflow. Unlike the month-dated landslide inventory, observed streamflow is daily, so this is a genuine day-resolution verification. GR4J is calibrated by random search on the training period (years before 2003, maximising Kling–Gupta efficiency) and evaluated out of sample on 2003–2016; it reproduces the observed hydrograph well (KGE 0.85 train, 0.74 test; NSE 0.70 / 0.60). Defining an observed flood as a day whose observed discharge exceeds its 98th training percentile (21.7 mm day⁻¹), the engine's discharge-exceedance hazard, driven by the *simulated* hydrograph, discriminates observed flood days out of sample with **ROC-AUC 0.896**; at a fixed alert level (simulated Q above the 98th training percentile, 23.2 mm day⁻¹) it scores POD 0.30, FAR 0.42 and CSI 0.25 against a 1.2 % flood base rate. The exact-attribution facility decomposes each alert into its exposure and discharge factors.

This is the validated positive that the daily *landslide* path lacks, and the contrast is the resolution argument seen from the other side. Discharge routing *integrates* rainfall over the catchment, supplying the temporal accumulation that a sub-daily I–D trigger must otherwise resolve in the rainfall itself; a daily forcing that structurally cannot trigger a convective-burst landslide (Section 5.4) is therefore adequate for a routed-discharge flood. Attainable skill tracks the forcing resolution *relative to the process*: daily suffices where routing integrates, and fails where triggering is sub-daily.

### 5.7 Architecture extensibility

Beyond the two validated paths above, the engine ships further adapters that exercise the *interface* rather than make a hazard claim; we are explicit that none is a validated prediction. A snowmelt provider supplies rain-plus-melt runoff per cell (wrapping a distributed snowmelt model); an InSAR provider turns a line-of-sight velocity field into a duration-independent deformation trigger that fuses with rainfall by noisy-OR, so a slope already creeping (`|v| > v_crit`) can cross the alert level under rain that alone would not trigger; an agent-based debris-flow model, run on demand from an alert, returns a runout footprint onto which the coarse probability is concentrated — the landslide analogue of the flood coupling of Section 2.5; and a wildfire-spread model couples the other way, as a one-way cascade in which a simulated burn scar amplifies the static susceptibility, so a subsequent rainfall nowcast sees the elevated post-fire debris-flow hazard the scar is known to carry. Each is a runnable example. Their purpose here is to show that a new forcing, a new trigger family and a new physical-refinement model each plug in through the same `Forcing` and `Trigger` interfaces *without touching the hazard logic*; their predictive skill rests on the wrapped models and their own validation, and is not claimed here. The validated results of this paper are the resolution diagnosis (Section 5.3), the flood path (Section 5.6) and the on-demand coupling's compute saving (Section 2.5).

## 6. Discussion

The headline is a methods result obtained *because* the engine holds the model fixed while the forcing varies. Three points follow.

**The resolution ceiling.** Distributing daily rainfall and adding a real susceptibility raster does not lift discrimination above chance (Section 5.2), yet the identical trigger on sub-daily forcing resolves the threshold crossing to within hours (Sections 5.4–5.5). The binding constraint is therefore the *resolution of the forcing*, not the susceptibility model or the threshold logic. This is intuitive once stated, with a daily value the maximum resolvable intensity is total/24 h, and short, intense, convective bursts are smeared below any I–D curve, but it is rarely isolated, because most studies vary the model on one product rather than the product under one model. The controlled experiment (Section 5.3) makes the mechanism explicit on a target with known ground truth: with the model and terrain held fixed, discrimination of planted events is near-perfect at sub-hourly resolution and decays to an effectively zero operational catch rate at daily resolution, so the real-data null reflects the resolution of the forcing, not an absence of recoverable signal in principle. The practical corollary is that an I–D nowcast is only as good as its rainfall's temporal resolution; effort spent on susceptibility sophistication is wasted where the forcing is daily. The flood path makes the same point with a *positive* result: where discharge routing integrates the rainfall, daily forcing is adequate and the engine discriminates observed floods out of sample (ROC-AUC 0.90, Section 5.6). Skill thus tracks forcing resolution *relative to the triggering process*, not resolution in the abstract — a daily product useless for convective-burst landslides is sufficient for routed floods.

**The inventory ceiling.** A second, independent ceiling is the validation target. The SERNAGEOMIN inventory is dated to ~month and is incomplete (notable events only). Month-level dating alone accounts for a large fraction of apparent misses (Table 2), and incompleteness makes per-cell FAR uninformative (Section 4.1). Credible discrimination and true lead-time skill require a day- or hour-resolution inventory, or event-segmented rainfall, a data problem, not a software one, and the single largest unlock for future validation.

**The architecture as enabler.** That both diagnoses were even possible rests on the forcing-agnostic design: swapping CR2MET daily for IMERG half-hourly, or adding a deformation trigger, requires no change to the hazard logic. The same interface lets the engine act as the downstream integrator of a family of process models, and the one-way on-demand coupling keeps the expensive physics confined to alerted areas. This is a different design point from existing tools rather than a replacement. Operational nowcasts such as LHASA (Kirschbaum and Stanley, 2018; Stanley et al., 2021) couple a static susceptibility map with a specific rainfall product (IMERG) and trigger, as a global monolithic chain; threshold calculators such as CTRL-T (Melillo et al., 2018) automate regional I–D estimation from event catalogues. nowcast is complementary: it makes the forcing, the trigger and the physical-refinement model *independently swappable* behind one interface — which is precisely what let us hold the model fixed and vary the forcing resolution. It is an experimental and integrating substrate, not a calibrated operational product, and an operational chain like LHASA is the kind of system its outputs would feed or be benchmarked against.

## 7. Limitations and future work

We are explicit about what the engine does *not* yet do. (i) The raw hazard is a bounded *index*; the engine ships isotonic calibration that maps it to a probability with reliability diagnostics (Brier score and skill, Wilson intervals, expected calibration error). Fitting it on the real distributed Maipo backtest (odd years → even) collapses the calibrated probability to climatology (Brier skill −3.4 for the raw index → −0.003 calibrated; ECE 0.080 → 0.003): an honest confirmation that the daily index carries no skill to calibrate, consistent with the Section 5.2 null. Validating the calibration where skill is present (the flood path, sub-daily forcing) remains future work. (ii) Trigger parameters are calibrated minimally (a single I–D intercept by CSI sweep); a formal calibration with uncertainty, for which the sibling rainfall–runoff engine already provides a DDS optimiser pattern, is future work. We benchmark discrimination against supervised baselines (Section 5.2) and against the full ensemble of 35 published I–D thresholds (Section 5.1), but a formal calibration with uncertainty against an *operational* early-warning system remains to be done. (iii) The engine includes a streaming nowcast that ingests forcing step by step and emits alerts online (bit-identical to the batch engine), but validation here is hindcast: a live data feed and operational evaluation are not yet wired in. (iv) Antecedent soil moisture is captured only implicitly through long rolling windows, not as an explicit state. (v) The multi-trigger combination weights (noisy-OR) and the deformation critical rate are not yet calibrated. (vi) The snowmelt, InSAR-deformation and debris-flow-runout adapters (Section 5.7) exercise the architecture but are not validated predictions; only the on-demand coupling's compute saving is quantified (Section 2.5). Addressing (i), (ii) and a day-resolution inventory would convert the present diagnosis into a validated operational claim.

## 8. Conclusions

`nowcast` is an open, dependency-light Rust engine that turns static susceptibility into a time-varying hazard through a single interchangeable forcing interface and a composable trigger, with native providers for rainfall–runoff, snowmelt, shallow-water inundation, agent-based debris-flow runout, a geospatial substrate and InSAR deformation, and exact closed-form attribution of every alert. Used as an experimental instrument over Chilean basins, it yields an honest and, we argue, generalisable result: the resolution of the forcing, not the model, sets the skill ceiling of intensity–duration nowcasting. A global I–D threshold never fires on daily forcing; a calibrated regional threshold transfers but, distributed at daily resolution, does not discriminate event locations (AUC ≈ 0.48); the identical trigger on half-hourly satellite forcing pins the threshold crossing to a timestamp hours ahead of documented flows across three climatically distinct events. The engine, its backtesting framework and all examples are reproducible, and the architecture is designed so that higher-resolution forcing, the demonstrated path to skill, can be plugged in without touching the hazard logic.

## Code and data availability

The `nowcast` source (ten Rust crates, including a command-line runner and PyO3 Python bindings, plus twenty-four examples) and all data-extraction scripts are openly available; a citable archive with a DOI will be deposited on Zenodo at submission. Input datasets are third-party and openly documented: CR2MET v2.5, GPM IMERG Final v07 (NASA GES DISC), CAMELS-CL (Alvarez-Garreton et al., 2018, PANGAEA https://doi.org/10.1594/PANGAEA.894885) and the SERNAGEOMIN inventory. Derived per-event series are regenerable from the provided scripts. *Note: the reference list below is a working set and must be verified (e.g. exact volume, pages and DOIs) before submission.*

## Author contributions

F.P.-O. designed and implemented the engine, performed the experiments and wrote the manuscript.

## Competing interests

The author declares no competing interests.

## References (to verify)

- Alvarez-Garreton, C., et al. (2018). The CAMELS-CL dataset: catchment attributes and meteorology for large sample studies, Chile dataset. *Hydrology and Earth System Sciences*, 22, 5817–5846.
- Audusse, E., Bouchut, F., Bristeau, M.-O., Klein, R., Perthame, B. (2004). A fast and stable well-balanced scheme with hydrostatic reconstruction for shallow water flows. *SIAM Journal on Scientific Computing*, 25(6), 2050–2065.
- Bogaard, T., Greco, R. (2018). Invited perspectives: Hydrological perspectives on precipitation intensity–duration thresholds for landslide initiation. *Natural Hazards and Earth System Sciences*, 18, 31–39.
- Boisier, J.P., et al. (2018). Anthropogenic and natural contributions to the Southwest South America precipitation decline and the CR2MET dataset. (CR2MET precipitation product.)
- Caine, N. (1980). The rainfall intensity–duration control of shallow landslides and debris flows. *Geografiska Annaler A*, 62, 23–27.
- Guzzetti, F., Peruccacci, S., Rossi, M., Stark, C.P. (2007). Rainfall thresholds for the initiation of landslides in central and southern Europe. *Meteorology and Atmospheric Physics*, 98, 239–267.
- Guzzetti, F., Gariano, S.L., Peruccacci, S., Brunetti, M.T., Marchesini, I., Rossi, M., Melillo, M. (2020). Geographical landslide early warning systems. *Earth-Science Reviews*, 200, 102973.
- Huffman, G.J., et al. (2020). GPM IMERG Final Precipitation L3 Half Hourly, V07. NASA GES DISC.
- Kirschbaum, D., Stanley, T. (2018). Satellite-based assessment of rainfall-triggered landslide hazard for situational awareness (LHASA). *Earth's Future*, 6(3), 505–523.
- Lundberg, S.M., Lee, S.-I. (2017). A unified approach to interpreting model predictions. *Advances in Neural Information Processing Systems (NeurIPS)*, 30.
- Melillo, M., Brunetti, M.T., Peruccacci, S., Gariano, S.L., Roccati, A., Guzzetti, F. (2018). A tool for the automatic calculation of rainfall thresholds for landslide occurrence. *Environmental Modelling & Software*, 105, 230–243.
- Perrin, C., Michel, C., Andréassian, V. (2003). Improvement of a parsimonious model for streamflow simulation (GR4J). *Journal of Hydrology*, 279, 275–289.
- Reichenbach, P., Rossi, M., Malamud, B.D., Mihir, M., Guzzetti, F. (2018). A review of statistically-based landslide susceptibility models. *Earth-Science Reviews*, 180, 60–91.
- Segoni, S., Piciullo, L., Gariano, S.L. (2018). A review of the recent literature on rainfall thresholds for landslide occurrence. *Landslides*, 15, 1483–1501.
- Stanley, T.A., Kirschbaum, D.B., Benz, G., et al. (2021). Data-driven landslide nowcasting at the global scale (LHASA v2). *Frontiers in Earth Science*, 9, 640043.
- Seibert, J., Vis, M.J.P. (2012). Teaching hydrological modeling with a user-friendly catchment-runoff-model software package (HBV-light). *Hydrology and Earth System Sciences*, 16, 3315–3325.
- Toro, E.F., Spruce, M., Speares, W. (1994). Restoration of the contact surface in the HLL Riemann solver (HLLC). *Shock Waves*, 4, 25–34.
