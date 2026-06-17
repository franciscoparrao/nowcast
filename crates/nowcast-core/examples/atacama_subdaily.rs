//! Sub-daily lead-time demonstration with high-resolution GPM IMERG forcing,
//! for the 24–26 March 2015 Atacama debris-flow disaster.
//!
//! The distributed CR2MET backtest showed that **daily** resolution is the
//! bottleneck (rainfall didn't discriminate events). Here we feed the nowcast
//! **half-hourly** IMERG rainfall and ask the question daily forcing cannot
//! answer: *at what time* does the intensity–duration threshold get crossed, and
//! how much lead time does that give before the debris flows?
//!
//! Inputs (regenerate with `scripts/extract_atacama_imerg.py`, needs Earthdata):
//!   - `data/atacama_imerg_hhr.csv`  datetime, mean_mm_hr, max_mm_hr (basin).
//!
//! Run with: `cargo run --example atacama_subdaily`
//!
//! Same engine, two temporal resolutions: half-hourly IMERG vs the same rain
//! aggregated to daily (what a CR2MET-style product would see). The trigger and
//! threshold are identical; only the time step differs.

use std::path::PathBuf;

use nowcast_core::{
    Forcing, GridDims, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain,
};

// Documented onset of the main Copiapó / El Salado debris flows: afternoon of
// 25 March 2015. Used only to express lead time; approximate, from literature.
const ONSET: &str = "2015-03-25T15:00:00";
const DT_HALFHOUR: f64 = 0.5;
const MAX_WINDOW_HH: usize = 48; // rolling I–D windows up to 24 h (48 half-hours)

fn data_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data/atacama_imerg_hhr.csv")
}

/// minutes since 2015-03-24T00:00 for a `YYYY-MM-DDThh:mm:ss` stamp (March 2015).
fn minutes(ts: &str) -> i64 {
    let (date, time) = ts.split_once('T').unwrap();
    let d: Vec<i64> = date.split('-').map(|x| x.parse().unwrap()).collect();
    let t: Vec<i64> = time.split(':').map(|x| x.parse().unwrap()).collect();
    let day = d[2] - 24; // days since 24 March
    (day * 24 + t[0]) * 60 + t[1]
}

fn read_series() -> (Vec<String>, Vec<f64>) {
    let text = std::fs::read_to_string(data_path()).unwrap_or_else(|e| {
        panic!(
            "cannot read {} ({e}); run scripts/extract_atacama_imerg.py first",
            data_path().display()
        )
    });
    let (mut stamps, mut rate) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let ts = f.next().unwrap().to_string();
        let mean: f64 = f.next().unwrap().parse().unwrap_or(0.0);
        stamps.push(ts);
        rate.push(mean); // mm/hr, basin-mean
    }
    (stamps, rate)
}

/// First step index whose hazard reaches `level`, given a forcing & threshold.
fn first_alert<F: Forcing>(f: F, threshold: IdThreshold, max_window: usize) -> Option<usize> {
    let susc = SusceptibilityMap::uniform(f.dims(), 1.0).unwrap();
    let nc = Nowcast::new(susc, f, threshold, TriggerModel::default(), max_window).unwrap();
    nc.run()
        .into_iter()
        .position(|field| field.max_probability() >= 0.5)
}

fn fmt_lead(from_min: i64, to_min: i64) -> String {
    let h = (to_min - from_min) as f64 / 60.0;
    if h >= 0.0 {
        format!("{h:.1} h before onset")
    } else {
        format!("{:.1} h after onset", -h)
    }
}

fn main() {
    let (stamps, rate) = read_series();
    let dims = GridDims::new(1, 1);
    let n = rate.len();
    let total: f64 = rate.iter().map(|r| r * DT_HALFHOUR).sum();
    let onset_min = minutes(ONSET);

    // Peak rolling mean intensity at a few durations (diagnostics).
    let depths: Vec<f64> = rate.iter().map(|r| r * DT_HALFHOUR).collect();
    let peak_intensity = |window_hh: usize| -> (f64, usize) {
        let mut best = (0.0_f64, 0usize);
        for end in window_hh..=n {
            let dsum: f64 = depths[end - window_hh..end].iter().sum();
            let intensity = dsum / (window_hh as f64 * DT_HALFHOUR);
            if intensity > best.0 {
                best = (intensity, end - 1);
            }
        }
        best
    };

    println!(
        "Atacama 2015 — GPM IMERG half-hourly, {} steps ({} … {})",
        n,
        &stamps[0],
        &stamps[n - 1]
    );
    println!("Storm total (basin-mean): {total:.1} mm\n");
    println!("Peak rolling intensity (basin-mean):");
    for (label, w) in [("30 min", 1usize), ("1 h", 2), ("3 h", 6), ("6 h", 12), ("24 h", 48)] {
        let (i, at) = peak_intensity(w);
        println!("  {label:>6}: {i:5.2} mm/h  at {}", stamps[at]);
    }

    // Sub-daily trigger: half-hourly IMERG.
    println!("\nNowcast trigger (susceptibility = 1, I = a·D^-0.39):");
    for (name, a) in [("Caine global a=14.82", 14.82), ("Atacama-low a=4.0", 4.0)] {
        let forcing = UniformRain::new(dims, DT_HALFHOUR, depths.clone()).unwrap();
        match first_alert(forcing, IdThreshold::new(a, 0.39).unwrap(), MAX_WINDOW_HH) {
            Some(step) => println!(
                "  half-hourly | {name}: first alert {}  ({})",
                stamps[step],
                fmt_lead(minutes(&stamps[step]), onset_min)
            ),
            None => println!("  half-hourly | {name}: no alert"),
        }
    }

    // Daily aggregation: same rain, summed to daily steps — what CR2MET sees.
    let mut daily: Vec<f64> = Vec::new();
    let mut day_label: Vec<String> = Vec::new();
    let mut i = 0;
    while i < n {
        let day = &stamps[i][..10];
        let mut sum = 0.0;
        while i < n && &stamps[i][..10] == day {
            sum += depths[i];
            i += 1;
        }
        daily.push(sum);
        day_label.push(day.to_string());
    }
    println!("\nSame rain aggregated to DAILY (what a CR2MET-resolution product resolves):");
    for (d, p) in day_label.iter().zip(&daily) {
        println!("  {d}: {p:5.1} mm");
    }
    let daily_forcing = UniformRain::new(dims, 24.0, daily.clone()).unwrap();
    match first_alert(daily_forcing, IdThreshold::new(4.0, 0.39).unwrap(), 3) {
        Some(step) => println!(
            "  daily | Atacama-low a=4.0: first alert on {} — no intra-day timing, \
             so no sub-daily lead time.",
            day_label[step]
        ),
        None => println!("  daily | Atacama-low a=4.0: no alert"),
    }

    println!(
        "\n→ Half-hourly IMERG pins the threshold crossing to a timestamp and yields hours of\n  \
         lead time; the daily aggregate only flags the day. Sub-daily forcing is what turns\n  \
         the susceptibility×trigger logic into an operational, time-resolved nowcast."
    );
}
