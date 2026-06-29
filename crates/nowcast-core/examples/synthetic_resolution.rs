//! Controlled resolution experiment — the positive counterpart to the real-data
//! null in `backtest_distributed`.
//!
//! Run with: `cargo run --release --example synthetic_resolution`
//!
//! Every real experiment in the paper validates against a month-dated, incomplete
//! inventory, so a null (AUC ~ 0.5) is ambiguous: is the method skill-less, or is
//! the *target* untestable? This experiment removes that ambiguity with a planted
//! ground truth. We synthesise one half-hourly rainfall field in which the only
//! thing that distinguishes an "event" cell-day from a "wet non-event" cell-day is
//! the **sub-daily intensity profile**, not the daily total:
//!
//! - event cell-days deliver their water as a short, intense convective burst
//!   (≈1 h) that crosses the I–D curve;
//! - confounder cell-days deliver a *similar or larger daily total* spread over
//!   many hours, so the burst is absent and the curve is never crossed;
//! - the same active cells host both kinds of day, so the static susceptibility
//!   carries no information that separates events from confounders.
//!
//! We then aggregate the identical field to progressively coarser resolution
//! (0.5 h → 24 h), holding the I–D engine and the 24 h maximum duration fixed, and
//! score discrimination (ROC-AUC, POD@area) against the planted events at each
//! resolution. The expected, and demonstrated, result is an AUC that is high at
//! sub-hourly resolution and collapses to ~0.5 at daily resolution: the engine
//! *can* discriminate when the signal is resolved, and daily aggregation
//! (intensity capped at total/24 h) destroys it. This is the mechanism behind the
//! real-data null, shown on a target where the truth is known.

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};

// --- experiment configuration ------------------------------------------------
const NCOLS: usize = 20;
const NROWS: usize = 20;
const N_DAYS: usize = 60;
const STEPS_PER_DAY: usize = 48; // half-hourly
const DT_SUB_H: f64 = 0.5;

const A_INTERCEPT: f64 = 4.0; // regional-style I–D intercept (mm/h at 1 h)
const B_EXPONENT: f64 = 0.39;
const MAX_DURATION_H: f64 = 24.0; // same physical max I–D duration at every resolution

// rainfall calendar (per active cell, per day) — probabilities and amounts
const P_ACTIVE_CELL: f64 = 0.55; // fraction of cells that ever rain
const P_EVENT_DAY: f64 = 0.05; // of an active cell's days, fraction that are bursts
const P_WET_DAY: f64 = 0.45; // of an active cell's days, fraction that are spread-rain
const EVENT_TOTAL: (f64, f64) = (16.0, 26.0); // mm, delivered in ~1 h (modest total)
// Confounders are *wetter* in daily total but smeared over the whole day, so their
// intensity stays low: at daily resolution they outrank the bursts (total is higher),
// which is exactly why a daily product cannot pick the events out.
const WET_TOTAL: (f64, f64) = (16.0, 46.0); // mm, delivered over many hours
const WET_SPREAD_H: (f64, f64) = (16.0, 23.0); // hours over which a wet day is smeared
const DRIZZLE_MM: f64 = 0.15; // per-step background noise ceiling

const SWEEP_K: [usize; 6] = [1, 2, 6, 12, 24, 48]; // aggregation factors (× 0.5 h)

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

/// Deterministic LCG (Numerical Recipes constants) → reproducible without `rand`.
struct Lcg(u64);
impl Lcg {
    fn next_u32(&mut self) -> u32 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 32) as u32
    }
    /// Uniform in [0, 1).
    fn unit(&mut self) -> f64 {
        self.next_u32() as f64 / (u32::MAX as f64 + 1.0)
    }
    /// Uniform in [lo, hi).
    fn range(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (hi - lo) * self.unit()
    }
}

