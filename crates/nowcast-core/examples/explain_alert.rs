//! Why did the nowcast alert (or not)? Exact, closed-form attribution.
//!
//! The hazard is `susceptibility × trigger_factor`, so each cell's value
//! decomposes exactly — no surrogate model, no sampling. This example explains,
//! cell by cell, what drove the hazard at the storm peak, and answers the
//! operational counterfactual: how much rain would each cell need to reach the
//! alert level?
//!
//! Run with: `cargo run --example explain_alert`

use nowcast_core::{Driver, GridDims, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain};

fn main() {
    // A 3-cell slope transect: scarp (very susceptible) → bench → valley floor.
    let dims = GridDims::new(3, 1);
    let labels = ["escarpe (0,90)", "ladera (0,50)", "fondo  (0,15)"];
    let susceptibility = SusceptibilityMap::new(dims, vec![0.90, 0.50, 0.15]).unwrap();

    // An hourly rain episode building to a 28 mm/h hour.
    let rain = vec![0.0, 4.0, 12.0, 28.0, 6.0, 0.0];
    let forcing = UniformRain::new(dims, 1.0, rain).unwrap();

    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::caine(),
        TriggerModel::default(),
        24,
    )
    .unwrap();

    let alert_level = 0.5;
    let peak = 3; // the 28 mm/h hour

    println!("Attribution at the storm peak (step {peak}, 28 mm/h):\n");
    for (cell, label) in labels.iter().enumerate() {
        let ex = nowcast.explain(cell, peak).unwrap();
        let mark = if ex.hazard >= alert_level { "⚠ ALERTA" } else { "· bajo  " };
        let driver = match ex.driver {
            Driver::TriggerLimited => "gatillo-limitado",
            Driver::TerrainLimited => "terreno-limitado",
            Driver::Balanced => "balanceado",
        };
        println!(
            "{mark}  {label:<16} peligro {:.2}  =  susc {:.2} × gatillo {:.2}   [{driver}]",
            ex.hazard, ex.susceptibility, ex.trigger_factor
        );
        println!(
            "            ventana I–D dominante: {:.0} h · {:.1} mm/h (crítica {:.1}, E={:.2})",
            ex.critical_duration_h, ex.mean_intensity_mm_h, ex.critical_intensity_mm_h, ex.exceedance
        );
    }

    println!("\nContrafactual — intensidad de 1 h necesaria para alcanzar alerta {alert_level}:");
    for (cell, label) in labels.iter().enumerate() {
        match nowcast.intensity_to_alert(cell, alert_level, 1.0) {
            Some(i) => println!("  {label:<16} {i:.1} mm/h"),
            None => println!("  {label:<16} inalcanzable — susceptibilidad < {alert_level} (cap del terreno)"),
        }
    }

    println!("\nLínea de explicación (trazable para un boletín):");
    println!("  {}", nowcast.explain(0, peak).unwrap().summary());
}
