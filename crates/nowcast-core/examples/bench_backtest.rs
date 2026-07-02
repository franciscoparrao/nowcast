//! Catalog-scale benchmark of the spatial day-resolution matcher.
//!
//! Sizes chosen to mimic a COOLR × IMERG national run — the workload the
//! optimised `spatial_daily_contingency` exists for: a 500×400 grid (200 000
//! cells), a decade of days, ~1 M alerted (cell, day) pairs and 5 000 dated
//! events. The pre-optimisation matcher (event × alert scan + days × cells
//! sweep) needs ~5·10⁹ set probes here; the current one probes each event's
//! own space–time window and counts correct negatives by set arithmetic.
//!
//! Run with: `cargo run --release --example bench_backtest`

use std::collections::BTreeSet;
use std::time::Instant;

use nowcast_core::{DayKey, GridDims, spatial_daily_contingency};

fn main() {
    let dims = GridDims::new(500, 400);
    let n_cells = dims.len() as u64;
    let n_days: i64 = 3650;
    let days: Vec<DayKey> = (0..n_days).collect();

    // Deterministic LCG so the benchmark is reproducible.
    let mut x = 42u64;
    let mut next = |m: u64| {
        x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
        (x >> 33) % m
    };

    let t0 = Instant::now();
    let mut alerted: BTreeSet<(usize, DayKey)> = BTreeSet::new();
    while alerted.len() < 1_000_000 {
        alerted.insert((next(n_cells) as usize, next(n_days as u64) as DayKey));
    }
    let events: Vec<(usize, DayKey)> = (0..5_000)
        .map(|_| (next(n_cells) as usize, next(n_days as u64) as DayKey))
        .collect();
    println!(
        "setup: {} cells × {} days, {} alerted pairs, {} events ({:.1?})",
        n_cells,
        n_days,
        alerted.len(),
        events.len(),
        t0.elapsed()
    );

    for (radius, tol) in [(1usize, 1u32), (2, 3)] {
        let t = Instant::now();
        let c = spatial_daily_contingency(dims, &days, &alerted, &events, radius, tol);
        println!(
            "radius {radius}, ±{tol} d: {:?} in {:.2?}  (POD {:.3}, base units {})",
            (c.hits, c.misses, c.false_alarms),
            t.elapsed(),
            c.pod().unwrap_or(f64::NAN),
            c.n(),
        );
    }
}