/// A smooth-ish susceptibility field in [0.15, 0.9]: a couple of Gaussian bumps so
/// the terrain is structured (not flat) but uncorrelated with which days are events.
fn build_susceptibility(dims: GridDims, rng: &mut Lcg) -> SusceptibilityMap {
    let bumps: Vec<(f64, f64, f64, f64)> = (0..3)
        .map(|_| {
            (
                rng.range(0.0, NCOLS as f64),
                rng.range(0.0, NROWS as f64),
                rng.range(4.0, 8.0),  // width
                rng.range(0.4, 0.7),  // peak height
            )
        })
        .collect();
    let mut v = vec![0.15_f64; dims.len()];
    for row in 0..NROWS {
        for col in 0..NCOLS {
            let mut s = 0.15;
            for &(bx, by, w, h) in &bumps {
                let d2 = (col as f64 - bx).powi(2) + (row as f64 - by).powi(2);
                s += h * (-d2 / (2.0 * w * w)).exp();
            }
            v[dims.index(col, row)] = s.clamp(0.15, 0.9);
        }
    }
    SusceptibilityMap::new(dims, v).unwrap()
}

/// Build the half-hourly rainfall field and the set of planted event `(cell, day)`.
fn build_rainfall(dims: GridDims, rng: &mut Lcg) -> (Vec<f64>, BTreeSet<(usize, usize)>) {
    let n = dims.len();
    let n_steps = N_DAYS * STEPS_PER_DAY;
    let mut depths = vec![0.0_f64; n_steps * n];
    let mut events: BTreeSet<(usize, usize)> = BTreeSet::new();

    let active: Vec<bool> = (0..n).map(|_| rng.unit() < P_ACTIVE_CELL).collect();

    for cell in 0..n {
        for day in 0..N_DAYS {
            // light background noise on every cell-day
            for s in 0..STEPS_PER_DAY {
                let step = day * STEPS_PER_DAY + s;
                depths[step * n + cell] += rng.range(0.0, DRIZZLE_MM);
            }
            if !active[cell] {
                continue;
            }
            let roll = rng.unit();
            if roll < P_EVENT_DAY {
                // EVENT: short intense burst (~1 h = 2 half-hour steps), random hour
                let total = rng.range(EVENT_TOTAL.0, EVENT_TOTAL.1);
                let start_h = rng.range(2.0, 21.0);
                let s0 = day * STEPS_PER_DAY + (start_h / DT_SUB_H) as usize;
                depths[s0 * n + cell] += total * 0.55;
                depths[(s0 + 1) * n + cell] += total * 0.45;
                events.insert((cell, day));
            } else if roll < P_EVENT_DAY + P_WET_DAY {
                // CONFOUNDER: similar daily total, smeared over many hours (low intensity)
                let total = rng.range(WET_TOTAL.0, WET_TOTAL.1);
                let spread_h = rng.range(WET_SPREAD_H.0, WET_SPREAD_H.1);
                let n_spread = ((spread_h / DT_SUB_H) as usize).max(1);
                let start_h = rng.range(0.0, (24.0 - spread_h).max(0.5));
                let s0 = day * STEPS_PER_DAY + (start_h / DT_SUB_H) as usize;
                let per = total / n_spread as f64;
                for j in 0..n_spread {
                    let step = s0 + j;
                    if step / STEPS_PER_DAY == day {
                        depths[step * n + cell] += per;
                    }
                }
            }
        }
    }
    (depths, events)
}

/// Aggregate a half-hourly field by factor `k` (sum within each block).
fn aggregate(depths: &[f64], n: usize, k: usize) -> Vec<f64> {
    if k == 1 {
        return depths.to_vec();
    }
    let n_steps = depths.len() / n;
    let n_out = n_steps / k;
    let mut out = vec![0.0_f64; n_out * n];
    for b in 0..n_out {
        for j in 0..k {
            let s = b * k + j;
            for c in 0..n {
                out[b * n + c] += depths[s * n + c];
            }
        }
    }
    out
}

/// Per-`(cell, day)` maximum hazard at a given resolution.
fn day_max_hazard(
    dims: GridDims,
    susc: &SusceptibilityMap,
    depths: &[f64],
    dt_h: f64,
) -> Vec<f64> {
    let n = dims.len();
    let steps_per_day = (24.0 / dt_h).round() as usize;
    let max_window = ((MAX_DURATION_H / dt_h).round() as usize).max(1);
    let forcing = GriddedRain::new(dims, dt_h, depths.to_vec()).unwrap();
    let nowcast = Nowcast::new(
        susc.clone(),
        forcing,
        IdThreshold::new(A_INTERCEPT, B_EXPONENT).unwrap(),
        TriggerModel::default(),
        max_window,
    )
    .unwrap();
    let mut day_max = vec![0.0_f64; N_DAYS * n];
    for (step, field) in nowcast.run().into_iter().enumerate() {
        let day = step / steps_per_day;
        if day >= N_DAYS {
            break;
        }
        for (cell, &p) in field.probability().iter().enumerate() {
            let idx = day * n + cell;
            if p > day_max[idx] {
                day_max[idx] = p;
            }
        }
    }
    day_max
}

