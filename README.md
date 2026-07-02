# nowcast

Dynamic **geohazard nowcasting** engine in Rust. Instead of a static
susceptibility surface, `nowcast` modulates susceptibility in time with a
dynamic trigger (rainfall, snowmelt) to produce a hazard probability that
changes step by step:

```
hazard(cell, t) = susceptibility(cell) × trigger_factor(I–D exceedance, t)
```

Part of the author's Rust geohazard family (SurtGIS, Hydroflux, Smelt, Anvil,
Cantus, Criterium) and a downstream integrator of `rainflow` (rainfall–runoff)
and `snowmelt-rs` (snowmelt).

## Building

`nowcast-core` is self-contained (`std` + `thiserror`) and builds and tests with no
network and no system libraries — it carries the paper's headline resolution
results on its own:

```bash
cargo test -p nowcast-core
cargo run -p nowcast-core --release --example synthetic_resolution
```

The **provider crates** (`nowcast-rainflow`, `-snowmelt`, `-hydroflux`, `-surtgis`,
`-swarm`, `-insar`, `-firespread`) wrap sibling engines from the author's Rust
geohazard family through **path dependencies**. Building the *full* workspace
(the bare `cargo build` / `cargo test` default) requires those repositories
checked out at these exact paths relative to this repository's root:

| Crate               | Engine dependency     | Expected checkout            |
|---------------------|-----------------------|------------------------------|
| `nowcast-rainflow`  | `rainflow-core`       | `../rainflow`                |
| `nowcast-snowmelt`  | `snowmelt-core`       | `../snowmelt-rs`             |
| `nowcast-surtgis`   | `surtgis-core`        | `../surtgis`                 |
| `nowcast-swarm`     | `swarm-abm` + `debris-flow` | `../swarm-abm`        |
| `nowcast-insar`     | `insar-core`          | `../insar-rs`                |
| `nowcast-firespread`| `firespread-core`     | `../firespread`              |
| `nowcast-hydroflux` | `hydroflux-solver-2d` | `../postdoc/hydroflux`       |
| `nowcast-cli`       | `surtgis-core`        | `../surtgis`                 |

`scripts/check_siblings.sh` verifies the layout. **Without the siblings, build
`nowcast-core` and its examples on their own** (`cargo test -p nowcast-core`) —
the core is self-contained and carries all headline results.

## Status — v0.1 (decoupled core)

`nowcast-core` is functional and dependency-light (`std` + `thiserror`), so it
builds and tests fully offline with no upstream Rust engines required.

- `Forcing` trait — dynamic water-input forcing over a grid and time axis.
- `UniformRain` — replays an observed single-gauge series (CR2/DGA) over a
  susceptibility raster; includes a `std`-only CSV reader.
- `IdThreshold` — empirical intensity–duration curve `I = a·D⁻ᵇ`, with a
  `caine()` preset (Caine, 1980).
- `TriggerModel` — logistic map from I–D exceedance to a `[0, 1]` factor.
- `Nowcast` — combines the above into per-step `HazardField`s and `Alert`s,
  using per-cell prefix sums for O(1) rolling-window accumulation.
- `backtest` — contingency metrics (POD, FAR, CSI, frequency bias) with
  event-centric monthly matching against a dated inventory.

### Quick start

```bash
cargo run --example quickstart   # observed rain × susceptibility → hazard + alerts
cargo test                       # unit + doctests
cargo clippy -- -D warnings

# Real backtest (Río Maipo, CR2MET 1979–2016 × SERNAGEOMIN inventory):
python3 scripts/extract_maipo_cr2met.py   # regenerate derived data (numpy + netCDF4)
cargo run --example backtest
```

### Backtest findings (Río Maipo, v0.1)

Validating the I–D trigger against 157 dated rainfall-triggered events:

- The **Caine (1980) global threshold (a=14.82) never fires** on CR2MET daily
  forcing here (POD 0) — a regional intercept is required.
- A calibrated **regional intercept a\*≈5.5 mm/h @ D=1h** is robust and transfers
  split-sample (calibrate odd years → validate even years, POD ≈ 0.50).
- **FAR ≈ 0.9 is structural**: the event base rate is ~4% and a single
  basin-centroid gauge over a Mediterranean wet-season climate makes the I–D
  trigger over-predict — motivating susceptibility weighting, antecedent
  moisture, and distributed forcing (v0.2).
