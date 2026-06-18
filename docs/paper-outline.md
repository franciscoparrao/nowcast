# Paper outline — dynamic geohazard nowcasting & the forcing-resolution limit

> Working scaffold for the manuscript. Maps each claim to a reproducible example
> and the real data behind it. Language: English (international venues).

## Working titles
- *"Resolution, not the model, limits dynamic landslide nowcasting: evidence from
  Chile with an interchangeable-forcing engine"*
- *"From static susceptibility to time-resolved hazard: a forcing-agnostic
  nowcasting engine and what rainfall resolution it actually needs"*

## One-sentence contribution
A dependency-light Rust engine that modulates static susceptibility by a dynamic
trigger through a single interchangeable `Forcing` interface, used to show — on
real Chilean inventories and three precipitation products — that the binding
constraint on operational landslide/flood nowcasting is the **resolution of the
forcing**, not the susceptibility model or the threshold logic.

## Why it's publishable (the honest hook)
Most nowcasting papers report a skill score on one product and one region. We
instead hold the *engine* fixed and vary the *forcing resolution* (CR2MET daily
5 km → CR2MET distributed → GPM IMERG half-hourly), turning an apparent null
(distributed daily ≈ random discrimination) into a constructive result: daily
products *structurally* cannot trigger intensity–duration nowcasting, and
sub-daily forcing recovers hours of lead time across opposite climates.

## Structure

1. **Introduction** — susceptibility is static; the SOTA gap is dynamic
   triggering (rainfall, snowmelt). Operational nowcasting needs forcing the
   community rarely interrogates by resolution.
2. **Engine** — `hazard(cell,t) = susceptibility × trigger(exceedance,t)`. The
   `Forcing` trait (random-access water-input field) decouples the hazard logic
   from the data source; two trigger families (rainfall I–D for landslides,
   discharge Q/Q_c for floods). Rust, std-only core, native providers wrapping
   `rainflow` (GR4J/HBV) and `snowmelt-rs`. → Fig 1 (architecture).
3. **Data & study sites** — SERNAGEOMIN dated inventories (15 BNA basins);
   CR2MET v2.5 daily; CAMELS-CL; GPM IMERG v07 half-hourly. Río Maipo (landslide
   backtest), Itata (flood), Atacama/Maipo/Santa Lucía events (sub-daily).
4. **Methods** — I–D threshold & logistic trigger; backtesting (monthly
   contingency POD/FAR/CSI; spatial event-centric matching; ROC-AUC, POD@area);
   regional threshold calibration + split-sample.
5. **Results**
   - 5.1 Lumped landslide backtest (Maipo): Caine global → POD 0; regional
     a*≈5.5 mm/h, split-sample val POD≈0.50; FAR structural. → Fig 2.
   - 5.2 Distributed daily backtest: AUC≈0.48 across configs → resolution, not
     lumping, is the limit. → Fig 3.
   - 5.3 Flood nowcast (Itata): Q98 threshold, austral-winter peaks. → Fig 4.
   - 5.4 Rain-on-snow distributed forcing: +46% water input, lapse-rate spread. → Fig 5.
   - 5.5 Sub-daily lead time (head-to-head + 3-climate table): daily structurally
     cannot trigger; IMERG pins crossing to a timestamp. → Fig 6 (lead-time SVG)
     + Table 1 (multi-event).
6. **Discussion** — the resolution ceiling; inventory month-dating as a second
   ceiling; operational implication (where high-res QPE exists, the engine is
   immediately useful); the interchangeable-forcing design as the enabler.
7. **Limitations** — single-gauge/centroid at v0.1; IMERG overestimation in
   hyper-arid zones; month-resolution inventory caps lead-time validation;
   no temperature series for distributed snowmelt over long periods.
8. **Conclusions & reproducibility** — Zenodo DOI of the code + derived data
   scripts; all results regenerable from the six examples.

## Figures / tables → reproducible source
| # | content | example / script |
|---|---|---|
| 1 | engine architecture & `Forcing` trait | (diagram) |
| 2 | Maipo I–D calibration sweep + split-sample | `backtest` |
| 3 | distributed AUC / POD@area | `backtest_distributed` |
| 4 | Itata flood hydrograph + threshold | `itata_flood` |
| 5 | rain-on-snow amplification by elevation | `rain_on_snow` |
| 6 | sub-daily lead-time timeline (Atacama) | `atacama_subdaily` + showcase SVG |
| T1| multi-event lead-time, 3 climates | `multi_event_leadtime` |
| T2| CR2MET vs IMERG head-to-head | `resolution_headtohead` |

## Target venues (rationale)
- **NHESS** (Copernicus, open) — methods + multi-hazard + the honest
  resolution/limitations framing fits its scope and review culture. *First choice.*
- **Landslides** (Springer) — if framed landslide-first (I–D + inventory).
- **Natural Hazards** (Springer) — broader hazard + EWS angle, more applied.

## Open decisions (for the user)
- Venue & framing (multi-hazard methods vs landslide-first).
- Scope of v0.2 to include (both providers, or core + IMERG resolution study only).
- Whether to add 1–2 more dated events to Table 1 before submission.
