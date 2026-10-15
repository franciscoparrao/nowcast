//! Controlled resolution experiment — the positive counterpart to the real-data
//! null in `backtest_distributed`.
//!
//! Run with: `cargo run --release --example synthetic_resolution`
//!
//! Every real experiment in the paper validates against a month-dated, incomplete
//! inventory, so a null (AUC ~ 0.5) is ambiguous: is the method skill-less, or is
//! the *target* untestable? This experiment removes that ambiguity with a known
//! ground truth.
//!
//! **The labels do not come from the rule under test.** An earlier version defined
//! an "event" as a burst that crosses the I–D curve, which makes an I–D trigger
//! recover it by construction — a deductive test, not evidence. Here the rainfall
//! is synthesised with two day types (a ≈1 h burst, or a similar-to-larger total
//! smeared over many hours), but whether a cell-day *fails* is decided by an
//! independent transient pore-pressure store (see `soil_failures`), integrated at
//! native half-hourly resolution. The two do not coincide: some bursts never fail
//! and many spread-rain days do, and the run prints that audit.
//!
//! We then aggregate the identical field to progressively coarser resolution
//! (0.5 h → 24 h), holding the I–D engine and the 24 h maximum duration fixed, and
//! score against those physical labels, **stratified by which mechanism caused the
//! failure**. That stratification is the result: aggregation wipes out the catch
//! rate on burst-driven failures (~46% → ~0%) while leaving accumulation-driven
//! failures untouched (~42% at every resolution). A pooled score hides this, since
//! it averages the population the aggregation destroys with the one it spares.

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};

#[path = "shared/curves.rs"]
mod curves;

// --- experiment configuration ------------------------------------------------
const NCOLS: usize = 20;
const NROWS: usize = 20;
const N_DAYS: usize = 60;
const STEPS_PER_DAY: usize = 48; // half-hourly
const DT_SUB_H: f64 = 0.5;

// Same calibrated regional curve as the field experiments (one copy; see
// shared/curves.rs). The discrimination scores here are rank-based, so they are
// insensitive to the intercept; it only sets the trigger rate.
const A_INTERCEPT: f64 = curves::REGIONAL.0;
const B_EXPONENT: f64 = curves::REGIONAL.1;
const MAX_DURATION_H: f64 = 24.0; // same physical max I–D duration at every resolution

// rainfall calendar (per active cell, per day) — probabilities and amounts
const P_ACTIVE_CELL: f64 = 0.55; // fraction of cells that ever rain
const P_EVENT_DAY: f64 = 0.05; // of an active cell's days, fraction that are bursts
const P_WET_DAY: f64 = 0.45; // of an active cell's days, fraction that are spread-rain
// Burst totals span a range wide enough that some bursts do NOT fail and some
// spread-rain days DO: the failure label must not be a relabelling of the class.
const EVENT_TOTAL: (f64, f64) = (8.0, 30.0); // mm, delivered in ~1 h
// Confounders are *wetter* in daily total but smeared over many hours, so their
// intensity stays low: at daily resolution they outrank the bursts (total is higher),
// which is exactly why a daily product cannot pick the events out.
const WET_TOTAL: (f64, f64) = (16.0, 46.0); // mm, delivered over many hours
const WET_SPREAD_H: (f64, f64) = (8.0, 23.0); // hours over which a wet day is smeared
const DRIZZLE_MM: f64 = 0.15; // per-step background noise ceiling

// --- ground truth: an independent failure mechanism -------------------------
// The labels must NOT come from the decision rule under test, or the experiment
// is deductive: an event defined as "crosses the I–D curve" is trivially
// recovered by an I–D trigger. We therefore generate failures with a transient
// pore-pressure analogue — a linear store fed by infiltration (capped, the
// excess runs off) and drained continuously — and fail the cell-day when the
// store exceeds a critical wetness. This is a different functional form from
// I = a·D^-b, so "can an empirical I–D rule recover events produced by this
// mechanism, and how does that degrade under aggregation?" is a real question.
// The store is always integrated at the NATIVE half-hourly step: the physics
// happens at full resolution, and it is the *observation* we degrade.
const DRAIN_PER_H: f64 = 0.25; // store drains with an ~4 h time constant
const INFILT_MAX_MM_H: f64 = 30.0; // infiltration capacity; excess is runoff
const THETA_CRIT_MM: f64 = 12.0; // failure when the store exceeds this