- Inventory month-dating noise costs ~0.3 of POD (±0→±3 month tolerance:
  POD 0.21→0.68).

### Distributed backtest (v0.2, `backtest_distributed`)

Repeating the backtest with **distributed** CR2MET rainfall over a 15×18 grid,
the **real** RandomForest susceptibility raster, and **spatial** (per-cell)
verification — scored with ROC-AUC and POD-at-area, since CSI/FAR are meaningless
for a spatially sparse, incomplete inventory. The result is an honest **null**:
all configurations sit at AUC ≈ 0.48 (verified independently in Python). At
CR2MET's 5 km / daily resolution the gridded rainfall does not discriminate the
recorded event cell-months, and 30 m susceptibility averaged to 5 km loses its
edge. **The bottleneck is forcing/susceptibility resolution (and month-level
inventory dating), not the lumping** — the case for plugging higher-resolution
forcing into the swappable `Forcing` trait (sub-basin rainflow/snowmelt, radar/
satellite QPE).

## v0.2 — distributed snowmelt forcing (done)

`nowcast-snowmelt` wraps the `snowmelt-rs` degree-day engine and implements
`Forcing` with **per-cell rain + snowmelt runoff**, the rain-on-snow path the
single gauge cannot represent. It pre-runs the (stateful) snow simulation once
and buffers the runoff so the nowcast can random-access it for the I–D windows.

```bash
cargo run -p nowcast-snowmelt --example rain_on_snow
```

On the same warm storm, a pre-existing snowpack raises basin water input ~+46%
(rain + melt vs rain alone), distributed down the elevation transect by the
lapse-rate temperature field — and the peak hazard with it.

## High-resolution sub-daily forcing (GPM IMERG)

The distributed backtest found *temporal* resolution to be a limiter. The
`atacama_subdaily` example feeds the engine **half-hourly** GPM IMERG rainfall
for the March 2015 Atacama debris-flow disaster and shows what daily forcing
cannot: the *time* the I–D threshold is crossed, and the resulting lead time.

```bash
python3 scripts/extract_atacama_imerg.py   # needs Earthdata creds + GESDISC app authorized
cargo run --example atacama_subdaily
```

Same engine, two resolutions: half-hourly pins the threshold crossing to a
timestamp, while the same rain aggregated to daily can only flag the day — no
intra-day timing. On the real IMERG data, the storm-core cell (lon −70.45,
lat −27.15; 108.5 mm event total) peaks at 40 mm/h and the I–D threshold is
crossed at 2015-03-24 05:00 UTC — hours ahead of the documented flows, a
timestamp the daily aggregate (107.6 mm on the 24th, no intra-day structure)
cannot resolve.

**Head-to-head (`resolution_headtohead`)** — same storm core, same I–D engine,
CR2MET daily vs IMERG half-hourly:

| | CR2MET daily (~5 km) | IMERG ½-hourly (~10 km) |
|---|---|---|
| event total | 30.1 mm | 108.5 mm |
| max resolvable intensity | 0.66 mm/h | 40.0 mm/h |
| I–D (a=4.0) | **no alert** (E ≤ 0.62) | **alert 24-Mar 04:30** (E≈12) |

With a daily value the finest resolvable duration is 24 h, so the peak intensity
is capped at *(total / 24 h)* and smeared below the I–D curve — the daily product
**structurally cannot trigger**, regardless of total. Higher-resolution forcing
overcomes the limit with no change to the engine (same `Forcing` trait).

**Multi-event generalisation (`multi_event_leadtime`)** — three dated aluviones
across opposite climates, same I–D engine on IMERG half-hourly:

| event | climate | total | peak 1 h | I–D crossing (UTC) | documented day | daily fires? |
|---|---|---|---|---|---|---|
| Atacama / Copiapó | arid N · convective | 108 mm | 40 mm/h | 24-Mar 04:30 | 25-Mar-2015 | yes |
| Cajón del Maipo | central Andes · summer | 28 mm | 5 mm/h | 25-Feb 16:30 | 25-Feb-2017 | **no** |
| Villa Santa Lucía | humid S · frontal | 83 mm | 17 mm/h | 15-Dec 15:00 | 16-Dec-2017 | yes |

The sub-daily crossing lands on or just before the documented day in all three.
For the Cajón del Maipo convective burst the daily product never triggers — only
sub-daily resolution detects it.

