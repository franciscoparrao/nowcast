//! Maipo daily backtest against PUBLISHED intensity--duration thresholds (review R-2).
//!
//! The `backtest` example *self-calibrates* a regional I--D intercept. A reviewer
//! rightly asks whether published thresholds — taken verbatim, not fitted here —
//! fare any better. This example applies the canonical compilation of empirical
//! I--D power laws `I = a·D^-b` (intensity mm/h, duration h) tabulated by
//!   Guzzetti, Peruccacci, Rossi & Stark (2007), "Rainfall thresholds for the
//!   initiation of landslides in central and southern Europe", Meteorology and
//!   Atmospheric Physics 98, 239–267, Table 2 (the 35 pure power laws; the c≠0
//!   asymptotic and normalized forms are excluded).
//! Each threshold is run UNCHANGED over the same CR2MET daily forcing and scored
//! against the SERNAGEOMIN inventory, exactly as in `backtest`.
//!
//! Run with: `cargo run --example published_thresholds`
//!
//! The point: at daily resolution the operating point is set by the maximum
//! resolvable 24-h intensity (a daily total spread over 24 h), not by which
//! published curve is chosen. High-intercept curves never fire (POD 0); low ones
//! fire on nearly every wet month (FAR → 1); none beats the self-calibrated
//! regional intercept. This is the resolution ceiling, restated through the
//! literature rather than through one fitted curve.

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{
    Contingency, GridDims, IdThreshold, MonthKey, Nowcast, SusceptibilityMap, TriggerModel,
    UniformRain, monthly_contingency,
};

const MAX_WINDOW_DAYS: usize = 7; // storm I–D durations (1–7 days)
const ALERT_LEVEL: f64 = 0.5; // hazard ≥ 0.5 ⟺ exceedance ≥ 1 (on/above the curve)
const TOL_MONTHS: u32 = 1; // absorb inventory month-level date uncertainty

/// Published I–D power laws `I = a·D^-b` (a in mm/h, b dimensionless), verbatim
/// from Guzzetti et al. (2007), Table 2. Entry 0 is the Caine (1980) global curve.
const PUBLISHED: &[(f64, f64)] = &[
    (14.82, 0.39),  // Caine (1980), global
    (41.66, 0.77),
    (44.668, 0.78),
    (66.18, 0.52),
    (41.83, 0.58),
    (39.71, 0.62),
    (35.23, 0.54),
    (26.51, 0.19),
    (30.53, 0.57),
    (176.40, 0.90),
    (27.3, 0.38),
    (20.1, 0.55),
    (91.46, 0.82),
    (9.23, 0.37),
    (10.0, 0.77),
    (5.94, 1.50),
    (32.0, 0.70),
    (47.742, 0.507),
    (9.521, 0.4955),
    (11.698, 0.4783),
    (11.00, 0.4459),
    (10.67, 0.5043),
    (12.649, 0.5324),
    (18.675, 0.565),
    (28.10, 0.74),
    (9.9, 0.52),
    (116.48, 0.63),
    (15.0, 0.70),
    (11.5, 0.26),
    (4.0, 0.45),
    (19.0, 0.50),
    (18.83, 0.59),
    (82.73, 1.13),
    (7.00, 0.60),
    (115.47, 0.80),
];

/// Self-calibrated regional intercept from the `backtest` example, for reference.
const REGIONAL: (f64, f64) = (5.5, 0.39);

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

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

