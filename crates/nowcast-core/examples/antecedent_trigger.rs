//! Antecedent-moisture trigger on the Maipo backtests: does a different MODEL do
//! better than the I–D curve on the same daily forcing?
//!
//! Added after the EMS review (2026-10-15): "'not the model' is untested: no
//! alternative model is run on the same data". The engine already ships an
//! `AntecedentTrigger` (antecedent precipitation index, API_t = Σ decay^k · P_{t−k},
//! excluding the current step); this example runs it alone and composed with the
//! I–D trigger as a dual cause–trigger rule (Bogaard & Greco 2018), on
//!
//!   A. the lumped monthly backtest (Section 5.1): the critical API depth is
//!      calibrated by max-CSI at ±1 month for each decay, and every rule is scored
//!      with Wilson intervals and the same year-shuffle null as `backtest.rs`;
//!   B. the distributed daily backtest (Section 5.2): ROC-AUC of the
//!      susceptibility-weighted hazard against the same ±1 cell / ±1 month event
//!      footprint, with a month-block bootstrap interval.
//!
//! Run with: `cargo run --release --example antecedent_trigger`

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{
    AntecedentTrigger, Combine, GridDims, GriddedRain, IdThreshold, IdTrigger, MonthKey,
    MultiNowcast, SusceptibilityMap, Trigger, TriggerModel, UniformRain, roc_auc,
};

#[path = "shared/verif.rs"]
mod verif;
use verif::{Lcg, frac_ge, month_contingency, wilson, year_shuffle};

const A_STAR: f64 = 5.5;
const B: f64 = 0.39;
const MAX_WINDOW_DAYS: usize = 7;
const TOL: i32 = 1;
const DECAYS: [f64; 3] = [0.85, 0.90, 0.95]; // per day: half-life 4.3 / 6.6 / 13.5 d
const N_PERM: usize = 2000;

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

fn read_lumped() -> (Vec<MonthKey>, Vec<f64>, Vec<MonthKey>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_cr2met_pr_1979_2016.csv")).unwrap();
    let (mut months, mut depths) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let (Some(date), Some(p)) = (f.next(), f.next()) else { continue };
        let mut d = date.split('-');
        months.push((d.next().unwrap().parse().unwrap(), d.next().unwrap().parse().unwrap()));
        depths.push(p.trim().parse().unwrap_or(0.0));
    }
    let ev = std::fs::read_to_string(data_dir().join("maipo_events_dated.csv")).unwrap();
    let events = ev
        .lines()
        .skip(1)
        .filter_map(|l| {
            let mut f = l.split(',');
            f.next()?;
            Some((f.next()?.trim().parse().ok()?, f.next()?.trim().parse().ok()?))
        })
        .collect();
    (months, depths, events)
}

/// Per-step (I–D factor, API factor) for a single forcing series.
fn lumped_factors(depths: &[f64], decay: f64, crit: f64) -> Vec<(f64, f64)> {
    let dims = GridDims::new(1, 1);
    let f = UniformRain::new(dims, 24.0, depths.to_vec()).unwrap();
    let id = IdTrigger::new(f.clone(), IdThreshold::new(A_STAR, B).unwrap(), TriggerModel::default(), MAX_WINDOW_DAYS).unwrap();
    let api = AntecedentTrigger::new(f, decay, crit, TriggerModel::default()).unwrap();
    (0..depths.len()).map(|t| (id.factor(0, t), api.factor(0, t))).collect()
}

#[derive(Clone, Copy)]
enum Rule {
    Id,
    Api,
    And,
    Or,
}

fn alert(rule: Rule, (fi, fa): (f64, f64)) -> bool {
    match rule {
        Rule::Id => fi >= 0.5,
        Rule::Api => fa >= 0.5,
        Rule::And => fi >= 0.5 && fa >= 0.5,
        Rule::Or => fi >= 0.5 || fa >= 0.5,
    }
}

struct Scored {
    pod: f64,
    far: f64,
    csi: f64,
    n_ev: u64,
    pod_ci: (f64, f64),
    alert_months: usize,
    p_null: f64,
}

