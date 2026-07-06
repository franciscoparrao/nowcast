//! Day-resolution skill scoring end-to-end — the reproducible skeleton for the
//! COOLR × IMERG experiment, on synthetic data with known ground truth.
//!
//! Motivation. The month-dated SERNAGEOMIN backtest can only score at monthly
//! resolution, which caps the achievable verification (Section on limitations in
//! the paper). A day-dated inventory such as NASA's Global Landslide Catalog /
//! COOLR, forced by half-hourly GPM IMERG, would instead permit a genuine
//! day-resolution skill score with a real non-event baseline. This example wires
//! the whole pipeline together on a controlled synthetic target so the plumbing
//! is exercised and reproducible *before* the real data are plugged in: swap the
//! synthetic `GriddedRain` for IMERG granules and the planted events for the
//! COOLR catalogue (via the `scripts/extract_*_imerg.py` pattern) and nothing
//! else changes.
//!
//! What it demonstrates:
//!   - sub-daily (hourly) forcing runs through the unchanged engine;
//!   - per-cell/day alerts collapse to a day key for a day-dated inventory;
//!   - `spatial_daily_contingency` scores POD/FAR/CSI with a non-event baseline;
//!   - `roc_auc` and `pod_at_area` score discrimination the way a sparse
//!     inventory demands.
//!
//! Run with: `cargo run --example daily_skill_synthetic`

use std::collections::BTreeSet;

use nowcast_core::{
    DayKey, GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, pod_at_area, roc_auc,
    spatial_daily_contingency,
};

fn main() {
    // --- Scene: 8×6 grid, 10 days at hourly (sub-daily) resolution. ---
    let (ncols, nrows) = (8usize, 6usize);
    let dims = GridDims::new(ncols, nrows);
    let n_cells = dims.len();
    let n_days = 10usize;
    let steps_per_day = 24usize;
    let n_steps = n_days * steps_per_day;

    // Uniform, moderately-high susceptibility: discrimination must come from the
    // forcing, which is the paper's thesis (resolution of the forcing sets skill).
    let susc = SusceptibilityMap::new(dims, vec![0.8; n_cells]).unwrap();

    // Light basin-wide drizzle everywhere (never triggers the I–D curve).
    let mut depths = vec![0.1f64; n_steps * n_cells]; // 0.1 mm/h background

    // Inject a short, intense burst at (cell, day): `peak` mm/h for `hours`.
    let mut burst = |cell: usize, day: usize, peak: f64, hours: usize| {
        for h in 0..hours {
            let step = day * steps_per_day + h;
            depths[step * n_cells + cell] = peak;
        }
    };

    // Three planted "COOLR" events: cell, day, with a triggering sub-daily burst.
    let events: Vec<(usize, DayKey)> = vec![(19, 2), (28, 5), (10, 8)];
    for &(cell, day) in &events {
        burst(cell, day as usize, 20.0, 2); // 40 mm in 2 h → well over Caine
    }

    // One nuisance storm well away from every event in space AND time (cell 5,
    // day 0: >1 cell and >1 day from all events), so it falls outside every
    // event footprint and registers as a genuine false alarm.
    burst(5, 0, 18.0, 2);

    let forcing = GriddedRain::new(dims, 1.0, depths).unwrap();

    // Caine (1980) global I–D threshold; default logistic trigger; 24-step window.
    let threshold = IdThreshold::caine();
    let trigger = nowcast_core::TriggerModel::new(4.0).unwrap();
    let engine = Nowcast::new(susc, forcing, threshold, trigger, steps_per_day).unwrap();

    let fields = engine.run(); // one HazardField per hourly step

    // --- Collapse hourly hazard to a per-(cell, day) score (daily max). ---
    let alert_level = 0.30;
    let mut scores = vec![0.0f64; n_cells * n_days]; // day-major: day*n_cells + cell
    let mut alerted: BTreeSet<(usize, DayKey)> = BTreeSet::new();
    for day in 0..n_days {
        for cell in 0..n_cells {
            let mut day_max = 0.0f64;
            for h in 0..steps_per_day {
                let p = fields[day * steps_per_day + h].probability()[cell];
                day_max = day_max.max(p);
            }
            scores[day * n_cells + cell] = day_max;
            if day_max >= alert_level {
                alerted.insert((cell, day as DayKey));
            }
        }
    }

    // Ground-truth labels aligned 1:1 with `scores` (exact cell/day events).
    let event_set: BTreeSet<(usize, DayKey)> = events.iter().copied().collect();
    let labels: Vec<bool> = (0..n_days)
        .flat_map(|day| (0..n_cells).map(move |cell| (cell, day as DayKey)))
        .map(|cd| event_set.contains(&cd))
        .collect();

    // --- Score. Contingency uses a ±1 cell, ±1 day matching window. ---
    let days: Vec<DayKey> = (0..n_days as DayKey).collect();
    let c = spatial_daily_contingency(dims, &days, &alerted, &events, 1, 1);
    let auc = roc_auc(&scores, &labels).unwrap().unwrap();
    let pod10 = pod_at_area(&scores, &labels, 0.10).unwrap().unwrap();

    println!("Day-resolution skill (synthetic COOLR × sub-daily forcing)");
    println!("  grid {ncols}×{nrows}, {n_days} days, hourly forcing, {} planted events", events.len());
    println!(
        "  contingency: hits {} misses {} false_alarms {} correct_negatives {}",
        c.hits, c.misses, c.false_alarms, c.correct_negatives
    );
    println!(
        "  POD {:.2}  FAR {:.2}  CSI {:.2}",
        c.pod().unwrap_or(f64::NAN),
        c.far().unwrap_or(f64::NAN),
        c.csi().unwrap_or(f64::NAN),
    );
    println!("  ROC-AUC {auc:.3}   POD@10%area {pod10:.2}");

    // The engine catches every planted event at sub-daily resolution...
    assert_eq!(c.hits as usize, events.len(), "all planted events should be hit");
    assert_eq!(c.misses, 0);
    // ...ranks event cell-days far above the field...
    assert!(auc > 0.95, "sub-daily forcing should discriminate (AUC {auc:.3})");
    assert!(pod10 >= 1.0 - 1e-9, "the tiny event set fits inside the top 10% area");
    // ...and the nuisance storm shows up as a genuine false alarm, not hidden.
    assert!(c.false_alarms >= 1, "the non-event storm should register as a false alarm");

    println!("\nPipeline verified end-to-end. Swap the synthetic forcing for IMERG");
    println!("granules and the planted events for the COOLR catalogue to run for real.");
}