## v0.2 — flood nowcasting from routed discharge (done)

`nowcast-rainflow` wraps the `rainflow` GR4J/HBV engine. A flood's trigger is
**discharge over a threshold** `Q_c` (the catchment routing already integrated
the rainfall), not a rainfall I–D curve — so it uses `Q(t)/Q_c` exceedance while
reusing the same `susceptibility × trigger` structure, `HazardField` and `Alert`.

```bash
cargo run -p nowcast-rainflow --example itata_flood
```

GR4J on the CAMELS-CL Itata catchment (1979–2016) → discharge → flood hazard:
the threshold (98th-percentile discharge) flags ~1.8% of days, and the largest
events all fall in austral winter (Jun–Aug), as expected for central-south Chile.

## Explainability (exact attribution)

Because the hazard is closed-form (`susceptibility × trigger_factor`), every
alert is **exactly** attributable — no surrogate model, no sampling. `explain`
decomposes a cell/step into its terrain and weather factors, the I–D window that
drove the trigger, and the binding constraint, plus a counterfactual
(`intensity_to_alert`: how much rain would lift the cell to the alert level).
SHAP applies one layer up, to the upstream ML susceptibility that enters here as
an already-interpretable input.

```bash
cargo run --example explain_alert
```

## Physical refinement (Hydroflux coupling)

`nowcast-hydroflux` wraps the `hydroflux` 2D shallow-water solver (HLLC + Audusse
+ Manning). One-way, on-demand: where the nowcast flags a flood, route the
discharge over the local DEM to turn the coarse probability into a **physical
inundation depth** per cell. `discharge_to_inflow_m3s` converts a routed
discharge to a volumetric inflow; `DepthField::refined_hazard` downscales the
alert onto the inundation footprint.

```bash
cargo run -p nowcast-hydroflux --example couple_flood
```

On a synthetic valley a 23 m³/s inflow concentrates in the channel (0.44 m) and
spares the banks — the coarse alert's 0.7 probability lands on the 24 of 264
cells that actually flood. (Pulls the hydrodynamic stack, so it builds online
once; the core stays offline.)

## Python bindings (`nowcast-python`)

PyO3 bindings expose the engine to Python (abi3 wheel, CPython ≥ 3.9) so it can be
driven from the susceptibility pipeline. Build with maturin:

```bash
cd crates/nowcast-python && maturin develop --release
```

```python
import nowcast as nc
m = nc.Nowcast.uniform([0.8]*9, 3, 3, [2, 5, 40, 80, 3], 24.0, id_a=6.0)
m.alerts(0.5)                 # [(step, n_cells, fraction, max_prob), ...]
m.explain(0, 3)              # exact terrain × trigger attribution (dict)

live = nc.LiveNowcast([0.8]*9, 3, 3, 24.0, id_a=6.0)
haz = live.push([40.0]*9)   # streaming, bit-identical to the batch engine

cal = nc.Calibrator.fit_isotonic(scores, outcomes)
nc.reliability(cal.calibrate(scores), outcomes, 10)   # Brier, skill, ECE, Wilson CIs
```

## Ensemble (probabilistic) nowcasting (`ensemble`)

The engine side of forecast forcing (SOTA roadmap axis 1). Given an ensemble of
forcing members — e.g. an ensemble rainfall nowcast (pySTEPS, or a deep generative
model such as DGMR) — `ensemble_hazard` runs the engine on each member and
aggregates to a **probabilistic hazard**: per-cell exceedance probability, mean and
spread. The members enter through the ordinary `Forcing` interface, so a real QPF
ensemble plugs in without touching the hazard logic; the exceedance probability
feeds the calibration tools.

```bash
cargo run --release --example ensemble_nowcast
```

The example shows the raw exceedance probability beating a deterministic forecast
and carrying uncertainty (spread), then isotonic calibration making it reliable.

## Calibrated probability (`calibrate`)

The hazard is a bounded *index*, not a probability. The `calibrate` module turns
it into one and quantifies the uncertainty, dependency-free:

- `Calibrator::fit_isotonic(scores, outcomes)` fits a monotone *index →
  probability* map by isotonic regression (pool-adjacent-violators);
- `reliability(preds, outcomes, n_bins)` returns a reliability diagram (predicted
  vs observed per bin with **Wilson 95 % intervals**), the **Brier** score and
  skill, and the expected calibration error.