fn score(day_month: &[MonthKey], fac: &[(f64, f64)], rule: Rule, events: &[MonthKey], rng: Option<&mut Lcg>) -> Scored {
    let present: BTreeSet<MonthKey> = day_month.iter().copied().collect();
    let alerted: BTreeSet<MonthKey> =
        day_month.iter().zip(fac).filter_map(|(&m, &f)| alert(rule, f).then_some(m)).collect();
    let observed: BTreeSet<MonthKey> = events.iter().copied().filter(|m| present.contains(m)).collect();
    let c = month_contingency(&present, &alerted, &observed, TOL);
    let csi = c.csi().unwrap_or(0.0);
    let p_null = match rng {
        Some(r) => {
            let (_, csis) = year_shuffle(&present, &alerted, &observed, TOL, N_PERM, r);
            frac_ge(&csis, csi)
        }
        None => f64::NAN,
    };
    Scored {
        pod: c.pod().unwrap_or(0.0),
        far: c.far().unwrap_or(0.0),
        csi,
        n_ev: c.hits + c.misses,
        pod_ci: wilson(c.hits, c.hits + c.misses),
        alert_months: alerted.len(),
        p_null,
    }
}

// ---------------------------------------------------------------- distributed
/// (grid, susceptibility, per-day month keys, step-major depths, (cell, month) events)
type DistData = (GridDims, Vec<f64>, Vec<MonthKey>, Vec<f64>, Vec<(usize, MonthKey)>);

fn read_dist() -> DistData {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_grid.csv")).unwrap();
    let (mut mr, mut mc, mut rows) = (0usize, 0usize, Vec::new());
    for line in text.lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let (cell, r, c, s): (usize, usize, usize, f64) =
            (f[0].parse().unwrap(), f[1].parse().unwrap(), f[2].parse().unwrap(), f[5].parse().unwrap());
        mr = mr.max(r);
        mc = mc.max(c);
        rows.push((cell, s));
    }
    rows.sort_by_key(|x| x.0);
    let dims = GridDims::new(mc + 1, mr + 1);
    let susc = rows.into_iter().map(|x| x.1).collect();
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_pr.csv")).unwrap();
    let (mut months, mut depths) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let mut d = f.next().unwrap().split('-');
        months.push((d.next().unwrap().parse().unwrap(), d.next().unwrap().parse().unwrap()));
        depths.extend(f.map(|v| v.parse::<f64>().unwrap_or(0.0)));
    }
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_events.csv")).unwrap();
    let events = text
        .lines()
        .skip(1)
        .filter_map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            Some((f[3].parse().ok()?, (f[1].parse().ok()?, f[2].parse().ok()?)))
        })
        .collect();
    (dims, susc, months, depths, events)
}

/// Same footprint as `backtest_distributed`: ±1 cell, ±1 month around each event.
fn footprint(dims: GridDims, months: &[MonthKey], events: &[(usize, MonthKey)]) -> BTreeSet<usize> {
    let n = dims.len();
    let mut pos = BTreeSet::new();
    for &(ec, (ey, em)) in events {
        let (er, ecol) = ((ec / dims.ncols) as i64, (ec % dims.ncols) as i64);
        for dr in -1..=1i64 {
            for dc in -1..=1i64 {
                let (nr, nc) = (er + dr, ecol + dc);
                if nr < 0 || nc < 0 || nr >= dims.nrows as i64 || nc >= dims.ncols as i64 {
                    continue;
                }
                for d in -TOL..=TOL {
                    let z = ey * 12 + (em as i32 - 1) + d;
                    let mk = (z.div_euclid(12), (z.rem_euclid(12) + 1) as u32);
                    if let Some(mi) = months.iter().position(|&x| x == mk) {
                        pos.insert(mi * n + nr as usize * dims.ncols + nc as usize);
                    }
                }
            }
        }
    }
    pos
}