const SWEEP_K: [usize; 6] = [1, 2, 6, 12, 24, 48]; // aggregation factors (× 0.5 h)

/// The knobs a reviewer will ask about, gathered so the `--sweep` mode can move
/// them one at a time. `Cfg::paper()` reproduces the constants above exactly
/// (the default run is bit-identical to the pre-sweep version of this example).
#[derive(Clone, Copy)]
struct Cfg {
    /// Burst totals (mm) delivered in ~1 h.
    event_total: (f64, f64),
    /// Store drainage rate (1/h); the time constant is its inverse.
    drain_per_h: f64,
    /// Failure when the store exceeds this (mm).
    theta_crit: f64,
    /// I–D intercept (mm/h) of the trigger under test.
    a_intercept: f64,
    label: &'static str,
}

impl Cfg {
    const fn paper() -> Cfg {
        Cfg {
            event_total: EVENT_TOTAL,
            drain_per_h: DRAIN_PER_H,
            theta_crit: THETA_CRIT_MM,
            a_intercept: A_INTERCEPT,
            label: "paper (bursts 8–30 mm, τ 4 h, θ 12 mm, a 5.5)",
        }
    }
    /// Daily total at which a burst spread over 24 h reaches the I–D curve:
    /// `I_crit(24 h) · 24 = a · 24^(1-b)`.
    fn daily_ceiling_mm(&self) -> f64 {
        self.a_intercept * 24f64.powf(1.0 - B_EXPONENT)
    }
}

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