```bash
cargo run --example calibrated_probability
```

In the example the raw index scores *worse* than climatology (Brier skill −0.04);
isotonic calibration lifts it to +0.24 and cuts the calibration error ~20×, with
honest per-bin uncertainty bands.

## Real-time loop (`LiveNowcast`)

The batch engine replays a whole series; `LiveNowcast` ingests forcing **one step
at a time** and emits a hazard field immediately, keeping only a bounded ring
buffer of recent prefix sums per cell (O(`max_window`) memory). It is
**bit-identical** to the batch engine on the same data — the streaming path is a
memory optimisation, not an approximation:

```rust
let mut engine = LiveNowcast::new(susc, threshold, trigger, max_window, dt_h)?;
let field = engine.push(&depths_this_step)?;     // alert as each step arrives
if let Some(a) = field.alert(0.5) { /* notify */ }
```

A `StepSource` abstracts where steps come from (a replayed forcing, a growing
file, a polled feed); `run_live` drives a source through the engine. See
`examples/live_loop.rs` (with a batch-parity assertion) and the `nowcast watch`
CLI verb.

## Command-line interface (`nowcast-cli`)

The `nowcast` binary exposes the engine without writing Rust:

```bash
# Run: susceptibility × rainfall → per-step hazard GeoTIFFs + alerts
nowcast run --susc susceptibility.tif --rain-rasters r0.tif,r1.tif,r2.tif \
            --dt-hours 0.5 --id-a 4.0 --out-dir hazard/
nowcast run --uniform-susc 0.8 --rain-csv gauge.csv --ncols 3 --id-a 6.0

# Backtest the I–D trigger against a dated inventory, sweeping the intercept
nowcast backtest --rain-csv data/maipo_cr2met_pr_1979_2016.csv \
                 --events-csv data/maipo_events_dated.csv --sweep 2:16:0.5

# Explain one cell/step: exact terrain × trigger attribution
nowcast explain --uniform-susc 0.8 --rain-csv gauge.csv --ncols 3 --cell 0 --step 3

# Watch: stream a gauge CSV through the real-time engine, alerting step by step
nowcast watch --uniform-susc 0.7 --rain-csv gauge.csv --ncols 4 --id-a 6.0
```

Susceptibility is a GeoTIFF (georeferenced output) or a uniform value; rainfall
is a single-gauge CSV column (broadcast over the grid) or a stack of per-step
GeoTIFFs (distributed forcing). Build with `cargo build -p nowcast-cli --release`.

## Roadmap
- A live data feed for the real-time loop and validation of the probability
  calibration on real held-out events (the machinery is in place; see the
  limitations in the manuscript).

## Workspace layout

```
crates/nowcast-core/       # the engine
  src/
    forcing.rs    Forcing trait + UniformRain (observed series, CSV)
    grid.rs       GridDims + SusceptibilityMap
    threshold.rs  IdThreshold (I = a·D^-b)
    trigger.rs    TriggerModel (logistic exceedance → factor)
    nowcast.rs    Nowcast engine + HazardField + Alert
    backtest.rs   Contingency metrics + monthly event matching
  examples/{quickstart,backtest}.rs
crates/nowcast-snowmelt/   # v0.2 distributed rain+melt Forcing provider
  src/lib.rs               SnowmeltForcing (wraps snowmelt-core)
  examples/rain_on_snow.rs
crates/nowcast-rainflow/   # v0.2 flood provider: discharge-exceedance trigger
  src/lib.rs               RainflowForcing + FloodThreshold + FloodNowcast
  examples/itata_flood.rs
crates/nowcast-hydroflux/  # v0.2 physical refinement: 2D shallow-water inundation
  src/lib.rs               Inundation + DepthField (+ couple_flood example)
crates/nowcast-surtgis/    # geospatial bridge: Raster<->grid, GeoTIFF in/out
  src/lib.rs               susceptibility_from_raster / write_hazard_geotiff
  examples/geotiff_roundtrip.rs
crates/nowcast-swarm/      # agent-based debris-flow runout refinement
  src/lib.rs               run_runout + Runout (+ couple_runout example)
crates/nowcast-insar/      # InSAR deformation as a 2nd trigger
  src/lib.rs               DeformationForcing + deformation_trigger
  examples/rain_and_creep.rs
crates/nowcast-firespread/ # wildfire hazard path + post-fire susceptibility cascade
  src/lib.rs               run_fire + FireField + post_fire_susceptibility
  examples/couple_fire.rs
crates/nowcast-cli/        # command-line runner: run / backtest / explain
  src/main.rs              `nowcast` binary (clap)
```

