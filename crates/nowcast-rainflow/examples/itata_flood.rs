//! Flood nowcast driven by a real rainflow-simulated hydrograph.
//!
//! Catchment: Río Itata en Cholguán (CAMELS-CL gauge 8123001) — a flashy
//! pluvial basin, exactly the kind that floods. We run rainflow's GR4J on its
//! daily precip/PET (CR2MET / Hargreaves, 1979–2016), set a flood threshold as a
//! high quantile of the simulated discharge, and nowcast flood hazard over a
//! small flood-exposure transect (floodplain → terrace → upland).
//!
//! Run with:
//!   cargo run -p nowcast-rainflow --example itata_flood
//!
//! Data path is absolute (rainflow's bundled CAMELS-CL sample); GR4J parameters
//! here are illustrative, not calibrated — the point is the end-to-end path
//! rainflow → discharge threshold → flood hazard, not a validated hydrograph.

use std::path::PathBuf;

use nowcast_core::{GridDims, SusceptibilityMap, TriggerModel};
use nowcast_rainflow::{FloodNowcast, FloodThreshold, RainflowForcing};
use rainflow_core::Gr4jParams;

const CAMELS: &str = "~/proyectos/rainflow/data/camels-cl/8123001.csv";
const QUANTILE: f64 = 0.98; // flood threshold = 98th percentile of discharge
const ALERT_LEVEL: f64 = 0.5;

fn read_camels() -> (Vec<String>, Vec<f64>, Vec<f64>) {
    let path = PathBuf::from(CAMELS.replacen('~', env!("HOME"), 1));
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    let (mut dates, mut p, mut pet) = (Vec::new(), Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let (Some(d), Some(pp), Some(pe)) = (f.next(), f.next(), f.next()) else {
            continue;
        };
        // precip/PET are complete; skip the rare malformed row defensively.
        let (Ok(pp), Ok(pe)) = (pp.trim().parse::<f64>(), pe.trim().parse::<f64>()) else {
            continue;
        };
        dates.push(d.to_string());
        p.push(pp);
        pet.push(pe);
    }
    (dates, p, pet)
}

fn main() {
    let (dates, precip, pet) = read_camels();
    let dims = GridDims::new(3, 1); // floodplain / terrace / upland transect

    // rainflow GR4J → discharge hydrograph (illustrative params).
    let params = Gr4jParams {
        x1: 350.0,
        x2: 0.0,
        x3: 90.0,
        x4: 1.5,
    };
    let forcing = RainflowForcing::gr4j(dims, 1.0, params, &precip, &pet).unwrap();
    let q = forcing.discharge();
    let q_max = q.iter().copied().fold(0.0, f64::max);
    let q_mean = q.iter().sum::<f64>() / q.len() as f64;

    let threshold = FloodThreshold::quantile(q, QUANTILE).unwrap();

    // Flood exposure: high on the floodplain, low on the upland.
    let susceptibility = SusceptibilityMap::new(dims, vec![0.9, 0.5, 0.1]).unwrap();
    let nowcast = FloodNowcast::from_rainflow(
        susceptibility,
        &forcing,
        threshold,
        TriggerModel::default(),
    )
    .unwrap();

    let alerts = nowcast.alerts(ALERT_LEVEL);
    println!("Itata (8123001) flood nowcast — {} days {}–{}", q.len(), &dates[0][..4], &dates[dates.len() - 1][..4]);
    println!(
        "Simulated discharge: mean {q_mean:.2} mm/day, max {q_max:.1} mm/day; flood threshold Q_c(p={QUANTILE}) = {:.1} mm/day",
        threshold.q_crit
    );
    println!(
        "Flood-alert days (peak hazard ≥ {ALERT_LEVEL}): {} ({:.1}% of record)\n",
        alerts.len(),
        100.0 * alerts.len() as f64 / q.len() as f64
    );

    // The five largest discharge days and their flood hazard on the floodplain.
    let mut idx: Vec<usize> = (0..q.len()).collect();
    idx.sort_by(|&a, &b| q[b].partial_cmp(&q[a]).unwrap());
    println!("Largest discharge events:");
    println!("{:>12} | {:>9} | {:>10} | {:>10}", "date", "Q mm/day", "Q/Q_c", "floodplain");
    for &i in idx.iter().take(5) {
        let field = nowcast.hazard_at(i);
        println!(
            "{:>12} | {:>9.1} | {:>10.2} | {:>10.3}",
            dates[i],
            q[i],
            threshold.exceedance(q[i]),
            field.probability()[0], // floodplain cell
        );
    }
}
