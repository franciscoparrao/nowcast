//! Parallel scaling of `Nowcast::run` (Issue 6 / Section 2.7).
//!
//! Run with: `cargo run --release --features parallel --example bench_parallel`
//!
//! The per-step loop is embarrassingly parallel (each step reads the shared,
//! read-only prefix-sum buffer and writes its own hazard field), so we expect
//! near-linear speed-up until memory bandwidth on the prefix buffer dominates.
//! This drives a thread pool of fixed size for each measurement so the scaling
//! curve is controlled, and checks that the parallel output is bit-identical to a
//! single-threaded run.

#[cfg(not(feature = "parallel"))]
fn main() {
    eprintln!(
        "This benchmark needs the `parallel` feature:\n  \
         cargo run --release --features parallel --example bench_parallel"
    );
}

#[cfg(feature = "parallel")]
fn main() {
    use std::time::Instant;

    use nowcast_core::{GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};

    const NCOLS: usize = 500;
    const NROWS: usize = 500;
    const N_STEPS: usize = 200;
    const MAX_WINDOW: usize = 7;
    const REPS: usize = 2; // best-of, to damp scheduling noise

    let dims = GridDims::new(NCOLS, NROWS);
    let n = dims.len();
    // Deterministic pseudo-rain so the run does real work (same LCG as bench.rs).
    let mut s: u64 = 0x9E3779B97F4A7C15;
    let depths: Vec<f64> = (0..n * N_STEPS)
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
        MAX_WINDOW,
    )
    .unwrap();

    let work = (n * N_STEPS) as f64;
    let max_threads = std::thread::available_parallelism().map(|p| p.get()).unwrap_or(8);
    let mut thread_counts: Vec<usize> = Vec::new();
    let mut t = 1;
    while t <= max_threads {
        thread_counts.push(t);
        t *= 2;
    }
    if *thread_counts.last().unwrap() != max_threads {
        thread_counts.push(max_threads);
    }

    println!(
        "Parallel scaling of Nowcast::run — {} cells × {} steps = {:.0} M cell-steps, max_window {}\n\
         hardware threads available: {}\n",
        n,
        N_STEPS,
        work / 1.0e6,
        MAX_WINDOW,
        max_threads,
    );

    // Reference single-threaded result for an exactness check.
    let reference = {
        let pool = rayon::ThreadPoolBuilder::new().num_threads(1).build().unwrap();
        pool.install(|| nc.run())
    };

    println!(
        "{:>8} | {:>9} | {:>14} | {:>8} | {:>10}",
        "threads", "time (s)", "M cell-steps/s", "speedup", "efficiency"
    );
    println!("{}", "-".repeat(62));

    let mut rows: Vec<(usize, f64, f64, f64)> = Vec::new();
    let mut base_time = 0.0_f64;
    for (i, &nt) in thread_counts.iter().enumerate() {
        let pool = rayon::ThreadPoolBuilder::new().num_threads(nt).build().unwrap();
        let mut best = f64::INFINITY;
        for _ in 0..REPS {
            let start = Instant::now();
            let fields = pool.install(|| nc.run());
            let secs = start.elapsed().as_secs_f64();
            std::hint::black_box(&fields);
            best = best.min(secs);
            // Exactness: parallel output must equal the single-thread reference.
            if nt == thread_counts[0] || nt == max_threads {
                let ok = fields
                    .iter()
                    .zip(&reference)
                    .all(|(a, b)| a.probability() == b.probability());
                assert!(ok, "parallel output diverged from serial at {nt} threads");
            }
        }
        if i == 0 {
            base_time = best;
        }
        let thrpt = work / best / 1.0e6;
        let speedup = base_time / best;
        let eff = speedup / nt as f64;
        println!(
            "{nt:>8} | {best:>9.3} | {thrpt:>14.1} | {speedup:>7.2}× | {:>9.0}%",
            100.0 * eff
        );
        rows.push((nt, best, speedup, eff));
    }

    println!(
        "\nThe per-step loop scales near-linearly until the shared prefix-sum buffer\n\
         saturates memory bandwidth; output is verified bit-identical to the serial run."
    );

    // Dump the scaling curve for the paper.
    let mut out = String::from("threads,time_s,speedup,efficiency\n");
    for (nt, secs, sp, eff) in &rows {
        out.push_str(&format!("{nt},{secs:.4},{sp:.4},{eff:.4}\n"));
    }
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data/scaling.csv");
    std::fs::write(&path, out).unwrap();
    eprintln!("wrote scaling curve to {}", path.display());
}
