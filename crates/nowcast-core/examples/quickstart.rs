//! Decoupled v0.1 workflow: an observed rain-gauge series modulating a small
//! susceptibility raster into a time-varying hazard, with threshold alerts.
//!
//! Run with: `cargo run --example quickstart`
//!
//! This is the "vía rápida" from the project brief — it validates the
//! susceptibility × trigger logic with no dependency on the upstream Rust
//! engines (rainflow / snowmelt-rs), which plug in as `Forcing` providers later.

use nowcast_core::{
    GridDims, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain,
};

fn main() -> nowcast_core::Result<()> {
    // A 3x2 slope tile: susceptibility from an upstream static model (Smelt /
    // external ML). Values in [0, 1], row-major.
    let dims = GridDims::new(3, 2);
    let susceptibility = SusceptibilityMap::new(
        dims,
        vec![
            0.10, 0.35, 0.80, // upper row
            0.55, 0.90, 0.20, // lower row
        ],
    )?;

    // An observed hourly rain episode (mm/step) from a CR2/DGA-style gauge,
    // here written inline as CSV with a header to exercise the parser.
    let csv = "\
date,rain_mm
2026-06-15T00:00,0.0
2026-06-15T01:00,2.0
2026-06-15T02:00,8.0
2026-06-15T03:00,22.0
2026-06-15T04:00,30.0
2026-06-15T05:00,5.0
2026-06-15T06:00,0.0
2026-06-15T07:00,0.0
";
    let dt_hours = 1.0;
    let forcing = UniformRain::from_csv(csv, 1, dims, dt_hours)?;

    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::caine(),   // global I = 14.82 D^-0.39 default
        TriggerModel::default(), // logistic, k = 4
        24,                      // rolling I-D windows up to 24 h
    )?;

    println!("Hourly hazard nowcast (peak over the {} cells):", dims.len());
    println!("{:>4}  {:>10}  {:>8}", "step", "peak_haz", "n_alert");
    let alert_level = 0.5;
    for field in nowcast.run() {
        let n_alert = field
            .probability()
            .iter()
            .filter(|&&p| p >= alert_level)
            .count();
        println!(
            "{:>4}  {:>10.3}  {:>8}",
            field.step,
            field.max_probability(),
            n_alert
        );
    }

    println!("\nAlerts at level {alert_level}:");
    for a in nowcast.alerts(alert_level)? {
        println!(
            "  step {:>2}: {} cell(s) ({:.0}% of grid), peak hazard {:.3}",
            a.step,
            a.n_cells,
            a.fraction * 100.0,
            a.max_probability
        );
    }

    Ok(())
}
