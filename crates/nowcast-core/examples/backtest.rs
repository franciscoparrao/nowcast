//! Backtest of the rainfall I–D trigger against the SERNAGEOMIN inventory for
//! the Río Maipo basin (Andean precordillera SE of Santiago).
//!
//! Real data:
//!   - `data/maipo_cr2met_pr_1979_2016.csv`  daily precip (CR2MET v2.5, mm) at
//!     the centroid of the dated rainfall-triggered events.
//!   - `data/maipo_events_dated.csv`         rainfall-triggered landslide events
//!     from the SERNAGEOMIN inventory, dated (year, month) from the record id.
//!
//! Run with: `cargo run --example backtest`
//!
//! What it tests: the *timing* of the dynamic trigger. At v0.1 the forcing is a
//! single representative gauge, so susceptibility (spatial) is held at 1.0 and
//! the hazard reduces to the trigger factor; an alert day is one whose rolling
//! mean rainfall sits on or above the I–D curve `I = a·D^-0.39`. We sweep the
//! intercept `a` (i.e. slide the curve) and score monthly hits/false-alarms
//! against the inventory — effectively calibrating a regional I–D intercept and
//! validating it split-sample (odd calibration years → even validation years).
//!
//! Caveats (honest v0.1): events are dated to ~month and the inventory month can
//! be off by weeks (the May 1993 Macul debris flow is filed under March), so we
//! match months with ±1 month tolerance; a single gauge cannot resolve where on
//! the susceptibility surface a slide occurs — that arrives with distributed
//! forcing in v0.2.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use nowcast_core::{
    Contingency, GridDims, IdThreshold, MonthKey, Nowcast, SusceptibilityMap, TriggerModel,
    UniformRain, monthly_contingency,
};

const B_EXPONENT: f64 = 0.39; // Caine (1980) duration exponent
const MAX_WINDOW_DAYS: usize = 7; // I–D storm durations (1–7 days), not seasonal accumulation
const ALERT_LEVEL: f64 = 0.5; // hazard ≥ 0.5 ⟺ exceedance ≥ 1 (on/above the curve)
const TOL_MONTHS: u32 = 1; // absorb inventory month-level date uncertainty

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

/// Read `data/...pr...csv` → per-day `(year, month)` keys and rainfall depths.
fn read_precip() -> (Vec<MonthKey>, Vec<f64>) {
    let path = data_dir().join("maipo_cr2met_pr_1979_2016.csv");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    let mut months = Vec::new();
    let mut depths = Vec::new();
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let (Some(date), Some(p)) = (f.next(), f.next()) else {
            continue;
        };
        let mut d = date.split('-');
        let year: i32 = d.next().unwrap().parse().unwrap();
        let month: u32 = d.next().unwrap().parse().unwrap();
        months.push((year, month));
        depths.push(p.trim().parse().unwrap_or(0.0));
    }
    (months, depths)
}

/// Read `data/...events...csv` → the `(year, month)` of each event.
fn read_events() -> Vec<MonthKey> {
    let path = data_dir().join("maipo_events_dated.csv");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    text.lines()
        .skip(1)
        .filter_map(|line| {
            let mut f = line.split(',');
            let _id = f.next()?;
            let year: i32 = f.next()?.parse().ok()?;
            let month: u32 = f.next()?.parse().ok()?;
            Some((year, month))
        })
        .collect()
}

/// Per-day alert flags for a given I–D intercept `a`, over a single-cell grid
/// with susceptibility 1.0 (so hazard == trigger factor).
fn alert_days(depths: &[f64], a: f64) -> Vec<bool> {
    let dims = GridDims::new(1, 1);
    let forcing = UniformRain::new(dims, 24.0, depths.to_vec()).unwrap();
    let susceptibility = SusceptibilityMap::uniform(dims, 1.0).unwrap();
    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::new(a, B_EXPONENT).unwrap(),
        TriggerModel::default(),
        MAX_WINDOW_DAYS,
    )
    .unwrap();
    nowcast
        .run()
        .into_iter()
        .map(|f| f.max_probability() >= ALERT_LEVEL)
        .collect()
}

