//! Physical refinement of a flood alert: from a nowcast discharge to an
//! inundation depth field via the Hydroflux 2D shallow-water solver.
//!
//! The nowcast (e.g. `nowcast-rainflow`) says "flood alert, Q over threshold" on
//! a coarse cell. Here we take that discharge, convert it to a volumetric inflow,
//! and route it over the local DEM of a small valley to see *where* the water
//! actually goes — concentrated in the channel, sparing the banks — and downscale
//! the coarse alert probability onto that physical footprint.
//!
//! Run with: `cargo run -p nowcast-hydroflux --example couple_flood`

use ndarray::Array2;
use nowcast_hydroflux::{
    discharge_to_inflow_m3s, Boundary, Inundation, Mesh2D, PointSource,
};
use hydroflux_solver_2d::{Boundaries2D, Side};

const NR: usize = 24; // downstream rows (row 0 = inlet)
const NC: usize = 11; // cross-section columns
const CENTER: usize = NC / 2;

/// A V-shaped valley: bed drops downstream and rises towards the banks.
fn valley() -> Mesh2D {
    let bed = Array2::from_shape_fn((NR, NC), |(i, j)| {
        let downstream = (NR - 1 - i) as f64 * 0.5; // 0.5 m drop per row
        let bank = (j as f64 - CENTER as f64).abs() * 0.8; // 0.8 m rise per column
        downstream + bank
    });
    Mesh2D::new(bed, 20.0, 20.0, 0.035) // 20 m cells, Manning 0.035
}

fn main() {
    // A nowcast flood alert: 40 mm/day routed off a 50 km² sub-catchment.
    let q_mm_day = 40.0;
    let area_km2 = 50.0;
    let nowcast_prob = 0.7;
    let inflow = discharge_to_inflow_m3s(q_mm_day, area_km2);
    println!(
        "Nowcast flood alert: {q_mm_day} mm/día sobre {area_km2} km² → inflow {inflow:.1} m³/s (prob {nowcast_prob})\n"
    );

    // Inject the inflow at the channel head; let it drain out the downstream side.
    let bcs = Boundaries2D {
        north: Boundary::Wall,
        south: Boundary::Transmissive,
        east: Boundary::Wall,
        west: Boundary::Wall,
    };
    let _ = Side::North; // (Side re-exported for custom BC wiring)
    let sources = vec![PointSource { row: 0, col: CENTER, q_mass: inflow }];

    let (field, stats) = Inundation::new(valley(), bcs, 1200.0)
        .expect("positive duration")
        .run_point_sources(&sources);
    assert!(!stats.truncated, "integration hit the step cap");

    println!(
        "Inundación física: profundidad máx {:.2} m · media {:.3} m · {:.0}% del valle inundado\n",
        field.max_depth(),
        field.mean_depth(),
        100.0 * field.inundated_fraction(0.05)
    );

    // Cross-section at mid-reach: water in the channel, dry banks.
    let row = NR / 2;
    println!("Sección transversal (fila {row}, ▏=0,1 m de agua):");
    let d = field.depth();
    for j in 0..NC {
        let h = d[row * NC + j];
        let bar = "▏".repeat((h / 0.1).round() as usize);
        let tag = if j == CENTER { "canal" } else { "banca" };
        println!("  col {j:>2} [{tag}]  {h:>5.2} m  {bar}");
    }

    // Downscale the coarse alert onto the physical footprint.
    let refined = field.refined_hazard(0, nowcast_prob, 0.05).unwrap();
    let flagged = refined.probability().iter().filter(|&&p| p > 0.0).count();
    println!(
        "\nPeligro refinado: la probabilidad {nowcast_prob} se concentra en {flagged}/{} celdas\n  \
         realmente inundadas — el resto del polígono de alerta queda en 0. La física localiza el riesgo.",
        refined.probability().len()
    );
}