## Wildfire and the post-fire cascade (firespread coupling)

`nowcast-firespread` adds fire as a parallel hazard and, more importantly, the
**post-fire cascade**: a burn scar sharply lowers the rainfall needed to trigger a
debris flow. `run_fire` drives the `firespread` engine (Rothermel + minimum travel
time) to a burn footprint; `post_fire_susceptibility` amplifies the static
susceptibility inside the scar; the ordinary rainfall nowcast then sees the
elevated hazard. In the example, a modest storm that leaves the unburned slope
below threshold flips hundreds of scar cells into alert.

```bash
cargo run -p nowcast-firespread --example couple_fire
```

## Composable triggers (rainfall ⊕ deformation)

The core generalises beyond a single rainfall trigger: a `Trigger` yields a
factor per cell/step, and `MultiNowcast` fuses several (`Combine::{Max, NoisyOr,
Product}`). `IdTrigger` is the rainfall I–D path; `ThresholdTrigger` a
duration-independent value/threshold. `nowcast-insar` adds ground deformation:
a LOS velocity field from `insar-rs` becomes a `DeformationForcing`, so a slope
that is already creeping needs less rain to be flagged.

```bash
cargo run -p nowcast-insar --example rain_and_creep
```

## Agent-based runout refinement (swarm-abm coupling)

`nowcast-swarm` is the landslide-side counterpart of `nowcast-hydroflux`: where
the nowcast flags a debris-flow alert, it runs the `swarm-abm` debris-flow model
(rain + flow agents over the terrain, calibrated on the 2015 Atacama event) to
simulate the **runout footprint**, then downscales the coarse probability onto
it (`Runout::refined_hazard`).

```bash
cargo run -p nowcast-swarm --example couple_runout
```

## Geospatial I/O (SurtGIS bridge)

`nowcast-core` is I/O-free; `nowcast-surtgis` is how real georeferenced data
enters and leaves. It converts SurtGIS `Raster<f32>` ↔ `SusceptibilityMap` /
`GriddedRain` / `HazardField` and writes hazard fields to GeoTIFF (native, no
GDAL). The example ingests the real 30 m RandomForest susceptibility of the Río
Maipo (5149×5855) and writes a georeferenced hazard GeoTIFF.

```bash
cargo run -p nowcast-surtgis --example geotiff_roundtrip
```

## Reproducibility

The core builds and tests fully offline (`std` + `thiserror`); the provider
crates pull in sibling engines but vendor no system libraries (GeoTIFF I/O is
pure-Rust, no GDAL). Every result and figure in the manuscript is produced by a
runnable example — there are no hidden scripts:

```bash
cargo test --workspace            # full unit + doc test suite
cargo clippy --workspace -- -D warnings
cargo run --release --example backtest               # lumped Maipo I–D backtest
cargo run --release --example published_thresholds   # 35 published I–D curves (Guzzetti et al. 2007)
cargo run --release --example synthetic_resolution   # controlled resolution experiment
cargo run -p nowcast-rainflow --release --example itata_validation  # flood path vs observed Q
```

Derived per-event data are regenerated by the `scripts/extract_*.py` helpers
(Python 3 with `numpy`/`xarray`); raw inputs are third-party and openly
documented (CR2MET v2.5, GPM IMERG Final v07, CAMELS-CL, the SERNAGEOMIN
inventory). Figures are rendered by `docs/paper/figs.R` (R + `ggplot2`).

## Citation

If you use this software or its results, please cite the manuscript (see
`CITATION.cff`). A citable archive with a DOI will be deposited on Zenodo at
submission.

## License

Dual-licensed under either of

- Apache License, Version 2.0 ([`LICENSE-APACHE`](LICENSE-APACHE))
- MIT license ([`LICENSE-MIT`](LICENSE-MIT))

at your option. Unless you explicitly state otherwise, any contribution
intentionally submitted for inclusion in the work by you, as defined in the
Apache-2.0 license, shall be dual-licensed as above, without any additional
terms or conditions.
