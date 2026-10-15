//! One-at-a-time sensitivity of the paper's headline numbers to the trigger
//! hyperparameters that the text holds fixed: the I–D exponent `b`, the maximum
//! I–D duration `m_max`, the logistic steepness `k` and the alert level.
//!
//! Added after the EMS review (2026-10-15): "k = 4, alert level 0.5, b = 0.39,
//! m_max = 7 d … are all fixed". Two families of results are re-run:
//!
//! 1. The lumped Maipo backtest (Section 5.1): for each setting the intercept
//!    `a*` is RE-CALIBRATED by max-CSI over the same sweep as `backtest.rs`, and
//!    POD/FAR/CSI at ±1 month are reported, plus the scores at the paper's fixed
//!    a* = 5.5 so the reader can separate "the optimum moves" from "the skill
//!    moves".
//! 2. The three storm cores of the single-factor decomposition (Section 5.6):
//!    peak exceedance E at half-hourly and at daily resolution for a* = 5.5, so
//!    the "Fires?" column of Table 8 can be read under other exponents.
//!
//! `k` and the alert level enter the lumped backtest only through the exceedance
//! at which the hazard crosses the level: with susceptibility 1,
//! hazard = T(E) = 1/(1+exp(−k(E−1))) ≥ L  ⇔  E ≥ 1 + ln(L/(1−L))/k, so they
//! are swept jointly as that threshold E_thr (= 1 exactly at L = 0.5 for any k).
//! Rank-based scores (AUC, POD@area) are invariant to k by construction and are
//! not re-run.
//!
//! Run with: `cargo run --release --example sensitivity_oat`

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{Contingency, IdThreshold, MonthKey, monthly_contingency};

#[path = "shared/curves.rs"]
mod curves;

const TOL_MONTHS: u32 = 1;

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

fn read_precip() -> (Vec<MonthKey>, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_cr2met_pr_1979_2016.csv")).unwrap();
    let (mut months, mut depths) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let (Some(date), Some(p)) = (f.next(), f.next()) else { continue };
        let mut d = date.split('-');
        months.push((d.next().unwrap().parse().unwrap(), d.next().unwrap().parse().unwrap()));
        depths.push(p.trim().parse().unwrap_or(0.0));
    }
    (months, depths)
}

fn read_events() -> Vec<MonthKey> {
    let text = std::fs::read_to_string(data_dir().join("maipo_events_dated.csv")).unwrap();
    text.lines()
        .skip(1)
        .filter_map(|l| {
            let mut f = l.split(',');
            f.next()?;
            Some((f.next()?.trim().parse().ok()?, f.next()?.trim().parse().ok()?))
        })
        .collect()
}

fn read_core(key: &str) -> (Vec<String>, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join(format!("event_{key}.csv"))).unwrap();
    let (mut t, mut r) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        t.push(f.next().unwrap().to_string());
        // The core files hold intensities (mm/h) per half-hour step; the engine wants depths per step.
        r.push(f.next().unwrap().trim().parse::<f64>().unwrap_or(0.0) * 0.5);
    }
    (t, r)
}

fn to_daily(stamps: &[String], depths: &[f64]) -> Vec<f64> {
    let mut daily = Vec::new();
    let (mut i, n) = (0usize, stamps.len());
    while i < n {
        let day = &stamps[i][..10];
        let mut sum = 0.0;
        while i < n && stamps[i][..10] == *day {
            sum += depths[i];
            i += 1;
        }
        daily.push(sum);
    }
    daily
}

fn prefix_sums(depths: &[f64]) -> Vec<f64> {
    let mut p = vec![0.0_f64; depths.len() + 1];
    for (i, d) in depths.iter().enumerate() {
        p[i + 1] = p[i] + d;
    }
    p
}

/// Per-step peak exceedance with the engine's window kernel (same as `IdThreshold::worst_window`).
fn exceedance_series(depths: &[f64], dt: f64, max_m: usize, a: f64, b: f64) -> Vec<f64> {
    let th = IdThreshold::new(a, b).unwrap();
    let prefix = prefix_sums(depths);
    (0..depths.len())
        .map(|t| {
            let avail = (t + 1).min(max_m);
            th.worst_window(dt, avail, |m| prefix[t + 1] - prefix[t + 1 - m]).0
        })
        .collect()
}

fn lumped(day_month: &[MonthKey], depths: &[f64], events: &[MonthKey], a: f64, b: f64, max_m: usize, e_thr: f64) -> Contingency {
    let e = exceedance_series(depths, 24.0, max_m, a, b);
    let alerts: Vec<bool> = e.iter().map(|&x| x >= e_thr).collect();
    monthly_contingency(day_month, &alerts, events, TOL_MONTHS).unwrap()
}

fn fmt(c: &Contingency) -> String {
    format!("POD {:.2} FAR {:.2} CSI {:.3}", c.pod().unwrap_or(0.0), c.far().unwrap_or(0.0), c.csi().unwrap_or(0.0))
}

