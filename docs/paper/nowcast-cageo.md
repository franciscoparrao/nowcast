# nowcast: a forcing-agnostic Rust engine for dynamic geohazard nowcasting, and why forcing resolution, not the model, sets the skill ceiling

**Francisco Parra-O.**¹

¹ Universidad de Santiago de Chile (USACH), Santiago, Chile. Correspondence: francisco.parra.o@usach.cl

*Target journal: Computers & Geosciences (Elsevier), Research/Software paper.*
*Draft. References are a starting set and must be verified before submission (see Code and data availability).*

---

## Abstract

Landslide and flood susceptibility maps are static: fixed predisposing factors combined by physical or machine-learning models. Turning susceptibility into an operational *nowcast* requires modulating it in time with the dynamic forcing that triggers failure, rainfall, snowmelt, routed discharge, ground deformation. We present **nowcast**, a dependency-light Rust engine that expresses time-varying hazard as `hazard(cell, t) = susceptibility(cell) × trigger(exceedance, t)` behind a single interchangeable forcing interface, so the hazard logic is decoupled from the data source and from the trigger family. The engine ships native providers that wrap sibling Rust models for rainfall–runoff, snowmelt, 2-D shallow-water inundation, an agent-based debris-flow model, a geospatial raster substrate, and PS-InSAR/SBAS deformation; triggers are composable (rainfall intensity–duration, discharge, deformation rate) and physical refinement runs on demand only where the cheap nowcast alerts. Using the engine as an experimental instrument over Chilean basins, we hold the model fixed and vary the resolution of the forcing. A backtest of 157 dated rainfall-triggered landslides in the Río Maipo basin shows that the global Caine (1980) intensity–duration threshold never fires on daily CR2MET forcing (probability of detection, POD = 0), while a calibrated regional intercept transfers split-sample (validation POD ≈ 0.50). Distributing that daily forcing over the basin and weighting by a real machine-learning susceptibility raster does **not** improve discrimination: the area under the ROC curve stays near random (AUC ≈ 0.48) because, at 5 km daily resolution, the gridded rainfall at event cell-months is no higher than average. The bottleneck is the *resolution* of the forcing, not the model: at half-hourly GPM IMERG resolution the same intensity–duration trigger pins the threshold crossing to a timestamp hours ahead of the documented flows, whereas the same rain aggregated to daily structurally cannot trigger (the finest resolvable mean intensity is total/24 h, smeared below the curve). We reproduce this across three dated events spanning opposite Chilean climates. The engine, its backtesting framework, and all examples are open and reproducible.

**Keywords:** nowcasting; landslides; floods; intensity–duration threshold; rainfall resolution; Rust; early warning; Chile.

---

## 1. Introduction

Operational forecasting of rainfall-triggered geohazards sits between two well-developed but disjoint bodies of work. On one side, *susceptibility* mapping combines static predisposing factors, slope, lithology, land cover, terrain indices, through statistical, physically-based or, increasingly, machine-learning models, to estimate *where* failures are possible (Reichenbach et al., 2018). On the other, empirical *rainfall thresholds*, most famously the intensity–duration (I–D) power law of Caine (1980) and its many regional successors (Guzzetti et al., 2007; Segoni et al., 2018), estimate *when* triggering rainfall has occurred. Operational early-warning, however, needs *where* and *when* together, as a hazard field that evolves through an event. The gap the geohazards community has flagged is precisely this dynamic step: coupling static susceptibility to the time-varying forcing that triggers failure (Bogaard and Greco, 2018).

Closing that gap raises three software problems that recur across hazards and regions. First, the *forcing* is heterogeneous: a rain gauge, a gridded reanalysis product, a satellite quantitative precipitation estimate, a rainfall–runoff model's hydrograph, or an InSAR deformation field, each with its own grid, units and time step. Second, the *trigger* family is hazard-dependent: landslides respond to rainfall I–D, floods to discharge over a threshold, slow slopes to deformation rate, yet all share the structure "exceedance of a critical level mapped to a hazard factor". Third, validation against dated event inventories is itself a methods problem with non-obvious metric choices for rare, spatially sparse, incomplete records.

We present **nowcast**, a Rust engine that addresses these three problems with one abstraction, an interchangeable *forcing* interface and a composable *trigger*, and that we then use as an experimental instrument to ask a question rarely posed directly: holding the model fixed, how much does the *resolution of the forcing* determine attainable skill? The contributions are:

