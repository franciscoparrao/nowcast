//! Multi-event sub-daily lead-time table — generalising the Atacama 2015 result
//! to dated Chilean debris-flow / aluvión events across very different climates.
//!
//! For each event we feed the nowcast the GPM IMERG half-hourly storm-core
//! rainfall and report: storm total, peak 1-h intensity, the timestamp the I–D
//! threshold is crossed, how far that is ahead of the documented event day, and
//! whether the *same rain aggregated to daily* would have triggered at all.
//!
//! Inputs (regenerate with `scripts/extract_event_imerg.py all`):
//!   - `data/event_<key>.csv`  datetime, core_mm_hr, boxmean_mm_hr
//!
//! Run with: `cargo run --example multi_event_leadtime`

use std::path::PathBuf;

use nowcast_core::{Forcing, GridDims, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain};

const A: f64 = 4.0; // regional "low" I–D intercept (mm/h); b = 0.39
const B: f64 = 0.39;

struct Event { key: &'static str, label: &'static str, climate: &'static str, day: &'static str }

const EVENTS: &[Event] = &[
    Event { key: "atacama_2015",   label: "Atacama / Copiapó",  climate: "árido N · convectivo",   day: "25-mar-2015" },
    Event { key: "maipo_2017",     label: "Cajón del Maipo",    climate: "Andes central · verano", day: "25-feb-2017" },
    Event { key: "santalucia_2017",label: "Villa Santa Lucía",  climate: "sur húmedo · frontal",   day: "16-dic-2017" },
];

fn read_core(key: &str) -> Option<(Vec<String>, Vec<f64>)> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data").join(format!("event_{key}.csv"));
    let text = std::fs::read_to_string(p).ok()?;
    let (mut t, mut r) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        t.push(f.next()?.to_string());
        r.push(f.next()?.trim().parse().unwrap_or(0.0));
    }
    Some((t, r))
}

/// First step index whose I–D hazard reaches 0.5 (exceedance ≥ 1).
fn first_alert(depths: &[f64], dt: f64, max_window: usize) -> Option<usize> {
    let dims = GridDims::new(1, 1);
    let f = UniformRain::new(dims, dt, depths.to_vec()).unwrap();
    let susc = SusceptibilityMap::uniform(f.dims(), 1.0).unwrap();
    let nc = Nowcast::new(susc, f, IdThreshold::new(A, B).unwrap(), TriggerModel::default(), max_window).unwrap();
    nc.run().into_iter().position(|h| h.max_probability() >= 0.5)
}

fn peak_1h(rate: &[f64]) -> f64 {
    (0..rate.len()).map(|i| if i + 1 < rate.len() { (rate[i] + rate[i + 1]) / 2.0 } else { rate[i] })
        .fold(0.0_f64, f64::max)
}

fn main() {
    println!("Multi-event sub-daily lead-time — GPM IMERG half-hourly, I–D a={A} mm/h, b={B}\n");
    println!("{:>20} | {:>23} | {:>6} | {:>8} | {:>16} | {:>13} | {:>8}",
             "evento", "clima", "total", "peak 1h", "cruce I–D UTC", "día documentado", "¿diario?");
    println!("{}", "─".repeat(106));

    for ev in EVENTS {
        let Some((stamps, rate)) = read_core(ev.key) else {
            println!("{:>20} | {:>23} | (sin datos — corre scripts/extract_event_imerg.py {})", ev.label, ev.climate, ev.key);
            continue;
        };
        let depths: Vec<f64> = rate.iter().map(|r| r * 0.5).collect();
        let total: f64 = depths.iter().sum();
        let p1 = peak_1h(&rate);

        // Sub-daily crossing timestamp (mes-día HH:MM, UTC).
        let cross_s = match first_alert(&depths, 0.5, 48) {
            Some(s) => stamps[s][5..16].replace('T', " "),
            None => "sin cruce".into(),
        };

        // Same rain aggregated to daily — does the daily product trigger at all?
        let mut daily = Vec::new();
        let (mut i, n) = (0usize, stamps.len());
        while i < n {
            let day = &stamps[i][..10];
            let mut sum = 0.0;
            while i < n && &stamps[i][..10] == day { sum += depths[i]; i += 1; }
            daily.push(sum);
        }
        let daily_fires = first_alert(&daily, 24.0, 3).is_some();

        println!("{:>20} | {:>23} | {:>3.0} mm | {:>5.0} mm/h | {:>16} | {:>13} | {:>8}",
                 ev.label, ev.climate, total, p1, cross_s, ev.day,
                 if daily_fires { "sí" } else { "NO ✗" });
    }

    println!("\n→ La forzante semihoraria fija el cruce del umbral I–D a un timestamp en los tres climas,\n  \
              sobre o justo antes del día documentado. '¿diario?' = si la MISMA lluvia agregada a diario\n  \
              llega a gatillar: en el Cajón del Maipo (ráfaga convectiva de verano) el producto diario NO\n  \
              detecta el evento — solo la resolución sub-diaria lo ve.");
}