/// Build the half-hourly rainfall field and record which `(cell, day)` were
/// generated as bursts — the *class*, kept only to report how far the physical
/// labels depart from it (they are not the labels).
fn build_rainfall(dims: GridDims, rng: &mut Lcg, cfg: &Cfg) -> (Vec<f64>, BTreeSet<(usize, usize)>) {
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
                let total = rng.range(cfg.event_total.0, cfg.event_total.1);
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

/// Ground truth: integrate the pore-pressure store at the native half-hourly
/// step and return the `(cell, day)` pairs in which it exceeds `THETA_CRIT_MM`.
///
/// This is the label generator, and it is deliberately **not** the I--D rule the
/// engine applies. It is intensity-sensitive through drainage (water arriving
/// faster than the store drains accumulates) rather than through a power law,
/// and infiltration capping means very intense rain partly runs off instead of
/// loading the profile — a mechanism an I--D curve does not represent at all.
fn soil_failures(dims: GridDims, depths: &[f64], cfg: &Cfg) -> BTreeSet<(usize, usize)> {
    let n = dims.len();
    let n_steps = depths.len() / n;
    let decay = 1.0 - cfg.drain_per_h * DT_SUB_H;
    let infilt_cap = INFILT_MAX_MM_H * DT_SUB_H;
    let mut theta = vec![0.0_f64; n];
    let mut failures = BTreeSet::new();
    for step in 0..n_steps {
        let day = step / STEPS_PER_DAY;
        for cell in 0..n {
            let infiltrated = depths[step * n + cell].min(infilt_cap);
            theta[cell] = theta[cell] * decay + infiltrated;
            if theta[cell] > cfg.theta_crit {
                failures.insert((cell, day));
            }
        }
    }
    failures
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
    a_intercept: f64,
) -> Vec<f64> {
    let n = dims.len();
    let steps_per_day = (24.0 / dt_h).round() as usize;
    let max_window = ((MAX_DURATION_H / dt_h).round() as usize).max(1);
    let forcing = GriddedRain::new(dims, dt_h, depths.to_vec()).unwrap();
    let nowcast = Nowcast::new(
        susc.clone(),
        forcing,
        IdThreshold::new(a_intercept, B_EXPONENT).unwrap(),
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

/// Per-resolution row: (dt_h, AUC susc=1, AUC ×susc, POD@5%, trigger rate,
/// POD@5% on burst-driven failures, POD@5% on accumulation-driven failures).
///
/// The last two are the point of the experiment: the labels now come from an
/// independent mechanism that fails cell-days by BOTH routes, so aggregation can
/// be shown to destroy one and spare the other, instead of being scored against a
/// population built to be burst-only.
type ResRow = (f64, f64, f64, f64, f64, f64, f64);

/// How far the physical labels depart from the generating class — reported so the
/// reader can check the labels are not a relabelling of "was a burst".
struct LabelAudit {
    n_positive: usize,
    /// Bursts that did NOT fail.
    bursts_missed: usize,
    n_bursts: usize,
    /// Failures on days that were NOT generated as bursts.
    non_burst_failures: usize,
}

/// Per-resolution metrics for one realisation (one random seed).
fn metrics_for_seed(dims: GridDims, seed: u64, cfg: &Cfg) -> (Vec<ResRow>, LabelAudit) {
    let n = dims.len();
    let mut rng = Lcg(seed);
    let susc = build_susceptibility(dims, &mut rng);
    let (depths_sub, bursts) = build_rainfall(dims, &mut rng, cfg);
    // Ground truth from the independent mechanism, at native resolution.
    let failures = soil_failures(dims, &depths_sub, cfg);
    let audit = LabelAudit {
        n_positive: failures.len(),
        bursts_missed: bursts.difference(&failures).count(),
        n_bursts: bursts.len(),
        non_burst_failures: failures.difference(&bursts).count(),
    };
    let positive: BTreeSet<usize> = failures.iter().map(|&(c, d)| d * n + c).collect();
    // Stratify the positives by which mechanism produced them.
    let pos_burst: BTreeSet<usize> =
        failures.intersection(&bursts).map(|&(c, d)| d * n + c).collect();
    let pos_accum: BTreeSet<usize> =
        failures.difference(&bursts).map(|&(c, d)| d * n + c).collect();
    let flat = SusceptibilityMap::uniform(dims, 1.0).unwrap();

    let mut rows = Vec::new();
    for &k in &SWEEP_K {
        let dt_h = DT_SUB_H * k as f64;
        let agg = aggregate(&depths_sub, n, k);
        let haz_flat = day_max_hazard(dims, &flat, &agg, dt_h, cfg.a_intercept);
        let haz_susc = day_max_hazard(dims, &susc, &agg, dt_h, cfg.a_intercept);
        rows.push((
            dt_h,
            auc(&haz_flat, &positive),
            auc(&haz_susc, &positive),
            pod_at_area(&haz_susc, &positive, 0.05),
            trigger_rate(&haz_flat, &flat, n),
            pod_at_area(&haz_susc, &pos_burst, 0.05),
            pod_at_area(&haz_susc, &pos_accum, 0.05),
        ));
    }
    (rows, audit)
}

fn mean_sd(xs: &[f64]) -> (f64, f64) {
    let m = xs.iter().sum::<f64>() / xs.len() as f64;
    let var = xs.iter().map(|x| (x - m).powi(2)).sum::<f64>() / xs.len() as f64;
    (m, var.sqrt())
}

fn seed_for(s: usize) -> u64 {
    // Distinct, deterministic seed per realisation (golden-ratio + FNV stride).
    0x9E3779B97F4A7C15u64.wrapping_add((s as u64).wrapping_mul(0x100000001B3))
}

/// `--sweep`: move the design knobs one at a time from the paper configuration
/// and report where the burst-driven collapse appears and disappears. The
/// paper's collapse (0.46 → 0.00) is entailed by its burst range sitting below
/// the daily ceiling of the a*=5.5 curve (38 mm/day); this mode shows what
/// happens when bursts straddle or exceed that ceiling, when the store drains
/// over days instead of hours, when the failure wetness changes, and under the
/// global Caine intercept. Fewer seeds (5) than the headline run: this is a
/// map, not a measurement.
fn sweep(dims: GridDims) {
    const SEEDS: usize = 5;
    let base = Cfg::paper();
    let configs: Vec<Cfg> = vec![
        base,
        Cfg { event_total: (30.0, 60.0), label: "bursts 30–60 mm (straddle the 38 mm daily ceiling)", ..base },
        Cfg { event_total: (60.0, 120.0), label: "bursts 60–120 mm (all above the daily ceiling)", ..base },
        Cfg { drain_per_h: 1.0, label: "τ 1 h (store drains within the burst)", ..base },
        Cfg { drain_per_h: 1.0 / 24.0, label: "τ 24 h (store remembers a day)", ..base },
        Cfg { drain_per_h: 1.0 / 120.0, label: "τ 120 h (antecedent-controlled failures)", ..base },
        Cfg { theta_crit: 6.0, label: "θ_crit 6 mm (fails easily)", ..base },
        Cfg { theta_crit: 24.0, label: "θ_crit 24 mm (fails rarely)", ..base },
        Cfg { a_intercept: curves::PUBLISHED[0].0, label: "a 14.82 (Caine global)", ..base },
        Cfg { event_total: (30.0, 60.0), drain_per_h: 1.0 / 24.0, label: "bursts 30–60 mm + τ 24 h", ..base },
        Cfg { event_total: (60.0, 120.0), a_intercept: curves::PUBLISHED[0].0, label: "bursts 60–120 mm + a 14.82", ..base },
    ];
    let nk = SWEEP_K.len();
    let dt_list: Vec<f64> = SWEEP_K.iter().map(|&k| DT_SUB_H * k as f64).collect();
    println!(
        "Synthetic sweep — {SEEDS} seeds per configuration; POD@5% on burst-driven | accumulation-driven failures
         at dt = 0.5, 3, 6, 12, 24 h; 'half' = coarsest dt at which burst POD is still ≥ half its 0.5 h value;
         'fail' = share of generated bursts that the store fails; 'non-burst' = failures on non-burst days (per seed).
"
    );
    println!("{:<56} | {:>6} | {:>24} | {:>12} | {:>5} | {:>5} | {:>9}",
        "configuration", "ceil", "burst POD 0.5/3/6/12/24h", "accum 0.5/24", "half", "fail", "non-burst");
    println!("{}", "-".repeat(140));
    let mut csv = String::from("config,daily_ceiling_mm,dt_h,pod_burst,pod_accum,auc_susc,trig,burst_fail_share,non_burst_failures
");
    for cfg in &configs {
        let (mut pb, mut pa, mut au, mut tr) = (vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk]);
        let (mut fail_share, mut nonburst) = (Vec::new(), Vec::new());
        for sd in 0..SEEDS {
            let (rows, audit) = metrics_for_seed(dims, seed_for(sd), cfg);
            fail_share.push(1.0 - audit.bursts_missed as f64 / audit.n_bursts.max(1) as f64);
            nonburst.push(audit.non_burst_failures as f64);
            for (i, &(_, _, as_, _, trig, b, a)) in rows.iter().enumerate() {
                pb[i].push(b); pa[i].push(a); au[i].push(as_); tr[i].push(trig);
            }
        }
        let pbm: Vec<f64> = pb.iter().map(|v| mean_sd(v).0).collect();
        let pam: Vec<f64> = pa.iter().map(|v| mean_sd(v).0).collect();
        let half_dt = dt_list.iter().zip(pbm.iter()).filter(|&(_, &p)| p >= 0.5 * pbm[0]).map(|(&d, _)| d).fold(0.0, f64::max);
        let (fs, _) = mean_sd(&fail_share);
        let (nb, _) = mean_sd(&nonburst);
        let pick = |i: usize| 100.0 * pbm[i];
        println!("{:<56} | {:>4.0}mm | {:>3.0}/{:>3.0}/{:>3.0}/{:>3.0}/{:>3.0}% | {:>4.0}/{:>4.0}% | {:>4.1}h | {:>4.0}% | {:>9.0}",
            cfg.label, cfg.daily_ceiling_mm(), pick(0), pick(2), pick(3), pick(4), pick(5),
            100.0 * pam[0], 100.0 * pam[5], half_dt, 100.0 * fs, nb);
        for i in 0..nk {
            csv.push_str(&format!("\"{}\",{:.1},{:.2},{:.4},{:.4},{:.4},{:.4},{:.4},{:.1}\n",
                cfg.label, cfg.daily_ceiling_mm(), dt_list[i], pbm[i], pam[i], mean_sd(&au[i]).0, mean_sd(&tr[i]).0, fs, nb));
        }
    }
    let path = data_dir().join("synthetic_sweep.csv");
    std::fs::write(&path, csv).unwrap();
    eprintln!("wrote sweep ({SEEDS} seeds × {} configurations) to {}", configs.len(), path.display());
}

fn main() {
    let dims = GridDims::new(NCOLS, NROWS);
    if std::env::args().any(|a| a == "--sweep") {
        sweep(dims);
        return;
    }
    let cfg = Cfg::paper();
    let n = dims.len();
    let nk = SWEEP_K.len();

    // Accumulate each metric across seeds, per resolution.
    let dt_list: Vec<f64> = SWEEP_K.iter().map(|&k| DT_SUB_H * k as f64).collect();
    let (mut afv, mut asv, mut podv, mut trigv) =
        (vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk], vec![vec![]; nk]);
    let (mut podb, mut poda) = (vec![vec![]; nk], vec![vec![]; nk]);
    let mut pos_counts = Vec::with_capacity(N_SEEDS);
    let (mut missed_v, mut burst_v, mut nonburst_v) = (Vec::new(), Vec::new(), Vec::new());
    for s in 0..N_SEEDS {
        let (rows, audit) = metrics_for_seed(dims, seed_for(s), &cfg);
        pos_counts.push(audit.n_positive);
        missed_v.push(audit.bursts_missed as f64);
        burst_v.push(audit.n_bursts as f64);
        nonburst_v.push(audit.non_burst_failures as f64);
        for (i, &(_, af, as_, pod, trig, pb, pa)) in rows.iter().enumerate() {
            afv[i].push(af);
            asv[i].push(as_);
            podv[i].push(pod);
            trigv[i].push(trig);
            podb[i].push(pb);
            poda[i].push(pa);
        }
    }
    let (pos_mean, _) = mean_sd(&pos_counts.iter().map(|&c| c as f64).collect::<Vec<_>>());

    let (missed_m, _) = mean_sd(&missed_v);
    let (burst_m, _) = mean_sd(&burst_v);
    let (nonburst_m, _) = mean_sd(&nonburst_v);
    println!(
        "Synthetic resolution experiment — {NCOLS}×{NROWS} grid ({n} cells), {N_DAYS} days, {N_SEEDS} seeds\n\
         ~{:.0} failing cell-days/seed ({:.2}% positives), I–D a={}, b={B_EXPONENT}, max duration {MAX_DURATION_H} h\n\
         Labels come from an INDEPENDENT mechanism (pore-pressure store, τ={:.0} h, θ_crit={} mm),\n\
         not from the I–D rule under test. Label audit per seed: {:.0} of {:.0} bursts do NOT fail,\n\
         and {:.0} failures happen on days that were not bursts — the label is not the class.\n\
         Same field per seed, aggregated to coarser resolution. Discrimination (mean ± sd over seeds):\n",
        pos_mean,
        100.0 * pos_mean / (N_DAYS * n) as f64,
        cfg.a_intercept,
        1.0 / cfg.drain_per_h,
        cfg.theta_crit,
        missed_m,
        burst_m,
        nonburst_m,
    );
    println!(
        "{:>8} | {:>16} | {:>16} | {:>14} | {:>12} || {:>13} | {:>13}",
        "dt (h)", "AUC (susc=1)", "AUC (×susc)", "POD@5%", "trig %", "POD burst", "POD accum"
    );
    println!("{}", "-".repeat(112));
    for i in 0..nk {
        let (af_m, af_s) = mean_sd(&afv[i]);
        let (as_m, as_s) = mean_sd(&asv[i]);
        let (pod_m, pod_s) = mean_sd(&podv[i]);
        let (tr_m, tr_s) = mean_sd(&trigv[i]);
        let (pb_m, pb_s) = mean_sd(&podb[i]);
        let (pa_m, pa_s) = mean_sd(&poda[i]);
        println!(
            "{:>8.1} | {af_m:>6.3} ± {af_s:<6.3} | {as_m:>6.3} ± {as_s:<6.3} | {:>5.0} ± {:<4.0}% | {:>4.1} ± {:<4.1}% || {:>4.0} ± {:<4.0}% | {:>4.0} ± {:<4.0}%",
            dt_list[i],
            100.0 * pod_m,
            100.0 * pod_s,
            100.0 * tr_m,
            100.0 * tr_s,
            100.0 * pb_m,
            100.0 * pb_s,
            100.0 * pa_m,
            100.0 * pa_s,
        );
    }

    println!(
        "\nReading the result:\n  \
         The labels come from a pore-pressure store, not from the I–D rule, so this is a real\n  \
         test rather than a restatement of the decision rule. That store fails cell-days by TWO\n  \
         routes — a short burst outrunning drainage, and slower rain accumulating past θ_crit —\n  \
         and aggregation does NOT treat them alike. Stratified by mechanism, the operational\n  \
         catch rate on BURST-driven failures collapses from ~46% at half-hourly to ~0% at daily,\n  \
         while ACCUMULATION-driven failures are caught at ~42% at every resolution. The pooled\n  \
         AUC decays only mildly (0.99 → 0.92) precisely BECAUSE it averages a population the\n  \
         aggregation destroys with one it leaves untouched — reporting the pooled number alone\n  \
         would hide the effect. Daily forcing is not uniformly blind: it is blind to the\n  \
         sub-daily triggering route, which is the one an I–D nowcast exists to catch.\n  \
         This isolates the aggregation mechanism on a target with known ground truth; it is not\n  \
         a claim about real-event base rates (see the field backtests)."
    );

    // Dump mean ± sd resolution curve for the paper figure.
    let mut out = String::from(
        "dt_h,auc_susc1_mean,auc_susc1_sd,auc_realsusc_mean,auc_realsusc_sd,pod_mean,pod_sd,\
         trig_mean,trig_sd,pod_burst_mean,pod_burst_sd,pod_accum_mean,pod_accum_sd\n",
    );
    for i in 0..nk {
        let (af_m, af_s) = mean_sd(&afv[i]);
        let (as_m, as_s) = mean_sd(&asv[i]);
        let (pod_m, pod_s) = mean_sd(&podv[i]);
        let (tr_m, tr_s) = mean_sd(&trigv[i]);
        let (pb_m, pb_s) = mean_sd(&podb[i]);
        let (pa_m, pa_s) = mean_sd(&poda[i]);
        out.push_str(&format!(
            "{:.2},{af_m:.4},{af_s:.4},{as_m:.4},{as_s:.4},{pod_m:.4},{pod_s:.4},{tr_m:.4},{tr_s:.4},\
             {pb_m:.4},{pb_s:.4},{pa_m:.4},{pa_s:.4}\n",
            dt_list[i]
        ));
    }
    let path = data_dir().join("synthetic_resolution.csv");
    std::fs::write(&path, out).unwrap();
    eprintln!("wrote resolution curve ({N_SEEDS} seeds) to {}", path.display());
}
