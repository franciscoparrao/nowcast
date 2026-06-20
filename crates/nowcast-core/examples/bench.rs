//! Computational performance of the I–D nowcast engine vs problem size.
//! Run: `cargo run --release --example bench`. Reports wall-clock and throughput
//! for `Nowcast::run` (all steps, all cells) over synthetic grids.

use std::time::Instant;

use nowcast_core::{GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};

fn bench(ncols: usize, nrows: usize, n_steps: usize, max_window: usize) -> (f64, usize) {
    let dims = GridDims::new(ncols, nrows);
    let n = dims.len();
    // Deterministic pseudo-rain (cheap LCG) so the run does real work.
    let mut s: u64 = 0x9E3779B97F4A7C15;
    let depths: Vec<f64> = (0..n * n_steps)
        .map(|_| {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((s >> 33) as f64 / u32::MAX as f64) * 20.0
        })
        .collect();
    let forcing = GriddedRain::new(dims, 24.0, depths).unwrap();
    let susc = SusceptibilityMap::uniform(dims, 0.5).unwrap();
    let nc = Nowcast::new(
        susc,
        forcing,
        IdThreshold::new(5.5, 0.39).unwrap(),
        TriggerModel::default(),
        max_window,
    )
    .unwrap();
    let t = Instant::now();
    let fields = nc.run();
    let secs = t.elapsed().as_secs_f64();
    std::hint::black_box(&fields);
    (secs, n * n_steps)
}

fn main() {
    println!(
        "I–D Nowcast::run — single thread, max_window = 7\n\n{:>8} {:>7} {:>10} {:>9} {:>14}",
        "cells", "steps", "cell-steps", "time (s)", "Mcell-steps/s"
    );
    let cfgs = [
        (100, 100, 60),
        (200, 200, 120),
        (300, 300, 200),
        (500, 500, 200),
        (700, 700, 365),
    ];
    for (c, r, steps) in cfgs {
        let (secs, work) = bench(c, r, steps, 7);
        println!(
            "{:>8} {:>7} {:>10} {:>9.3} {:>14.1}",
            c * r,
            steps,
            work,
            secs,
            work as f64 / secs / 1.0e6
        );
    }
    println!(
        "\nComplexity: O(cells × steps × max_window). Per-cell prefix sums make each\n\
         rolling-window sum O(1); memory is O(cells × steps) for the prefix buffer."
    );
}