1. **A forcing-agnostic, dependency-light engine** (Section 2). The core depends only on the Rust standard library and an error-handling crate, builds and tests offline, and exposes hazard as `susceptibility × trigger`. Native providers wrap sibling engines for rainfall–runoff, snowmelt, 2-D shallow-water inundation, agent-based debris-flow runout, a geospatial raster substrate, and PS-InSAR deformation, through the same interface.
2. **A composable multi-trigger** (Section 2.4): rainfall I–D and any duration-independent value/threshold signal (discharge, deformation rate) combine through noisy-OR, maximum or product, with exact, closed-form attribution of every alert.
3. **A backtesting framework with metrics appropriate for sparse dated inventories** (Section 4): monthly and spatial event-centred contingency, ROC-AUC and probability-of-detection-at-area, regional threshold calibration and split-sample validation.
4. **A resolution diagnosis** (Sections 5–6). On real Chilean data, distributing daily forcing and adding real susceptibility does not improve discrimination, an honest null that, together with a sub-daily head-to-head, locates the binding constraint on the *forcing resolution* rather than the model, and quantifies the lead time that sub-daily forcing unlocks.

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

The susceptibility × trigger product is a fast, coarse screen. Where it alerts, the engine couples, one-way and on demand, to a process model that refines the alert into a physical field, only over the alerted area. Two couplings are implemented. For **floods**, a 2-D shallow-water solver (HLLC fluxes, Audusse well-balanced reconstruction, semi-implicit Manning friction) routes the alerting discharge over the local digital elevation model and returns an inundation depth per cell; the coarse probability is downscaled onto the physical footprint (probability where depth ≥ a threshold, else zero). For **landslides/debris flows**, an agent-based model (rain and flow agents over the terrain raster, calibrated on the 2015 Atacama event) returns the runout footprint, downscaled the same way. The two couplings are symmetric: the expensive, physical model runs only where the cheap nowcast already alerted.

### 2.6 Exact attribution

Because Eq. (1) is closed-form, every alert is *exactly* attributable, there is no surrogate model to approximate, as post-hoc methods such as SHAP (Lundberg and Lee, 2017) require for black boxes. For any cell and step the engine reports the two factors, the rolling I–D window that drove the trigger (its duration, mean intensity, critical intensity and exceedance), which side, terrain or forcing, is the binding constraint, and a counterfactual: the rainfall intensity, at a given duration, that would lift the cell to the alert level (or `None` where susceptibility alone caps it below the level). SHAP remains relevant one layer upstream, explaining the machine-learning *susceptibility* that enters Eq. (1) as an already-interpretable input.

### 2.7 Implementation

The engine is written in Rust (edition 2024) as a Cargo workspace of eight crates. The core (`nowcast-core`) depends only on `std` and `thiserror`, so it builds and tests offline with no system dependencies; native providers are separate crates that wrap sibling engines through path dependencies: rainfall–runoff (GR4J/HBV; Perrin et al., 2003; Seibert and Vis, 2012), distributed snowmelt, 2-D shallow water (Audusse et al., 2004; Toro et al., 1994), the agent-based debris-flow model, the geospatial raster substrate (native GeoTIFF I/O, no GDAL dependency), and PS-InSAR/SBAS deformation. The build is reproducible, the codebase is covered by ~43 unit and documentation tests, and the linter runs clean under `-D warnings`. Fourteen runnable examples reproduce every result below.

## 3. Data and study area

All experiments use openly documented Chilean datasets (Table 1). Susceptibility is a RandomForest probability raster at 30 m resolution (values 0.001–0.965 over the Río Maipo basin, 5149 × 5855 cells). Daily precipitation is CR2MET v2.5 (0.05°, ~5 km; Boisier et al., 2018). Sub-daily precipitation is GPM IMERG Final v07 half-hourly (~0.1°; Huffman et al., 2020). Catchment forcing for the flood path uses CAMELS-CL (Alvarez-Garreton et al., 2018). The dated event inventory is the SERNAGEOMIN landslide/flow catalogue; events are dated to the year and month from the record identifier (the year column is unreliable), and the month itself is only approximate, the 25 March 2015 Quebrada de Macul-type flows of the Atacama event, for instance, are filed under March though the extreme rainfall fell over 24–26 March.

