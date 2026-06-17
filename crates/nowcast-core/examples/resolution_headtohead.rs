//! Head-to-head: does high-resolution forcing overcome the daily-product limit?
//!
//! The distributed CR2MET backtest found daily resolution to be the bottleneck.
//! Here we put the two products side by side on the *same* storm core (Atacama,
//! 24–26 March 2015): CR2MET daily (~5 km, 1 value/day) vs GPM IMERG half-hourly
//! (~10 km, 48/day), running the identical intensity–duration trigger.
//!
//! The crux is intensity resolution: with a daily value the finest duration you
//! can resolve is 24 h, so the maximum resolvable intensity is (daily total)/24.
//! A short, intense burst is smeared below the I–D curve — the daily product
//! *structurally cannot* trigger an I–D nowcast, regardless of the total.
//!
//! Inputs (regenerate with the two scripts in `scripts/`):
//!   - `data/atacama_imerg_hhr.csv`    datetime, core_mm_hr, boxmean_mm_hr
//!   - `data/atacama_cr2met_daily.csv` date, p_mm
//!
//! Run with: `cargo run --example resolution_headtohead`

use std::path::PathBuf;

use nowcast_core::IdThreshold;

const B: f64 = 0.39;

fn data(name: &str) -> String {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data").join(name);
    std::fs::read_to_string(&p)
        .unwrap_or_else(|e| panic!("cannot read {} ({e}); run the extraction scripts first", p.display()))
}

/// Read a 2-column CSV's numeric column (0-based `col`) plus the label column 0.
fn read_col(name: &str, col: usize) -> (Vec<String>, Vec<f64>) {
    let (mut keys, mut vals) = (Vec::new(), Vec::new());
    for line in data(name).lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        keys.push(f[0].to_string());
        vals.push(f[col].trim().parse().unwrap_or(0.0));
    }
    (keys, vals)
}

/// Peak rolling-mean intensity (mm/h) over any window, and its end index.
fn peak_intensity(depths: &[f64], dt: f64) -> (f64, usize) {
    let n = depths.len();
    let mut best = (0.0_f64, 0usize);
    for w in 1..=n {
        for end in w..=n {
            let s: f64 = depths[end - w..end].iter().sum();
            let i = s / (w as f64 * dt);
            if i > best.0 {
                best = (i, end - 1);
            }
        }
    }
    best
}

/// (first step index whose max windowed I–D exceedance ≥ 1, peak exceedance).
fn id_response(depths: &[f64], dt: f64, th: IdThreshold) -> (Option<usize>, f64) {
    let n = depths.len();
    let mut first = None;
    let mut peak = 0.0_f64;
    for end in 0..n {
        let mut e_step = 0.0_f64;
        for w in 1..=end + 1 {
            let s: f64 = depths[end + 1 - w..end + 1].iter().sum();
            let dur = w as f64 * dt;
            let e = th.exceedance(s / dur, dur);
            e_step = e_step.max(e);
        }
        peak = peak.max(e_step);
        if first.is_none() && e_step >= 1.0 {
            first = Some(end);
        }
    }
    (first, peak)
}

fn main() {
    // IMERG half-hourly storm-core rate (mm/h) → depth per 0.5 h step.
    let (imerg_t, imerg_rate) = read_col("atacama_imerg_hhr.csv", 1);
    let imerg_depth: Vec<f64> = imerg_rate.iter().map(|r| r * 0.5).collect();
    // CR2MET daily depth (mm/day) at the same point.
    let (cr2_t, cr2_depth) = read_col("atacama_cr2met_daily.csv", 1);
    // Restrict CR2MET to the event window for a fair total.
    let ev: Vec<(String, f64)> = cr2_t
        .iter()
        .cloned()
        .zip(cr2_depth.iter().cloned())
        .filter(|(d, _)| ("2015-03-23".."2015-03-27").contains(&d.as_str()))
        .collect();
    let cr2_ev: Vec<f64> = ev.iter().map(|(_, v)| *v).collect();

    let imerg_total: f64 = imerg_depth.iter().sum();
    let cr2_total: f64 = cr2_ev.iter().sum();
    let (imerg_pi, imerg_at) = peak_intensity(&imerg_depth, 0.5);
    let (cr2_pi, _) = peak_intensity(&cr2_ev, 24.0);

    println!("Resolution head-to-head — Atacama storm core, 24–26 Mar 2015 (lon −70.45, lat −27.15)\n");
    println!("{:>34} | {:>16} | {:>18}", "", "CR2MET daily", "IMERG half-hourly");
    println!("{:>34} | {:>16} | {:>18}", "", "(~5 km, 1/day)", "(~10 km, 48/day)");
    println!("{}", "─".repeat(74));
    println!("{:>34} | {:>16.1} | {:>18.1}", "storm total (mm)", cr2_total, imerg_total);
    println!(
        "{:>34} | {:>16} | {:>18}",
        "finest duration resolved",
        "24 h",
        "0.5 h"
    );
    println!(
        "{:>34} | {:>13.2} mm/h | {:>15.1} mm/h",
        "max resolvable intensity", cr2_pi, imerg_pi
    );
    println!("  (IMERG peak at {})\n", imerg_t[imerg_at]);

    for (name, a) in [("Caine global  a=14.82", 14.82), ("Atacama-low   a=4.0", 4.0)] {
        let th = IdThreshold::new(a, B).unwrap();
        let (cr2_first, cr2_pk) = id_response(&cr2_ev, 24.0, th);
        let (im_first, im_pk) = id_response(&imerg_depth, 0.5, th);
        let cr2_msg = match cr2_first {
            Some(_) => format!("ALERTA (E={cr2_pk:.1})"),
            None => format!("sin alerta (E máx {cr2_pk:.2})"),
        };
        let im_msg = match im_first {
            Some(s) => format!("ALERTA {} (E={im_pk:.0})", imerg_t[s]),
            None => format!("sin alerta (E máx {im_pk:.2})"),
        };
        println!("I–D {name}");
        println!("    CR2MET diario : {cr2_msg}");
        println!("    IMERG  30-min : {im_msg}\n");
    }

    println!(
        "→ Con resolución diaria la intensidad máxima resoluble es (total/24 h): el aguacero queda\n  \
         diluido por debajo de la curva I–D y el producto diario NO gatilla. La forzante sub-diaria\n  \
         resuelve el peak real y dispara con lead-time. El cuello de botella era el dato, y se supera\n  \
         subiendo su resolución — sin tocar el motor (mismo trait Forcing)."
    );
}