/// ROC-AUC (Mann–Whitney U with average ranks for ties).
fn auc(scores: &[f64], positive: &BTreeSet<usize>) -> f64 {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| scores[a].partial_cmp(&scores[b]).unwrap());
    let mut rank = vec![0.0_f64; scores.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i;
        while j + 1 < order.len() && scores[order[j + 1]] == scores[order[i]] {
            j += 1;
        }
        let avg = (i + j) as f64 / 2.0 + 1.0;
        for &idx in &order[i..=j] {
            rank[idx] = avg;
        }
        i = j + 1;
    }
    let n_pos = positive.len();
    let n_neg = scores.len() - n_pos;
    if n_pos == 0 || n_neg == 0 {
        return f64::NAN;
    }
    let sum_pos: f64 = positive.iter().map(|&idx| rank[idx]).sum();
    (sum_pos - n_pos as f64 * (n_pos as f64 + 1.0) / 2.0) / (n_pos as f64 * n_neg as f64)
}

/// POD when the top `frac` of cell-days (by score) are alerted.
fn pod_at_area(scores: &[f64], positive: &BTreeSet<usize>, frac: f64) -> f64 {
    let mut sorted = scores.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let thr = sorted[((sorted.len() as f64) * (1.0 - frac)) as usize];
    let (mut hits, total) = (0u32, positive.len() as u32);
    for &idx in positive {
        if scores[idx] >= thr {
            hits += 1;
        }
    }
    hits as f64 / total as f64
}

/// Fraction of cell-days whose hazard crosses the alert level (trigger factor > 0.5,
/// i.e. the I–D curve was actually crossed somewhere in the day).
fn trigger_rate(scores: &[f64], susc: &SusceptibilityMap, n: usize) -> f64 {
    // hazard = susc × trigger_factor; trigger_factor > 0.5 ⇔ hazard > 0.5 × susc.
    let mut fired = 0u32;
    for (idx, &h) in scores.iter().enumerate() {
        let cell = idx % n;
        if h > 0.5 * susc.get(cell) {
            fired += 1;
        }
    }
    fired as f64 / scores.len() as f64
}

const N_SEEDS: usize = 20; // independent realisations → mean ± sd, not one draw

/// Per-resolution row: (dt_h, AUC susc=1, AUC ×susc, POD@5%, trigger rate).
type ResRow = (f64, f64, f64, f64, f64);

/// Per-resolution metrics for one realisation (one random seed).
fn metrics_for_seed(dims: GridDims, seed: u64) -> (Vec<ResRow>, usize) {
    let n = dims.len();
    let mut rng = Lcg(seed);
    let susc = build_susceptibility(dims, &mut rng);
    let (depths_sub, events) = build_rainfall(dims, &mut rng);
    let positive: BTreeSet<usize> = events.iter().map(|&(c, d)| d * n + c).collect();
    let flat = SusceptibilityMap::uniform(dims, 1.0).unwrap();

    let mut rows = Vec::new();
    for &k in &SWEEP_K {
        let dt_h = DT_SUB_H * k as f64;
        let agg = aggregate(&depths_sub, n, k);
        let haz_flat = day_max_hazard(dims, &flat, &agg, dt_h);
        let haz_susc = day_max_hazard(dims, &susc, &agg, dt_h);
        rows.push((
            dt_h,
            auc(&haz_flat, &positive),
            auc(&haz_susc, &positive),
            pod_at_area(&haz_susc, &positive, 0.05),
            trigger_rate(&haz_flat, &flat, n),
        ));
    }
    (rows, positive.len())
}

fn mean_sd(xs: &[f64]) -> (f64, f64) {
    let m = xs.iter().sum::<f64>() / xs.len() as f64;
    let var = xs.iter().map(|x| (x - m).powi(2)).sum::<f64>() / xs.len() as f64;
    (m, var.sqrt())
}