**Table 1.** Datasets.

| Dataset | Variable | Resolution | Use |
|---|---|---|---|
| RandomForest susceptibility | landslide susceptibility | 30 m | static background |
| CR2MET v2.5 | daily precipitation | 0.05°, daily | lumped & distributed forcing |
| GPM IMERG Final v07 | precipitation rate | ~0.1°, 30 min | sub-daily forcing |
| CAMELS-CL | precip, PET, streamflow | catchment, daily | flood path (GR4J/HBV) |
| SERNAGEOMIN inventory | dated events | point, ~month | validation target |

Study sites are the Río Maipo basin (central Andes, landslide backtest), the Río Itata catchment (humid south-central, flood path), and three dated debris-flow events spanning opposite climates: Atacama/Copiapó (25 March 2015, arid, convective), Cajón del Maipo (25 February 2017, central Andes, summer convective) and Villa Santa Lucía (16 December 2017, humid south, frontal).

## 4. Methods

### 4.1 Backtesting metrics

Validation of a nowcast is binary forecast verification: on each unit the model either alerts or not, and an event either occurred or not, giving a 2×2 contingency table and the standard categorical scores, probability of detection (POD), false-alarm ratio (FAR), critical success index (CSI), frequency bias. Because the inventory is dated only to ~month, the matching unit is the calendar month, and matching is **event-centred** with a ±tolerance window (an event is a hit if an alert falls within the window; a false alarm is an alerted month far from any event) to avoid inflating misses when one event spans a tolerance window.

For **spatial** verification on a grid, per-cell month matching against a spatially sparse, *incomplete* inventory makes CSI/FAR meaningless, almost every "false alarm" is a susceptible, wet cell with no *recorded* event. We therefore score discrimination, as the susceptibility and early-warning literatures do (Reichenbach et al., 2018): ROC-AUC (does the hazard rank event cell-months above quiet ones?) and POD at a fixed alerted-area fraction (catch rate for a given alert budget).

### 4.2 Threshold calibration and split-sample validation

The I–D intercept `a` is calibrated by sweeping it (fixed `b = 0.39`) and selecting the value that maximises CSI against the dated events; the trigger steepness `k` and the maximum rolling window are fixed (`k = 4`, `m_max = 7` days for the daily landslide path, I–D durations of storms, not seasonal accumulation). Validation is split-sample: the intercept is calibrated on odd calendar years and evaluated on even years.

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

### 5.2 Distributed daily backtest, an honest null

Repeating the backtest with **distributed** CR2MET rainfall over a 15 × 18 grid (270 cells, 123 120 cell-months, 884 in the event footprint), the **real** RandomForest susceptibility resampled to that grid, and spatial verification, yields essentially **no discrimination**: ROC-AUC is 0.463 for a lumped basin-mean baseline weighted by susceptibility, 0.477 for distributed rainfall with uniform susceptibility, and 0.485 for distributed rainfall weighted by real susceptibility, all near the random 0.5. An independent re-computation in Python confirms AUC = 0.488; the mean 7-day rainfall at recorded event cell-months (45 mm) is in fact *lower* than the grid-wide average (49 mm). At 5 km daily resolution the gridded rainfall does not discriminate where and when these landslides were recorded, and 30 m susceptibility averaged to 5 km loses its edge. Distributing the forcing does not help: the limiting factor is the *resolution* of the forcing and susceptibility (and the month-level inventory), not the lumping.

### 5.3 Resolution head-to-head

The previous null motivates a controlled comparison of forcing resolution on the same storm core (Atacama, 24–26 March 2015; the IMERG event-total maximum at lon −70.45, lat −27.15), with the identical I–D engine (Table 3). With a daily value the finest resolvable duration is 24 h, so the maximum resolvable mean intensity is (daily total)/24 h: CR2MET sees 30.1 mm spread to 0.66 mm h⁻¹, an exceedance of at most 0.62 (regional `a = 4.0`) or 0.17 (Caine), the daily product **structurally cannot trigger**, regardless of total. The same storm core in half-hourly IMERG peaks at 40 mm h⁻¹; the I–D threshold is crossed at 04:30 UTC on 24 March (`E ≈ 12`), hours before the documented flows. Higher-resolution forcing overcomes the limit with no change to the engine.

**Table 3.** CR2MET daily vs GPM IMERG half-hourly at the Atacama storm core (same I–D engine).

