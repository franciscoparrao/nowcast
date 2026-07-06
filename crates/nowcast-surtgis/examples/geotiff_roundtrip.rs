//! End-to-end through the SurtGIS bridge: read a real susceptibility raster,
//! and run a small nowcast whose hazard is written back out as a georeferenced
//! GeoTIFF.
//!
//! Run with: `cargo run -p nowcast-surtgis --example geotiff_roundtrip`
//!
//! Part 1 proves the *read* bridge on a real 30 m RandomForest susceptibility
//! raster of the Río Maipo basin (if present). Part 2 runs the full
//! in → engine → out path on a small synthetic grid and writes a hazard GeoTIFF
//! you can open in QGIS. (The core uses per-cell prefix sums, so drive coarse
//! grids — match the forcing resolution — rather than 30 M-cell rasters directly.)

use std::path::Path;

use nowcast_core::{GridDims, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain};
use nowcast_surtgis::{
    Georef, Raster, GeoTransform, susceptibility_from_raster, write_hazard_geotiff,
};
use surtgis_core::io::read_geotiff;

const MAIPO_SUSC: &str =
    "/home/franciscoparrao/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif";

fn part1_read_real() {
    println!("── Part 1 · read real susceptibility raster ──");
    match read_geotiff::<f32, _>(MAIPO_SUSC, Some(0)) {
        Ok(raster) => {
            let (rows, cols) = raster.shape();
            let (mut lo, mut hi, mut n) = (f32::INFINITY, f32::NEG_INFINITY, 0usize);
            for &v in raster.data().iter() {
                if v.is_finite() {
                    lo = lo.min(v);
                    hi = hi.max(v);
                    n += 1;
                }
            }
            println!("  Río Maipo RandomForest: {rows}×{cols} = {} celdas", rows * cols);
            println!("  susceptibilidad válida: {n} celdas, rango [{lo:.3}, {hi:.3}]");
            println!("  georref: {:?}", raster.transform());
            println!("  → susceptibility_from_raster produciría un SusceptibilityMap de esa grilla.\n");
        }
        Err(e) => println!("  (raster real no disponible: {e})\n"),
    }
}

fn part2_synthetic_pipeline() {
    println!("── Part 2 · nowcast sintético → GeoTIFF de peligro ──");
    // A 30×20 tile, susceptibility rising to the SW corner.
    let (ncols, nrows) = (30usize, 20usize);
    let dims = GridDims::new(ncols, nrows);
    let susc: Vec<f64> = (0..nrows)
        .flat_map(|r| (0..ncols).map(move |c| {
            let s = 0.15 + 0.8 * (r as f64 / nrows as f64) * (1.0 - c as f64 / ncols as f64);
            s.clamp(0.0, 1.0)
        }))
        .collect();
    let susceptibility = SusceptibilityMap::new(dims, susc).unwrap();

    // A short rain episode peaking at a 30 mm/h hour.
    let forcing = UniformRain::new(dims, 1.0, vec![0.0, 6.0, 18.0, 30.0, 4.0]).unwrap();
    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::caine(),
        TriggerModel::default(),
        24,
    )
    .unwrap();

    let georef = Georef {
        // 30 m pixels, UTM-19S-like origin (illustrative).
        transform: GeoTransform::new(350_000.0, 6_300_000.0, 30.0, 30.0),
        crs: None,
    };

    let peak = nowcast.hazard_at(3).unwrap(); // the 30 mm/h hour
    let out = std::env::temp_dir().join("nowcast_maipo_hazard.tif");
    write_hazard_geotiff(&peak, &georef, &out).unwrap();
    println!("  peligro peak: máx {:.2} en grilla {ncols}×{nrows}", peak.max_probability());
    println!("  escrito GeoTIFF georreferenciado → {}", out.display());

    // Confirm the round-trip.
    let back: Raster<f32> = read_geotiff(&out, Some(0)).unwrap();
    let back_susc = susceptibility_from_raster(&back).unwrap(); // re-ingest as [0,1]
    println!(
        "  releído: {}×{}, máx {:.2} — round-trip OK\n",
        back.shape().0,
        back.shape().1,
        back_susc.values().iter().copied().fold(0.0, f64::max),
    );
    let _ = std::fs::remove_file(&out);
}

fn main() {
    if !Path::new(MAIPO_SUSC).exists() {
        println!("(nota: raster real del Maipo no encontrado; corro solo la parte sintética)\n");
    }
    part1_read_real();
    part2_synthetic_pipeline();
}