fn auc_ci(scores: &[f64], labels: &[bool], n_cells: usize, n_months: usize, rng: &mut Lcg) -> (f64, f64, f64) {
    let point = roc_auc(scores, labels).unwrap().unwrap_or(f64::NAN);
    let mut v = Vec::new();
    for _ in 0..500 {
        let (mut s, mut l) = (Vec::with_capacity(scores.len()), Vec::with_capacity(scores.len()));
        for _ in 0..n_months {
            let m = rng.below(n_months);
            s.extend_from_slice(&scores[m * n_cells..(m + 1) * n_cells]);
            l.extend_from_slice(&labels[m * n_cells..(m + 1) * n_cells]);
        }
        if let Ok(Some(a)) = roc_auc(&s, &l) {
            v.push(a);
        }
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    (point, v[(0.025 * (v.len() - 1) as f64) as usize], v[(0.975 * (v.len() - 1) as f64) as usize])
}

fn main() {
    // ---------------- A. lumped
    let (day_month, depths, events) = read_lumped();
    let crits: Vec<f64> = (1..=40).map(|i| i as f64 * 10.0).collect(); // 10–400 mm
    println!("A. Lumped Maipo backtest (±{TOL} month), I–D at a*={A_STAR}; API critical depth calibrated by max-CSI\n");
    println!("{:<34} | {:>7} | {:>24} | {:>5} | {:>6} | {:>7} | {:>10}", "rule", "crit mm", "POD [Wilson 95%]", "FAR", "CSI", "alert-m", "P(null≥)");
    println!("{}", "-".repeat(110));
    let mut rng = Lcg(20261016);
    let base = lumped_factors(&depths, DECAYS[1], 100.0);
    let s = score(&day_month, &base, Rule::Id, &events, Some(&mut rng));
    println!("{:<34} | {:>7} | {:.2} [{:.2}, {:.2}] (n={:>2}) | {:>5.2} | {:>6.3} | {:>7} | {:>10.3}", "I–D alone (paper)", "-", s.pod, s.pod_ci.0, s.pod_ci.1, s.n_ev, s.far, s.csi, s.alert_months, s.p_null);
    let mut best_api = (0.0, 0.0, 0.0); // (decay, crit, csi)
    for &decay in &DECAYS {
        for (rule, name) in [(Rule::Api, "API alone"), (Rule::And, "I–D AND API (cause×trigger)"), (Rule::Or, "I–D OR API")] {
            let mut best = (f64::NAN, -1.0);
            for &c in &crits {
                let f = lumped_factors(&depths, decay, c);
                let sc = score(&day_month, &f, rule, &events, None);
                if sc.csi > best.1 {
                    best = (c, sc.csi);
                }
            }
            let f = lumped_factors(&depths, decay, best.0);
            let sc = score(&day_month, &f, rule, &events, Some(&mut rng));
            println!("{:<34} | {:>7.0} | {:.2} [{:.2}, {:.2}] (n={:>2}) | {:>5.2} | {:>6.3} | {:>7} | {:>10.3}",
                format!("{name}, decay {decay}"), best.0, sc.pod, sc.pod_ci.0, sc.pod_ci.1, sc.n_ev, sc.far, sc.csi, sc.alert_months, sc.p_null);
            if matches!(rule, Rule::Api) && sc.csi > best_api.2 {
                best_api = (decay, best.0, sc.csi);
            }
        }
    }

    // Split-sample check of the cause×trigger rule: calibrate the critical API
    // depth on odd years, score the even years (with their own year-shuffle null).
    println!("\nSplit-sample, I–D AND API: crit calibrated on odd years, scored on even years");
    for &decay in &DECAYS[..2] {
        let odd = |m: &MonthKey| m.0 % 2 == 1;
        let (mut best, mut best_csi) = (f64::NAN, -1.0);
        for &c in &crits {
            let f = lumped_factors(&depths, decay, c);
            let (dmo, fo): (Vec<MonthKey>, Vec<(f64, f64)>) = day_month.iter().zip(&f).filter(|(m, _)| odd(m)).map(|(m, x)| (*m, *x)).unzip();
            let evo: Vec<MonthKey> = events.iter().copied().filter(|m| odd(m)).collect();
            let sc = score(&dmo, &fo, Rule::And, &evo, None);
            if sc.csi > best_csi {
                best_csi = sc.csi;
                best = c;
            }
        }
        let f = lumped_factors(&depths, decay, best);
        let (dme, fe): (Vec<MonthKey>, Vec<(f64, f64)>) = day_month.iter().zip(&f).filter(|(m, _)| !odd(m)).map(|(m, x)| (*m, *x)).unzip();
        let eve: Vec<MonthKey> = events.iter().copied().filter(|m| !odd(m)).collect();
        let and_e = score(&dme, &fe, Rule::And, &eve, Some(&mut rng));
        let id_e = score(&dme, &fe, Rule::Id, &eve, Some(&mut rng));
        println!("  decay {decay}: crit(odd) {best:.0} mm | even years — I–D AND API: POD {:.2} FAR {:.2} CSI {:.3} P(null≥) {:.3} ({} alert-m) || I–D alone: POD {:.2} FAR {:.2} CSI {:.3} P(null≥) {:.3} ({} alert-m)",
            and_e.pod, and_e.far, and_e.csi, and_e.p_null, and_e.alert_months, id_e.pod, id_e.far, id_e.csi, id_e.p_null, id_e.alert_months);
    }

    // ---------------- B. distributed
    let (dims, susc_v, dm, ddepths, devents) = read_dist();
    let n = dims.len();
    let n_steps = ddepths.len() / n;
    let mut months: Vec<MonthKey> = Vec::new();
    for &m in &dm {
        if months.last() != Some(&m) && !months.contains(&m) {
            months.push(m);
        }
    }
    let midx: Vec<usize> = dm.iter().map(|m| months.iter().position(|x| x == m).unwrap()).collect();
    let pos = footprint(dims, &months, &devents);
    let labels: Vec<bool> = (0..months.len() * n).map(|i| pos.contains(&i)).collect();
    let susc = SusceptibilityMap::new(dims, susc_v).unwrap();
    let flat = SusceptibilityMap::uniform(dims, 1.0).unwrap();
    let g = GriddedRain::new(dims, 24.0, ddepths).unwrap();
    let (decay, crit, _) = best_api;
    println!("\nB. Distributed daily backtest — {} cells × {} months, {} positive cell-months; API decay {decay}, crit {crit:.0} mm (lumped optimum)\n", n, months.len(), pos.len());
    println!("{:<40} | {:>26}", "configuration", "ROC-AUC [month-block 95%]");
    println!("{}", "-".repeat(70));
    let mut brng = Lcg(42);
    let configs: Vec<(&str, Combine, bool, bool)> = vec![
        ("I–D alone × real susc (paper)", Combine::Max, true, false),
        ("API alone, susc = 1", Combine::Max, false, true),
        ("API alone × real susc", Combine::Max, false, true),
        ("I–D × API (product) × real susc", Combine::Product, true, true),
        ("I–D ⊕ API (noisy-OR) × real susc", Combine::NoisyOr, true, true),
    ];
    for (i, (name, comb, use_id, use_api)) in configs.into_iter().enumerate() {
        let mut trig: Vec<Box<dyn Trigger>> = Vec::new();
        if use_id {
            trig.push(Box::new(IdTrigger::new(g.clone(), IdThreshold::new(A_STAR, B).unwrap(), TriggerModel::default(), MAX_WINDOW_DAYS).unwrap()));
        }
        if use_api {
            trig.push(Box::new(AntecedentTrigger::new(g.clone(), decay, crit, TriggerModel::default()).unwrap()));
        }
        let sm = if i == 1 { flat.clone() } else { susc.clone() };
        let mn = MultiNowcast::new(sm, trig, comb).unwrap();
        let mut maxh = vec![0.0_f64; months.len() * n];
        for (t, field) in mn.run().into_iter().enumerate().take(n_steps) {
            let mi = midx[t];
            for (c, &p) in field.probability().iter().enumerate() {
                let k = mi * n + c;
                if p > maxh[k] {
                    maxh[k] = p;
                }
            }
        }
        let (a, lo, hi) = auc_ci(&maxh, &labels, n, months.len(), &mut brng);
        println!("{name:<40} | {a:.3} [{lo:.3}, {hi:.3}]");
    }
}