| | CR2MET daily (~5 km) | IMERG ½-hourly (~10 km) |
|---|---|---|
| event total | 30.1 mm | 108.5 mm |
| finest resolvable duration | 24 h | 0.5 h |
| max resolvable intensity | 0.66 mm h⁻¹ | 40.0 mm h⁻¹ |
| I–D (a=4.0) | no alert (E ≤ 0.62) | alert 24-Mar 04:30 UTC (E ≈ 12) |

### 5.4 Multi-event sub-daily lead time

The sub-daily result generalises across three dated debris-flow events in opposite climates (Table 4). In all three the half-hourly I–D crossing lands on or just before the documented day. For the Cajón del Maipo summer convective burst, the same rainfall aggregated to daily **never triggers**, only sub-daily resolution detects it. We emphasise that the inventory provides no event onset *hour*, so these are not formal lead-time skill scores but a demonstration that sub-daily forcing resolves the threshold-crossing time that daily forcing cannot.

**Table 4.** Sub-daily I–D crossing across three events (GPM IMERG half-hourly, storm core, `a = 4.0`).

| Event | Climate | Total | Peak 1 h | I–D crossing (UTC) | Documented day | Daily fires? |
|---|---|---|---|---|---|---|
| Atacama / Copiapó | arid, convective | 108 mm | 40 mm h⁻¹ | 24-Mar 04:30 | 25-Mar-2015 | yes |
| Cajón del Maipo | central Andes, summer | 28 mm | 5 mm h⁻¹ | 25-Feb 16:30 | 25-Feb-2017 | **no** |
| Villa Santa Lucía | humid south, frontal | 83 mm | 17 mm h⁻¹ | 15-Dec 15:00 | 16-Dec-2017 | yes |

### 5.5 Flood path

To exercise the discharge trigger, GR4J (Perrin et al., 2003) is run on the CAMELS-CL Río Itata catchment (1979–2016); the flood threshold is set at the 98th percentile of simulated discharge (24.8 mm day⁻¹). Alerts fire on 1.8 % of days (243 days), and the largest events fall in austral winter (June–August), consistent with a pluvial regime. The exact-attribution facility decomposes the largest event (96 mm day⁻¹, `Q/Q_c = 3.89`) into exposure and discharge factors.

### 5.6 Multi-trigger and physical coupling

Two demonstrations exercise the composable architecture. First, **rainfall ⊕ deformation**: on a transect whose LOS velocity rises from 2 to 45 mm yr⁻¹, a marginal sub-threshold rain day (rainfall factor 0.14 everywhere) leaves the hazard low where the ground is stable but, fused by noisy-OR with the deformation trigger (`v_crit = 20 mm yr⁻¹`), lifts the hazard above the alert level on the creeping cells, a slope already moving needs less rain to be flagged. Second, **physical refinement**: on the real Copiapó terrain stack, the agent-based debris-flow model run from a nowcast alert produces a runout footprint onto which the coarse probability is concentrated; the analogous shallow-water coupling concentrates a flood alert's probability in the channel cells that physically inundate, sparing the banks. These illustrate, rather than validate, the architecture.

## 6. Discussion

The headline is a methods result obtained *because* the engine holds the model fixed while the forcing varies. Three points follow.

**The resolution ceiling.** Distributing daily rainfall and adding a real susceptibility raster does not lift discrimination above chance (Section 5.2), yet the identical trigger on sub-daily forcing resolves the threshold crossing to within hours (Sections 5.3–5.4). The binding constraint is therefore the *resolution of the forcing*, not the susceptibility model or the threshold logic. This is intuitive once stated, with a daily value the maximum resolvable intensity is total/24 h, and short, intense, convective bursts are smeared below any I–D curve, but it is rarely isolated, because most studies vary the model on one product rather than the product under one model. The practical corollary is that an I–D nowcast is only as good as its rainfall's temporal resolution; effort spent on susceptibility sophistication is wasted where the forcing is daily.

**The inventory ceiling.** A second, independent ceiling is the validation target. The SERNAGEOMIN inventory is dated to ~month and is incomplete (notable events only). Month-level dating alone accounts for a large fraction of apparent misses (Table 2), and incompleteness makes per-cell FAR uninformative (Section 4.1). Credible discrimination and true lead-time skill require a day- or hour-resolution inventory, or event-segmented rainfall, a data problem, not a software one, and the single largest unlock for future validation.

