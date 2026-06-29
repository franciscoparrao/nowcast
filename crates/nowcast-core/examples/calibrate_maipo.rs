//! Calibrate the hazard index on the REAL Río Maipo distributed backtest.
//!
//! The `calibrated_probability` example shows the calibration machinery on a
//! synthetic field with a planted signal. This one runs it on real data: the
//! distributed Maipo backtest (15×18 CR2MET grid, real RandomForest
//! susceptibility, SERNAGEOMIN event footprint) — the same setup that yields the
//! honest null (ROC-AUC ≈ 0.48). We fit the isotonic calibrator on odd years and
//! score reliability on even years.
//!
//! The expected — and honest — outcome is that the daily index carries little
//! information, so the calibrated probability collapses toward the base rate and
//! the reliability diagram is nearly flat: calibration does not manufacture skill
//! the forcing does not contain. It does, however, turn the bare index into a
//! probability with quantified uncertainty (Wilson intervals), which is the point.
//!
//! Run with: `cargo run --release --example calibrate_maipo`

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{
    Calibrator, GridDims, GriddedRain, IdThreshold, MonthKey, Nowcast, SusceptibilityMap,
    TriggerModel, brier_score, reliability,
};

const A_INTERCEPT: f64 = 5.5;
const B_EXPONENT: f64 = 0.39;
const MAX_WINDOW_DAYS: usize = 7;
const CELL_RADIUS: usize = 1;
const TOL_MONTHS: u32 = 1;

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

fn read_grid() -> (GridDims, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_grid.csv")).unwrap();
    let (mut max_row, mut max_col) = (0usize, 0usize);
    let mut rows: Vec<(usize, f64)> = Vec::new();
    for line in text.lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let cell: usize = f[0].parse().unwrap();
        max_row = max_row.max(f[1].parse().unwrap());
        max_col = max_col.max(f[2].parse().unwrap());
        rows.push((cell, f[5].parse().unwrap()));
    }
    rows.sort_by_key(|(c, _)| *c);
    let susc = rows.into_iter().map(|(_, s)| s).collect();
    (GridDims::new(max_col + 1, max_row + 1), susc)
}

fn read_precip(dims: GridDims) -> (Vec<MonthKey>, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_pr.csv")).unwrap();
    let (mut months, mut depths) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let mut d = f.next().unwrap().split('-');
        let y: i32 = d.next().unwrap().parse().unwrap();
        let m: u32 = d.next().unwrap().parse().unwrap();
        months.push((y, m));
        depths.extend(f.map(|v| v.parse::<f64>().unwrap_or(0.0)));
    }
    let _ = dims;
    (months, depths)
}

fn read_events() -> Vec<(usize, MonthKey)> {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_events.csv")).unwrap();
    text.lines()
        .skip(1)
        .filter_map(|line| {
            let f: Vec<&str> = line.split(',').collect();
            Some((f[3].parse().ok()?, (f[1].parse().ok()?, f[2].parse().ok()?)))
        })
        .collect()
}

/// Per-(month, cell) maximum hazard index, month-major flatten order.
fn monthly_max_hazard(
    dims: GridDims,
    susc: &SusceptibilityMap,
    depths: &[f64],
    day_month: &[MonthKey],
) -> (Vec<MonthKey>, Vec<f64>) {
    let forcing = GriddedRain::new(dims, 24.0, depths.to_vec()).unwrap();
    let nowcast = Nowcast::new(
        susc.clone(),
        forcing,
        IdThreshold::new(A_INTERCEPT, B_EXPONENT).unwrap(),
        TriggerModel::default(),
        MAX_WINDOW_DAYS,
    )
    .unwrap();

    let mut months: Vec<MonthKey> = Vec::new();
    let mut seen: BTreeSet<MonthKey> = BTreeSet::new();
    for &mk in day_month {
        if seen.insert(mk) {
            months.push(mk);
        }
    }
    let month_idx = |mk: MonthKey| months.iter().position(|&x| x == mk).unwrap();

    let n = dims.len();
    let mut max_haz = vec![0.0_f64; months.len() * n];
    for (step, field) in nowcast.run().into_iter().enumerate() {
        let base = month_idx(day_month[step]) * n;
        for (cell, &p) in field.probability().iter().enumerate() {
            if p > max_haz[base + cell] {
                max_haz[base + cell] = p;
            }
        }
    }
    (months, max_haz)
}