fn main() {
    let dims = GridDims::new(NCOLS, NROWS);
    let n = dims.len();
    let nk = SWEEP_K.len();

    // Accumulate each metric across seeds, per resolution.
    let dt_list: Vec<f64> = SWEEP_K.iter().map(|&k| DT_SUB_H * k as f64).collect();
    let (mut afv, mut asv, mut podv, mut trigv) =
        (vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk]);
    let mut pos_counts = Vec::with_capacity(N_SEEDS);
    for s in 0..N_SEEDS {
        // Distinct, deterministic seed per realisation (golden-ratio + FNV stride).
        let seed = 0x9E3779B97F4A7C15u64.wrapping_add((s as u64).wrapping_mul(0x100000001B3));
        let (rows, n_pos) = metrics_for_seed(dims, seed);
        pos_counts.push(n_pos);
        for (i, &(_, af, as_, pod, trig)) in rows.iter().enumerate() {
            afv[i].push(af);
            asv[i].push(as_);
            podv[i].push(pod);
            trigv[i].push(trig);
        }
    }
    let (pos_mean, _) = mean_sd(&pos_counts.iter().map(|&c| c as f64).collect::<Vec<_>>());

    println!(
        "Synthetic resolution experiment — {NCOLS}×{NROWS} grid ({n} cells), {N_DAYS} days, {N_SEEDS} seeds\n\
         ~{:.0} planted event cell-days/seed ({:.2}% positives), I–D a={A_INTERCEPT}, b={B_EXPONENT}, max duration {MAX_DURATION_H} h\n\
         Same field per seed, aggregated to coarser resolution. Discrimination (mean ± sd over seeds):\n",
        pos_mean,
        100.0 * pos_mean / (N_DAYS * n) as f64,
    );
    println!(
        "{:>8} | {:>16} | {:>16} | {:>14} | {:>12}",
        "dt (h)", "AUC (susc=1)", "AUC (×susc)", "POD@5%", "trig %"
    );
    println!("{}", "-".repeat(78));
    for i in 0..nk {
        let (af_m, af_s) = mean_sd(&afv[i]);
        let (as_m, as_s) = mean_sd(&asv[i]);
        let (pod_m, pod_s) = mean_sd(&podv[i]);
        let (tr_m, tr_s) = mean_sd(&trigv[i]);
        println!(
            "{:>8.1} | {af_m:>6.3} ± {af_s:<6.3} | {as_m:>6.3} ± {as_s:<6.3} | {:>5.0} ± {:<4.0}% | {:>4.1} ± {:<4.1}%",
            dt_list[i],
            100.0 * pod_m,
            100.0 * pod_s,
            100.0 * tr_m,
            100.0 * tr_s,
        );
    }

    println!(
        "\nReading the result:\n  \
         At fine resolution the I–D engine separates the planted bursts from same-total\n  \
         spread-rain confounders (AUC ≈ 1). As the identical field is aggregated, the maximum\n  \
         resolvable intensity falls toward total/24 h, the bursts smear below the curve, the\n  \
         trigger rate drops, and the operational catch rate (POD@5%) collapses toward zero at\n  \
         daily resolution — discrimination is lost to resolution, with model and terrain fixed.\n  \
         The experiment illustrates the aggregation mechanism on a target with known ground\n  \
         truth; it is not a claim about real-event rainfall (see the field backtests)."
    );

    // Dump mean ± sd resolution curve for the paper figure.
    let mut out =
        String::from("dt_h,auc_susc1_mean,auc_susc1_sd,auc_realsusc_mean,auc_realsusc_sd,pod_mean,pod_sd,trig_mean,trig_sd\n");
    for i in 0..nk {
        let (af_m, af_s) = mean_sd(&afv[i]);
        let (as_m, as_s) = mean_sd(&asv[i]);
        let (pod_m, pod_s) = mean_sd(&podv[i]);
        let (tr_m, tr_s) = mean_sd(&trigv[i]);
        out.push_str(&format!(
            "{:.2},{af_m:.4},{af_s:.4},{as_m:.4},{as_s:.4},{pod_m:.4},{pod_s:.4},{tr_m:.4},{tr_s:.4}\n",
            dt_list[i]
        ));
    }
    let path = data_dir().join("synthetic_resolution.csv");
    std::fs::write(&path, out).unwrap();
    eprintln!("wrote resolution curve ({N_SEEDS} seeds) to {}", path.display());
}