**The architecture as enabler.** That both diagnoses were even possible rests on the forcing-agnostic design: swapping CR2MET daily for IMERG half-hourly, or adding a deformation trigger, requires no change to the hazard logic. The same interface lets the engine act as the downstream integrator of a family of process models, and the one-way on-demand coupling keeps the expensive physics confined to alerted areas.

## 7. Limitations and future work

We are explicit about what the engine does *not* yet do. (i) The hazard is a bounded *index*, not a calibrated probability; it carries no uncertainty, ensemble or reliability assessment. (ii) Trigger parameters are calibrated minimally (a single I–D intercept by CSI sweep); a formal calibration with uncertainty, for which the sibling rainfall–runoff engine already provides a DDS optimiser pattern, is future work, as is a comparison against an operational baseline (a published regional I–D threshold, persistence, climatology). (iii) Validation is hindcast; the engine has no real-time ingestion loop, which the name ultimately implies. (iv) Antecedent soil moisture is captured only implicitly through long rolling windows, not as an explicit state. (v) The multi-trigger combination weights (noisy-OR) and the deformation critical rate are not yet calibrated. (vi) The physical-coupling and multi-trigger results (Section 5.6) are illustrative demonstrations, not validations. Addressing (i), (ii) and a day-resolution inventory would convert the present diagnosis into a validated operational claim.

## 8. Conclusions

`nowcast` is an open, dependency-light Rust engine that turns static susceptibility into a time-varying hazard through a single interchangeable forcing interface and a composable trigger, with native providers for rainfall–runoff, snowmelt, shallow-water inundation, agent-based debris-flow runout, a geospatial substrate and InSAR deformation, and exact closed-form attribution of every alert. Used as an experimental instrument over Chilean basins, it yields an honest and, we argue, generalisable result: the resolution of the forcing, not the model, sets the skill ceiling of intensity–duration nowcasting. A global I–D threshold never fires on daily forcing; a calibrated regional threshold transfers but, distributed at daily resolution, does not discriminate event locations (AUC ≈ 0.48); the identical trigger on half-hourly satellite forcing pins the threshold crossing to a timestamp hours ahead of documented flows across three climatically distinct events. The engine, its backtesting framework and all examples are reproducible, and the architecture is designed so that higher-resolution forcing, the demonstrated path to skill, can be plugged in without touching the hazard logic.

## Code and data availability

The `nowcast` source (eight Rust crates, fourteen examples) and all data-extraction scripts are openly available; a citable archive with a DOI will be deposited on Zenodo at submission. Input datasets are third-party and openly documented: CR2MET v2.5, GPM IMERG Final v07 (NASA GES DISC), CAMELS-CL (Alvarez-Garreton et al., 2018, PANGAEA https://doi.org/10.1594/PANGAEA.894885) and the SERNAGEOMIN inventory. Derived per-event series are regenerable from the provided scripts. *Note: the reference list below is a working set and must be verified (e.g. exact volume, pages and DOIs) before submission.*

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
- Huffman, G.J., et al. (2020). GPM IMERG Final Precipitation L3 Half Hourly, V07. NASA GES DISC.
- Lundberg, S.M., Lee, S.-I. (2017). A unified approach to interpreting model predictions. *Advances in Neural Information Processing Systems (NeurIPS)*, 30.
- Perrin, C., Michel, C., Andréassian, V. (2003). Improvement of a parsimonious model for streamflow simulation (GR4J). *Journal of Hydrology*, 279, 275–289.
- Reichenbach, P., Rossi, M., Malamud, B.D., Mihir, M., Guzzetti, F. (2018). A review of statistically-based landslide susceptibility models. *Earth-Science Reviews*, 180, 60–91.
- Segoni, S., Piciullo, L., Gariano, S.L. (2018). A review of the recent literature on rainfall thresholds for landslide occurrence. *Landslides*, 15, 1483–1501.
- Seibert, J., Vis, M.J.P. (2012). Teaching hydrological modeling with a user-friendly catchment-runoff-model software package (HBV-light). *Hydrology and Earth System Sciences*, 16, 3315–3325.
- Toro, E.F., Spruce, M., Speares, W. (1994). Restoration of the contact surface in the HLL Riemann solver (HLLC). *Shock Waves*, 4, 25–34.
