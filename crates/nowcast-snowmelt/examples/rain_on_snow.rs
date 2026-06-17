//! What distributed snowmelt forcing buys over the v0.1 single gauge:
//! **rain-on-snow amplification** and **spatially varying** water input.
//!
//! Run with: `cargo run -p nowcast-snowmelt --example rain_on_snow`
//!
//! Same warm storm, two antecedent states on the same DEM:
//!   A) bare ground  → water input = rain only.
//!   B) snow on the ground → water input = rain + melt (the storm both rains and
//!      melts the pack), so more water reaches the slope and the hazard is
//!      higher — and it varies cell to cell because melt follows the lapse-rate
//!      temperature field down the DEM.

use ndarray::Array2;
use nowcast_core::{Forcing, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};
use nowcast_snowmelt::{Dem, DegreeDayParams, MeteoForcing, SnowModel, SnowmeltForcing};

const ROWS: usize = 1;
const COLS: usize = 6; // an elevation transect, 1500 m … 2500 m

fn dem() -> Dem {
    Dem::new(Array2::from_shape_fn((ROWS, COLS), |(_, j)| {
        1500.0 + 200.0 * j as f64
    }))
    .unwrap()
}

/// Build the forcing for a warm storm, optionally preceded by cold snowy days.
fn forcing(with_snowpack: bool) -> SnowmeltForcing {
    let model = SnowModel::new(dem(), DegreeDayParams::default()).unwrap();
    let mut series = Vec::new();
    if with_snowpack {
        // Three cold snowy days to build a pack across the transect.
        for _ in 0..3 {
            series.push(MeteoForcing::Uniform {
                t_ref: -6.0,
                z_ref: 1500.0,
                precip: 40.0,
            });
        }
    }
    // The warm storm day (same for both scenarios).
    series.push(MeteoForcing::Uniform {
        t_ref: 9.0,
        z_ref: 1500.0,
        precip: 50.0,
    });
    SnowmeltForcing::run(model, &series, 1.0).unwrap()
}

fn peak_hazard(f: SnowmeltForcing, storm_step: usize) -> (f64, Vec<f64>) {
    let dims = f.dims();
    // Higher susceptibility at lower elevations (toe of the transect).
    let susc = SusceptibilityMap::new(
        dims,
        (0..dims.len()).map(|c| 0.9 - 0.1 * c as f64).collect(),
    )
    .unwrap();
    let storm_runoff = f.runoff_at(storm_step).to_vec();
    let nowcast = Nowcast::new(
        susc,
        f,
        IdThreshold::new(4.0, 0.39).unwrap(),
        TriggerModel::default(),
        7,
    )
    .unwrap();
    (nowcast.hazard_at(storm_step).max_probability(), storm_runoff)
}

fn main() {
    let a = forcing(false); // bare ground; storm is the only (step 0) day
    let b = forcing(true); //  snowpack first; storm is step 3

    let (haz_a, runoff_a) = peak_hazard(a, 0);
    let (haz_b, runoff_b) = peak_hazard(b, 3);

    let elevations: Vec<f64> = (0..COLS).map(|j| 1500.0 + 200.0 * j as f64).collect();

    println!("Rain-on-snow amplification — same 50 mm warm storm\n");
    println!(
        "{:>9} | {:>10} | {:>14}",
        "elev (m)", "A rain (mm)", "B rain+melt (mm)"
    );
    for c in 0..COLS {
        println!(
            "{:>9.0} | {:>10.1} | {:>14.1}",
            elevations[c], runoff_a[c], runoff_b[c]
        );
    }

    println!("\nWater input on the storm day (basin mean):");
    let mean = |v: &[f64]| v.iter().sum::<f64>() / v.len() as f64;
    println!("  A bare ground : {:.1} mm", mean(&runoff_a));
    println!(
        "  B rain-on-snow: {:.1} mm  (+{:.0}% from melt)",
        mean(&runoff_b),
        100.0 * (mean(&runoff_b) / mean(&runoff_a) - 1.0)
    );

    println!("\nPeak hazard on the storm day:");
    println!("  A bare ground : {haz_a:.3}");
    println!("  B rain-on-snow: {haz_b:.3}");
    println!(
        "\n→ v0.1's single gauge sees only the {:.0} mm of rain; the distributed",
        mean(&runoff_a)
    );
    println!("  snowmelt provider adds the melt contribution, cell by cell.");
}