/// Wilson 95 % interval for `k` successes in `n` trials.
fn wilson(k: u64, n: u64) -> (f64, f64) {
    if n == 0 {
        return (f64::NAN, f64::NAN);
    }
    let (z, p, n) = (1.96_f64, k as f64 / n as f64, n as f64);
    let denom = 1.0 + z * z / n;
    let centre = (p + z * z / (2.0 * n)) / denom;
    let half = z * (p * (1.0 - p) / n + z * z / (4.0 * n * n)).sqrt() / denom;
    (centre - half, centre + half)
}

/// Same counting as `monthly_contingency`, on month-level sets (used by the
/// permutation null, which shuffles alert months rather than days).
fn month_contingency(
    present: &BTreeSet<MonthKey>,
    alerted: &BTreeSet<MonthKey>,
    observed: &BTreeSet<MonthKey>,
    tol: i32,
) -> Contingency {
    let shift = |(y, m): MonthKey, d: i32| -> MonthKey {
        let idx = y * 12 + (m as i32 - 1) + d;
        (idx.div_euclid(12), (idx.rem_euclid(12) + 1) as u32)
    };
    let alert_near = |mk: MonthKey| (-tol..=tol).any(|d| alerted.contains(&shift(mk, d)));
    let event_near = |mk: MonthKey| (-tol..=tol).any(|d| observed.contains(&shift(mk, d)));
    let mut c = Contingency::default();
    for &ev in observed {
        if alert_near(ev) {
            c.hits += 1;
        } else {
            c.misses += 1;
        }
    }
    for &mk in present {
        if event_near(mk) {
            continue;
        }
        if alerted.contains(&mk) {
            c.false_alarms += 1;
        } else {
            c.correct_negatives += 1;
        }
    }
    c
}

/// Deterministic LCG (Numerical Recipes constants) — no `rand` dependency.
struct Lcg(u64);
impl Lcg {
    fn next_u32(&mut self) -> u32 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 32) as u32
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u32() as usize) % n
    }
}

fn nulls_and_intervals(day_month: &[MonthKey], alerts: &[bool], events: &[MonthKey], a: f64) {
    const N_PERM: usize = 2000;
    let present: BTreeSet<MonthKey> = day_month.iter().copied().collect();
    let alerted: BTreeSet<MonthKey> = day_month
        .iter()
        .zip(alerts)
        .filter_map(|(&mk, &al)| al.then_some(mk))
        .collect();
    let observed: BTreeSet<MonthKey> =
        events.iter().copied().filter(|mk| present.contains(mk)).collect();
    let years: Vec<i32> = {
        let mut v: Vec<i32> = present.iter().map(|&(y, _)| y).collect();
        v.dedup();
        v
    };
    println!(
        "\nUncertainty and nulls at a*={a:.1}: {} event-months, {} alert-months of {} ({:.0} % alert rate)",
        observed.len(),
        alerted.len(),
        present.len(),
        100.0 * alerted.len() as f64 / present.len() as f64
    );
    println!(
        "{:>6} | {:>22} | {:>22} | {:>6} || {:>26} | {:>8} | {:>12}",
        "tol", "POD [Wilson 95%]", "FAR [Wilson 95%]", "CSI", "year-shuffle POD mean/p95", "CSI p95", "P(CSI>=obs)"
    );
    println!("{}", "-".repeat(124));
    let mut rng = Lcg(20261015);
    for tol in 0..=3i32 {
        let c = month_contingency(&present, &alerted, &observed, tol);
        let (pod_lo, pod_hi) = wilson(c.hits, c.hits + c.misses);
        let (far_lo, far_hi) = wilson(c.false_alarms, c.hits + c.false_alarms);
        let obs_csi = c.csi().unwrap_or(0.0);
        // Year-shuffle null: alert months of year y are reassigned to year perm(y).
        let (mut pods, mut csis) = (Vec::with_capacity(N_PERM), Vec::with_capacity(N_PERM));
        let mut ge = 0usize;
        for _ in 0..N_PERM {
            let mut perm = years.clone();
            for i in (1..perm.len()).rev() {
                let j = rng.below(i + 1);
                perm.swap(i, j);
            }
            let map: BTreeMap<i32, i32> = years.iter().copied().zip(perm).collect();
            let shuffled: BTreeSet<MonthKey> = alerted.iter().map(|&(y, m)| (map[&y], m)).collect();
            let cn = month_contingency(&present, &shuffled, &observed, tol);
            let csi_n = cn.csi().unwrap_or(0.0);
            pods.push(cn.pod().unwrap_or(0.0));
            csis.push(csi_n);
            if csi_n >= obs_csi {
                ge += 1;
            }
        }
        pods.sort_by(|x, y| x.partial_cmp(y).unwrap());
        csis.sort_by(|x, y| x.partial_cmp(y).unwrap());
        let mean = pods.iter().sum::<f64>() / pods.len() as f64;
        let p95 = |v: &[f64]| v[(0.95 * (v.len() - 1) as f64) as usize];
        println!(
            "{:>4}mo | {:>4.2} [{:.2}, {:.2}] (n={:>2}) | {:>4.2} [{:.2}, {:.2}] (n={:>3}) | {:>6.3} || {:>12.2} / {:<11.2} | {:>8.3} | {:>12.3}",
            tol, c.pod().unwrap_or(0.0), pod_lo, pod_hi, c.hits + c.misses,
            c.far().unwrap_or(0.0), far_lo, far_hi, c.hits + c.false_alarms, obs_csi,
            mean, p95(&pods), p95(&csis), ge as f64 / N_PERM as f64
        );
    }
    // Wet-season alerter: every May–September month.
    let wet: BTreeSet<MonthKey> = present.iter().copied().filter(|&(_, m)| (5..=9).contains(&m)).collect();
    println!("\nWet-season alerter (every May–September month, {} alert-months):", wet.len());
    for tol in 0..=3i32 {
        print_row(&format!("wet ±{tol}"), &month_contingency(&present, &wet, &observed, tol));
    }
    // Independent-alerter chance curve at the observed alert rate (the reviewer's formula).
    let f = alerted.len() as f64 / present.len() as f64;
    println!(
        "Independent alerter at rate f={f:.2}: P(hit within ±t) = {}",
        (0..=3).map(|t| format!("{:.2}", 1.0 - (1.0 - f).powi(2 * t + 1))).collect::<Vec<_>>().join(" / ")
    );
}

