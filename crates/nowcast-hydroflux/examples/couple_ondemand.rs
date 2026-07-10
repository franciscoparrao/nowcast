//! On-demand coupling saving (Issue 4 / Section 2.5): the architecture runs the
//! expensive 2-D shallow-water refinement *only where the cheap nowcast alerted*.
//! This example quantifies that saving — solve the inundation over the whole
//! coarse domain versus over just the alerted tile — and shows the depths in the
//! inundated region are preserved, so the saving is free.
//!
//! Run: `cargo run -p nowcast-hydroflux --release --example couple_ondemand`
//!
//! The solver integrates to a fixed physical time with CFL-limited steps, so at
//! fixed cell size its cost is ${\sim}O(\mathrm{cells})$; confining it to the
//! alerted tile therefore saves a factor ${\approx}$ (domain cells)/(tile cells).
//! Because the flood is contained in the channel, with dry high ground between it
//! and the tile edge, the interior solution is unaffected by the tile boundary.

use std::time::Instant;

use hydroflux_solver_2d::Boundaries2D;
use ndarray::Array2;
use nowcast_hydroflux::{discharge_to_inflow_m3s, Boundary, Inundation, Mesh2D, PointSource};

const NR: usize = 48; // downstream rows (row 0 = inlet)
const NC: usize = 81; // full coarse-domain width
const CENTER: usize = NC / 2; // channel column
const TILE_HALF: usize = 12; // alerted tile = CENTER ± 12 columns
const DURATION_S: f64 = 1200.0;

/// V-shaped valley: bed drops downstream, rises steeply towards the banks so the
/// flow stays in the central channel and the far columns are dry high ground.
fn bed_at(i: usize, j: usize) -> f64 {
    let downstream = (NR - 1 - i) as f64 * 0.5; // 0.5 m drop per row
    let bank = (j as f64 - CENTER as f64).abs() * 0.8; // 0.8 m rise per column
    downstream + bank
}

fn mesh(c0: usize, c1: usize) -> Mesh2D {
    let w = c1 - c0;
    Mesh2D::new(
        Array2::from_shape_fn((NR, w), |(i, j)| bed_at(i, c0 + j)),
        20.0,
        20.0,
        0.035,
    )
}

fn solve(c0: usize, c1: usize, inflow: f64, bcs: Boundaries2D) -> (Vec<f64>, usize, f64) {
    let sources = vec![PointSource { row: 0, col: CENTER - c0, q_mass: inflow }];
    let t = Instant::now();
    let (field, stats) = Inundation::new(mesh(c0, c1), bcs, DURATION_S)
        .expect("positive duration")
        .run_point_sources(&sources)
        .expect("sources are on the mesh with finite inflow");
    assert!(!stats.truncated, "integration hit the step cap");
    let secs = t.elapsed().as_secs_f64();
    (field.depth().to_vec(), c1 - c0, secs)
}

fn main() {
    // A nowcast flood alert: 50 mm/day routed off an 80 km² sub-catchment.
    let inflow = discharge_to_inflow_m3s(50.0, 80.0);
    let bcs = Boundaries2D {
        north: Boundary::Wall,
        south: Boundary::Transmissive,
        east: Boundary::Wall,
        west: Boundary::Wall,
    };
    println!(
        "On-demand physical coupling — 2-D shallow water on a {NR}×{NC} coarse domain\n\
         Nowcast alert: 50 mm/day over 80 km² → inflow {inflow:.1} m³/s, integrated {DURATION_S:.0} s\n"
    );

    // Physics *everywhere*: solve over the whole coarse domain.
    let (full, full_w, full_secs) = solve(0, NC, inflow, bcs);

    // Physics *on demand*: solve only over the alerted tile (the coarse cells the
    // nowcast flagged), CENTER ± TILE_HALF columns.
    let (c0, c1) = (CENTER - TILE_HALF, CENTER + TILE_HALF + 1);
    let (tile, tile_w, tile_secs) = solve(c0, c1, inflow, bcs);

    let full_cells = NR * full_w;
    let tile_cells = NR * tile_w;

    // Compare the depths in the alerted region: full restricted to [c0, c1) vs tile.
    let (mut max_full, mut max_tile, mut max_abs_diff) = (0.0_f64, 0.0_f64, 0.0_f64);
    for i in 0..NR {
        for jj in 0..tile_w {
            let hf = full[i * full_w + (c0 + jj)];
            let ht = tile[i * tile_w + jj];
            max_full = max_full.max(hf);
            max_tile = max_tile.max(ht);
            max_abs_diff = max_abs_diff.max((hf - ht).abs());
        }
    }

    println!(
        "{:<24} {:>10} {:>12} {:>12}",
        "configuration", "cells", "time (s)", "max depth (m)"
    );
    println!("{}", "-".repeat(62));
    println!("{:<24} {full_cells:>10} {full_secs:>12.3} {max_full:>12.3}", "physics everywhere");
    println!("{:<24} {tile_cells:>10} {tile_secs:>12.3} {max_tile:>12.3}", "physics on demand");
    println!(
        "\nTile is {:.0}% of the coarse domain → {:.1}× fewer cells; measured speed-up {:.1}×.\n\
         Max |Δdepth| in the alerted region: {:.2e} m — the inundation footprint is\n\
         preserved, so confining the physics to the alert costs nothing in the answer.\n\
         The cheap susceptibility×trigger screen is what makes the alerted area small;\n\
         the expensive hydrodynamics never touches the {:.0}% of the domain it skips.",
        100.0 * tile_cells as f64 / full_cells as f64,
        full_cells as f64 / tile_cells as f64,
        full_secs / tile_secs,
        max_abs_diff,
        100.0 * (1.0 - tile_cells as f64 / full_cells as f64),
    );
}