/// Positive cell-months (event footprint), as flat indices `month*n + cell`.
fn positive_footprint(dims: GridDims, months: &[MonthKey], events: &[(usize, MonthKey)]) -> BTreeSet<usize> {
    let n = dims.len();
    let month_idx = |mk: MonthKey| months.iter().position(|&x| x == mk);
    let mut pos = BTreeSet::new();
    let (r, tol) = (CELL_RADIUS as i64, TOL_MONTHS as i32);
    for &(ec, (ey, em)) in events {
        let (er, ecol) = ((ec / dims.ncols) as i64, (ec % dims.ncols) as i64);
        for dr in -r..=r {
            for dc in -r..=r {
                let (nr, nc) = (er + dr, ecol + dc);
                if nr < 0 || nc < 0 || nr >= dims.nrows as i64 || nc >= dims.ncols as i64 {
                    continue;
                }
                let cell = nr as usize * dims.ncols + nc as usize;
                for d in -tol..=tol {
                    let z = ey * 12 + (em as i32 - 1) + d;
                    let mk = (z.div_euclid(12), (z.rem_euclid(12) + 1) as u32);
                    if let Some(mi) = month_idx(mk) {
                        pos.insert(mi * n + cell);
                    }
                }
            }
        }
    }
    pos
}

fn main() {
    let (dims, susc_vals) = read_grid();
    let (day_month, depths) = read_precip(dims);
    let events = read_events();
    let susc = SusceptibilityMap::new(dims, susc_vals).unwrap();
    let n = dims.len();

    let (months, haz) = monthly_max_hazard(dims, &susc, &depths, &day_month);
    let pos = positive_footprint(dims, &months, &events);

    // (index, outcome) per cell-month; split by year parity (odd = fit, even = test).
    let year_of = |i: usize| months[i / n].0;
    let (mut tr_s, mut tr_o, mut te_s, mut te_o) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for (i, &s) in haz.iter().enumerate() {
        let o = pos.contains(&i);
        if year_of(i) % 2 == 1 {
            tr_s.push(s);
            tr_o.push(o);
        } else {
            te_s.push(s);
            te_o.push(o);
        }
    }

    let base = te_o.iter().filter(|&&o| o).count() as f64 / te_o.len() as f64;
    println!(
        "Real Maipo calibration — {}×{} grid, {} cell-months ({} positive, base rate {:.4})",
        dims.ncols, dims.nrows, haz.len(), pos.len(), pos.len() as f64 / haz.len() as f64
    );
    println!(
        "fit on odd years ({} cell-months), test on even years ({} cell-months, base rate {:.4})\n",
        tr_s.len(), te_s.len(), base
    );

    let cal = Calibrator::fit_isotonic(&tr_s, &tr_o).unwrap();
    let cal_test = cal.calibrate(&te_s);

    let raw_rel = reliability(&te_s, &te_o, 6).unwrap();
    let cal_rel = reliability(&cal_test, &te_o, 6).unwrap();

    println!("                    Brier      skill      ECE");
    println!("  raw index       {:.5}   {:>+.3}   {:.4}", raw_rel.brier, raw_rel.brier_skill, raw_rel.ece);
    println!("  calibrated      {:.5}   {:>+.3}   {:.4}", cal_rel.brier, cal_rel.brier_skill, cal_rel.ece);
    println!(
        "  (calibrated Brier {:.5}; climatology Brier {:.5})\n",
        brier_score(&cal_test, &te_o),
        base * (1.0 - base)
    );

    println!("Calibrated probability vs observed frequency (Wilson 95% intervals):");
    println!("  pred     obs      95% CI              n");
    for b in &cal_rel.bins {
        println!(
            "  {:.4}  {:.4}   [{:.4}, {:.4}]   {:>6}",
            b.p_pred_mean, b.p_obs, b.ci_low, b.ci_high, b.n
        );
    }
    println!(
        "\nReading: the calibrated probabilities cluster near the base rate and the\n\
         reliability curve is nearly flat — at daily 5 km resolution the index barely\n\
         ranks event cell-months above quiet ones (the Section 5.2 null), so calibration\n\
         honestly recovers ~climatology rather than inventing skill. What it adds is a\n\
         probability with explicit uncertainty: the same machinery will express real\n\
         skill once the forcing resolution supplies it (Sections 5.3–5.6)."
    );
}