/// Per-day alert flags for a given I–D curve (a, b), susceptibility 1.0.
fn alert_days(depths: &[f64], a: f64, b: f64) -> Vec<bool> {
    let dims = GridDims::new(1, 1);
    let forcing = UniformRain::new(dims, 24.0, depths.to_vec()).unwrap();
    let susceptibility = SusceptibilityMap::uniform(dims, 1.0).unwrap();
    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::new(a, b).unwrap(),
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

fn score(
    day_month: &[MonthKey],
    depths: &[f64],
    events: &[MonthKey],
    a: f64,
    b: f64,
) -> Contingency {
    let alerts = alert_days(depths, a, b);
    monthly_contingency(day_month, &alerts, events, TOL_MONTHS)
}

fn main() {
    let (day_month, depths) = read_precip();
    let events = read_events();
    let event_months: BTreeSet<MonthKey> = events.iter().copied().collect();
    let max_daily = depths.iter().cloned().fold(0.0_f64, f64::max);

    println!(
        "Maipo backtest vs PUBLISHED I–D thresholds (Guzzetti et al. 2007, Table 2)\n\
         {} daily steps {}–{}, {} dated events in {} months; max daily rainfall {:.1} mm\n\
         alert ⟺ rolling-mean rainfall on/above I = a·D^-b; ±{} month match\n",
        depths.len(),
        day_month.first().unwrap().0,
        day_month.last().unwrap().0,
        events.len(),
        event_months.len(),
        max_daily,
        TOL_MONTHS,
    );

    // --- Resolution ceiling: what daily total each curve needs to fire at 24 h --
    // Max resolvable 24-h intensity is (daily total)/24, so a curve fires at the
    // 24-h window only if some day's total ≥ 24 · I_crit(24 h) = 24 · a · 24^-b.
    println!("Per-curve 24-h firing requirement (needs a day with ≥ this total):");
    let mut need_le_max = 0usize;
    for &(a, b) in PUBLISHED {
        let need = 24.0 * a * 24.0_f64.powf(-b);
        if need <= max_daily {
            need_le_max += 1;
        }
    }
    println!(
        "  {} of {} published curves can ever fire at 24 h on this basin (need ≤ {:.0} mm/day);\n  \
         the other {} are structurally above the daily ceiling regardless of calibration.\n",
        need_le_max,
        PUBLISHED.len(),
        max_daily,
        PUBLISHED.len() - need_le_max,
    );

    // --- Score every published curve, collect the distribution ------------------
    let mut rows: Vec<(f64, f64, Contingency)> = PUBLISHED
        .iter()
        .map(|&(a, b)| (a, b, score(&day_month, &depths, &events, a, b)))
        .collect();

    let never_fire = rows.iter().filter(|(_, _, c)| c.hits + c.false_alarms == 0).count();
    let pod_zero = rows.iter().filter(|(_, _, c)| c.hits == 0).count();
    let far_high = rows
        .iter()
        .filter(|(_, _, c)| c.far().is_some_and(|f| f >= 0.9))
        .count();

    println!("Across the {} published curves applied to daily forcing:", PUBLISHED.len());
    println!("  • {never_fire} never fire at all (no alert in 38 years)");
    println!("  • {pod_zero} have POD = 0 (never catch a dated event)");
    println!("  • {far_high} of those that do fire have FAR ≥ 0.90\n");

    // Best CSI in the published ensemble vs the self-calibrated regional curve.
    rows.sort_by(|a, b| {
        b.2.csi()
            .unwrap_or(0.0)
            .partial_cmp(&a.2.csi().unwrap_or(0.0))
            .unwrap()
    });
    let pct = |x: Option<f64>| x.map(|v| format!("{v:.2}")).unwrap_or_else(|| "  - ".into());
    println!("Top published curves by CSI (the literature's best case here):");
    println!("       a       b  | POD   FAR   CSI   bias");
    for (a, b, c) in rows.iter().take(5) {
        println!(
            "  {a:7.2} {b:6.3} | {}  {}  {}  {}",
            pct(c.pod()),
            pct(c.far()),
            pct(c.csi()),
            pct(c.frequency_bias()),
        );
    }
    let caine = score(&day_month, &depths, &events, PUBLISHED[0].0, PUBLISHED[0].1);
    let reg = score(&day_month, &depths, &events, REGIONAL.0, REGIONAL.1);
    println!(
        "\n  Caine (1980) a=14.82 b=0.39 | {}  {}  {}  {}",
        pct(caine.pod()),
        pct(caine.far()),
        pct(caine.csi()),
        pct(caine.frequency_bias()),
    );
    println!(
        "  self-calib.  a= 5.50 b=0.39 | {}  {}  {}  {}   (regional, from `backtest`)",
        pct(reg.pod()),
        pct(reg.far()),
        pct(reg.csi()),
        pct(reg.frequency_bias()),
    );

    let best_pub_csi = rows.first().and_then(|(_, _, c)| c.csi()).unwrap_or(0.0);
    println!(
        "\nReading: the best published curve reaches CSI {:.3} vs {:.3} for the self-\n\
         calibrated regional intercept — published thresholds do not rescue daily forcing.\n\
         At 24 h the maximum resolvable intensity is (daily total)/24, so a curve either\n\
         sits above the wettest day (POD 0) or, lowered enough to fire, fires on most wet\n\
         months (FAR → 1). The binding constraint is forcing resolution, not the choice of\n\
         published threshold.",
        best_pub_csi, reg.csi().unwrap_or(0.0),
    );
}