fn print_row(label: &str, c: &Contingency) {
    let pct = |x: Option<f64>| x.map(|v| format!("{:.2}", v)).unwrap_or_else(|| "  -".into());
    println!(
        "{label:>10} | H {:>3}  M {:>3}  F {:>4}  CN {:>4} | POD {:>4}  FAR {:>4}  CSI {:>4}  bias {:>4}",
        c.hits,
        c.misses,
        c.false_alarms,
        c.correct_negatives,
        pct(c.pod()),
        pct(c.far()),
        pct(c.csi()),
        pct(c.frequency_bias()),
    );
}

fn main() {
    let (day_month, depths) = read_precip();
    let events = read_events();
    let event_months: BTreeSet<MonthKey> = events.iter().copied().collect();
    let in_period: usize = event_months
        .iter()
        .filter(|(y, _)| (1979..=2016).contains(y))
        .count();

    println!(
        "Maipo I–D backtest — {} days ({}–{}), {} dated rain events in {} distinct months ({} in period)\n",
        depths.len(),
        day_month.first().unwrap().0,
        day_month.last().unwrap().0,
        events.len(),
        event_months.len(),
        in_period,
    );
    println!("I_crit(D) = a · D^-{B_EXPONENT}   |   alert ⟺ rolling mean rainfall on/above the curve   |   ±{TOL_MONTHS} month match\n");

    // --- Full-period sweep of the intercept a -------------------------------
    println!("Sweep of intercept a (full period 1979–2016, monthly contingency):");
    let sweep: Vec<f64> = (4..=32).map(|i| i as f64 * 0.5).collect(); // 2.0 .. 16.0
    let mut best = (f64::NAN, -1.0_f64, Contingency::default()); // (a, csi, table)
    for &a in &sweep {
        let alerts = alert_days(&depths, a);
        let c = monthly_contingency(&day_month, &alerts, &events, TOL_MONTHS).unwrap();
        let csi = c.csi().unwrap_or(0.0);
        if csi > best.1 {
            best = (a, csi, c);
        }
        // Print a sparse subset to keep the table readable.
        if (a * 2.0) as i64 % 2 == 0 {
            print_row(&format!("a={a:>4.1}"), &c);
        }
    }
    println!();
    print_row(&format!("BEST a={:.1}", best.0), &best.2);
    println!(
        "  → max-CSI intercept a* = {:.1} mm/h @ D=1h  (vs Caine global a=14.82)\n",
        best.0
    );

    // --- Split-sample: calibrate a* on odd years, validate on even years ----
    let odd: Vec<bool> = day_month.iter().map(|(y, _)| y % 2 == 1).collect();
    let cal_days: Vec<MonthKey> = day_month
        .iter()
        .zip(&odd)
        .filter_map(|(&m, &o)| o.then_some(m))
        .collect();
    let val_days: Vec<MonthKey> = day_month
        .iter()
        .zip(&odd)
        .filter_map(|(&m, &o)| (!o).then_some(m))
        .collect();
    let cal_events: Vec<MonthKey> = events.iter().copied().filter(|(y, _)| y % 2 == 1).collect();
    let val_events: Vec<MonthKey> = events.iter().copied().filter(|(y, _)| y % 2 == 0).collect();

    let mut cal_best = (f64::NAN, -1.0_f64);
    for &a in &sweep {
        let alerts = alert_days(&depths, a);
        let cal_alerts: Vec<bool> = alerts
            .iter()
            .zip(&odd)
            .filter_map(|(&al, &o)| o.then_some(al))
            .collect();
        let c = monthly_contingency(&cal_days, &cal_alerts, &cal_events, TOL_MONTHS).unwrap();
        let csi = c.csi().unwrap_or(0.0);
        if csi > cal_best.1 {
            cal_best = (a, csi);
        }
    }
    let a_star = cal_best.0;
    let alerts = alert_days(&depths, a_star);
    let cal_alerts: Vec<bool> = alerts
        .iter()
        .zip(&odd)
        .filter_map(|(&al, &o)| o.then_some(al))
        .collect();
    let val_alerts: Vec<bool> = alerts
        .iter()
        .zip(&odd)
        .filter_map(|(&al, &o)| (!o).then_some(al))
        .collect();
    println!("Split-sample (calibrate a* on odd years, validate on even years):");
    println!("  calibrated a* = {a_star:.1} mm/h");
    print_row(
        "cal(odd)",
        &monthly_contingency(&cal_days, &cal_alerts, &cal_events, TOL_MONTHS).unwrap(),
    );
    print_row(
        "val(even)",
        &monthly_contingency(&val_days, &val_alerts, &val_events, TOL_MONTHS).unwrap(),
    );

    // --- Sensitivity to inventory date uncertainty --------------------------
    // The inventory month is approximate (the May 1993 Macul flow is filed as
    // March). Widening the match window shows how much skill that noise costs.
    println!("\nMatch-window sensitivity at a*={:.1} (inventory months are approximate):", best.0);
    let best_alerts = alert_days(&depths, best.0);
    for tol in [0u32, 1, 2, 3] {
        print_row(
            &format!("±{tol} mo"),
            &monthly_contingency(&day_month, &best_alerts, &events, tol).unwrap(),
        );
    }

    // --- Uncertainty and nulls (added after the EMS review, 2026-10-15) --------
    // The scores above are point values over ~18 event-months; a reviewer
    // computed that the rise of POD with the match tolerance is what an
    // alerter firing at random at the same monthly rate would produce. So:
    // (1) Wilson 95 % intervals on POD and FAR, (2) a year-shuffle null that
    // keeps seasonality and within-year clustering of the alerts but breaks
    // their link to the event years, (3) the wet-season alerter (every
    // May–September month), and (4) the alert rate itself.
    nulls_and_intervals(&day_month, &best_alerts, &events, best.0);

    // --- Caine global threshold for reference -------------------------------
    println!("\nReference — Caine (1980) global threshold a=14.82:");
    let caine_alerts = alert_days(&depths, 14.82);
    print_row(
        "Caine",
        &monthly_contingency(&day_month, &caine_alerts, &events, TOL_MONTHS).unwrap(),
    );
}