fn main() {
    let (day_month, depths) = read_precip();
    let events = read_events();
    let _distinct: BTreeSet<MonthKey> = events.iter().copied().collect();
    let sweep: Vec<f64> = (4..=32).map(|i| i as f64 * 0.5).collect();
    let (a_paper, b_paper) = curves::REGIONAL;
    let e_thr = |k: f64, level: f64| 1.0 + (level / (1.0 - level)).ln() / k;

    // (b, m_max days, k, level, label)
    let settings: Vec<(f64, usize, f64, f64, String)> = {
        let mut v = vec![(b_paper, 7, 4.0, 0.5, "paper: b 0.39, m_max 7 d, k 4, level 0.5".to_string())];
        for &b in &[0.19, 0.565, 0.78] {
            v.push((b, 7, 4.0, 0.5, format!("b {b} (published range 0.19–1.50, median 0.57)")));
        }
        for &m in &[1usize, 3] {
            v.push((b_paper, m, 4.0, 0.5, format!("m_max {m} d")));
        }
        for &(k, l) in &[(4.0, 0.4), (4.0, 0.6), (2.0, 0.6), (8.0, 0.6)] {
            v.push((b_paper, 7, k, l, format!("k {k}, level {l} (E_thr {:.2})", e_thr(k, l))));
        }
        v
    };

    println!("One-at-a-time sensitivity — lumped Maipo backtest (±{TOL_MONTHS} month), a* re-calibrated by max-CSI per setting\n");
    println!("{:<52} | {:>5} | {:>30} | {:>30}", "setting", "a*", "at re-calibrated a*", "at the paper's a* = 5.5");
    println!("{}", "-".repeat(126));
    let mut csv = String::from("setting,b,m_max_d,k,level,e_thr,a_star,pod_star,far_star,csi_star,pod_55,far_55,csi_55\n");
    for (b, m, k, l, label) in &settings {
        let thr = e_thr(*k, *l);
        let mut best = (f64::NAN, -1.0, Contingency::default());
        for &a in &sweep {
            let c = lumped(&day_month, &depths, &events, a, *b, *m, thr);
            let csi = c.csi().unwrap_or(0.0);
            if csi > best.1 {
                best = (a, csi, c);
            }
        }
        let fixed = lumped(&day_month, &depths, &events, a_paper, *b, *m, thr);
        println!("{label:<52} | {:>5.1} | {:>30} | {:>30}", best.0, fmt(&best.2), fmt(&fixed));
        csv.push_str(&format!(
            "\"{label}\",{b},{m},{k},{l},{thr:.3},{:.1},{:.3},{:.3},{:.4},{:.3},{:.3},{:.4}\n",
            best.0, best.2.pod().unwrap_or(0.0), best.2.far().unwrap_or(0.0), best.2.csi().unwrap_or(0.0),
            fixed.pod().unwrap_or(0.0), fixed.far().unwrap_or(0.0), fixed.csi().unwrap_or(0.0)
        ));
    }

    // Storm cores: peak E at half-hourly and daily for a* = 5.5 under each b.
    let cores = [("atacama_2015", "Atacama/Copiapó"), ("maipo_2017", "Cajón del Maipo"), ("santalucia_2017", "Villa Santa Lucía")];
    println!("\nStorm cores (Section 5.6): peak exceedance E at a* = 5.5, half-hourly | daily (max window 24 h); crosses if E ≥ 1\n");
    println!("{:<8} | {:<18} | {:<18} | {:<18}", "b", cores[0].1, cores[1].1, cores[2].1);
    println!("{}", "-".repeat(70));
    csv.push_str("core_b,atacama_hh,atacama_daily,maipo_hh,maipo_daily,santalucia_hh,santalucia_daily\n");
    for &b in &[0.19, b_paper, 0.565, 0.78] {
        let mut row = format!("{b:<8.3}");
        let mut vals = Vec::new();
        for (key, _) in &cores {
            let (t, r) = read_core(key);
            let e_hh = exceedance_series(&r, 0.5, 48, a_paper, b).iter().cloned().fold(0.0, f64::max);
            let daily = to_daily(&t, &r);
            let e_d = exceedance_series(&daily, 24.0, 1, a_paper, b).iter().cloned().fold(0.0, f64::max);
            row.push_str(&format!(" | {:>7.2} | {:>7.2}  ", e_hh, e_d));
            vals.push(e_hh);
            vals.push(e_d);
        }
        println!("{row}");
        csv.push_str(&format!("{b},{}\n", vals.iter().map(|v| format!("{v:.3}")).collect::<Vec<_>>().join(",")));
    }
    println!("\nReading: the lumped optimum a* moves with b (the curve pivots about D = 1 h), the skill at the optimum\n\
              does not; m_max and the (k, level) pair move the scores at the fixed a* but not the flat optimum.\n\
              At the cores, b changes the daily E more than the half-hourly E because the daily window is 24 h\n\
              (D^b grows with b), so the 'Fires? daily' column is the b-sensitive one.");
    let path = data_dir().join("sensitivity_oat.csv");
    std::fs::write(&path, csv).unwrap();
    eprintln!("wrote {}", path.display());
}
