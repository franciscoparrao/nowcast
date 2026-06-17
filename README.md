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

## Roadmap

- **v0.2** — native `Forcing` providers wrapping the sibling engines:
  `rainflow` (routed discharge → flood nowcasting) and `snowmelt-rs`
  (rain + snowmelt runoff per cell → rain-on-snow landslide triggering).
- Backtesting against dated event inventories (SERNAGEOMIN): hit rate, false
  alarms, lead time.
- CLI runner and PyO3 bindings (`nowcast-cli`, `nowcast-python`), matching the
  family's crate layout.

## Workspace layout

```
crates/nowcast-core/   # the engine (this is what exists today)
  src/
    forcing.rs    Forcing trait + UniformRain (observed series, CSV)
    grid.rs       GridDims + SusceptibilityMap
    threshold.rs  IdThreshold (I = a·D^-b)
    trigger.rs    TriggerModel (logistic exceedance → factor)
    nowcast.rs    Nowcast engine + HazardField + Alert
  examples/quickstart.rs
```

## License

MIT OR Apache-2.0
