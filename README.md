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
timestamp (hours of lead time before the flows), while the same rain aggregated
to daily can only flag the day — no intra-day timing. (Logic smoke-tested;
running on real IMERG needs the one-time Earthdata GES DISC authorization noted
in `data/README.md`.)

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

## Roadmap

- Acople with Hydroflux and XAI (SHAP) for traceability.
- CLI runner and PyO3 bindings (`nowcast-cli`, `nowcast-python`), matching the
  family's crate layout.

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
```

## License

MIT OR Apache-2.0
