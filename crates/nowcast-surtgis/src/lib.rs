//! # nowcast-surtgis
//!
//! The geospatial keystone: bridges [`SurtGIS`](surtgis_core) georeferenced
//! rasters to and from the nowcast grid types. `nowcast-core` is deliberately
//! I/O-free (a flat row-major `Vec`), so this crate is how real data enters and
//! leaves the engine:
//!
//! - **in** — a susceptibility raster (TWI / curvature / an ML susceptibility
//!   map) becomes a [`SusceptibilityMap`]; a stack of precipitation rasters
//!   becomes a [`GriddedRain`].
//! - **out** — a [`HazardField`] becomes a georeferenced `Raster<f32>` written
//!   to GeoTIFF, ready for QGIS or downstream tools.
//!
//! Grid convention: SurtGIS `Raster` data is row-major `Array2<T>` of shape
//! `(rows, cols)`; the nowcast [`GridDims`] is `{ncols, nrows}` with flat index
//! `row * ncols + col`. The two line up cell-for-cell, so a
//! [`SusceptibilityMap`] built here shares the grid of the source raster and of
//! any [`HazardField`] written back out.
//!
//! SurtGIS uses pure-Rust GeoTIFF I/O (no system GDAL) by default.

use ndarray::Array2;
use nowcast_core::{GridDims, GriddedRain, HazardField, SusceptibilityMap};
use surtgis_core::io::{read_geotiff, write_geotiff};
use thiserror::Error;

pub use surtgis_core::{CRS, GeoTransform, Raster};

/// Errors from bridging SurtGIS and nowcast.
#[derive(Debug, Error)]
pub enum Error {
    /// Underlying SurtGIS raster / I/O error.
    #[error("surtgis: {0}")]
    SurtGis(#[from] surtgis_core::Error),
    /// nowcast grid/validation error.
    #[error(transparent)]
    Core(#[from] nowcast_core::Error),
    /// Inputs disagreed on grid shape.
    #[error("grid shape mismatch: {0}")]
    Shape(String),
    /// Inputs share a shape but not a georeference (origin/resolution/CRS) —
    /// stacking them would silently misalign the forcing against the grid.
    #[error("georeference mismatch: {0}")]
    Georef(String),
}

pub type Result<T> = std::result::Result<T, Error>;

/// Georeferencing carried alongside the headerless nowcast grids, so an output
/// [`HazardField`] can be written back in the source raster's projection.
#[derive(Debug, Clone)]
pub struct Georef {
    pub transform: GeoTransform,
    pub crs: Option<CRS>,
}

impl Georef {
    /// Capture the georeferencing of a raster (e.g. the susceptibility input).
    pub fn of<T: surtgis_core::RasterCell>(raster: &Raster<T>) -> Self {
        Self {
            transform: *raster.transform(),
            crs: raster.crs().cloned(),
        }
    }
}

fn dims_of<T: surtgis_core::RasterCell>(raster: &Raster<T>) -> GridDims {
    GridDims::new(raster.cols(), raster.rows())
}

/// Convert a susceptibility `Raster<f32>` to a [`SusceptibilityMap`].
///
/// Values are clamped to `[0, 1]` and `NaN`/nodata cells become `0.0` (no
/// background hazard outside the mapped area), so any real susceptibility raster
/// is admissible.
pub fn susceptibility_from_raster(raster: &Raster<f32>) -> Result<SusceptibilityMap> {
    let nodata = raster.nodata();
    let values: Vec<f64> = raster
        .data()
        .iter()
        .map(|&v| {
            if v.is_nan() || nodata.is_some_and(|nd| v == nd) {
                0.0
            } else {
                (v as f64).clamp(0.0, 1.0)
            }
        })
        .collect();
    Ok(SusceptibilityMap::new(dims_of(raster), values)?)
}

/// Read a susceptibility GeoTIFF into a [`SusceptibilityMap`] plus its [`Georef`]
/// (first band, 0-indexed).
pub fn read_susceptibility<P: AsRef<std::path::Path>>(
    path: P,
) -> Result<(SusceptibilityMap, Georef)> {
    let raster: Raster<f32> = read_geotiff(path, Some(0))?;
    let georef = Georef::of(&raster);
    Ok((susceptibility_from_raster(&raster)?, georef))
}

/// `true` when two geotransforms describe the same grid placement, within a
/// tight relative tolerance (f64 serialization noise, not real offsets).
fn same_transform(a: &GeoTransform, b: &GeoTransform) -> bool {
    let close = |x: f64, y: f64| (x - y).abs() <= 1e-9 + 1e-9 * x.abs().max(y.abs());
    close(a.origin_x, b.origin_x)
        && close(a.origin_y, b.origin_y)
        && close(a.pixel_width, b.pixel_width)
        && close(a.pixel_height, b.pixel_height)
        && close(a.row_rotation, b.row_rotation)
        && close(a.col_rotation, b.col_rotation)
}

/// Stack per-step precipitation rasters (mm) into a [`GriddedRain`] forcing. All
/// rasters must share the same shape **and georeference**; `dt_hours` is the
/// step length.
///
/// The georeference check (origin, resolution, rotation, and CRS when both
/// declare one) is what catches the realistic failure: a tile from a different
/// grid with the same shape — same-sized CR2MET and IMERG cutouts, say — which
/// would otherwise stack silently and misalign rain against susceptibility.
pub fn gridded_rain_from_rasters(rasters: &[Raster<f32>], dt_hours: f64) -> Result<GriddedRain> {
    let first = rasters
        .first()
        .ok_or_else(|| Error::Shape("no rasters provided".into()))?;
    let dims = dims_of(first);
    let n = dims.len();
    let mut depths = Vec::with_capacity(rasters.len() * n);
    for (s, r) in rasters.iter().enumerate() {
        if dims_of(r) != dims {
            return Err(Error::Shape(format!("raster {s} differs from raster 0")));
        }
        if !same_transform(r.transform(), first.transform()) {
            return Err(Error::Georef(format!(
                "raster {s} has a different geotransform than raster 0 (same shape, different grid)"
            )));
        }
        if let (Some(ca), Some(cb)) = (r.crs(), first.crs())
            && ca != cb
        {
            return Err(Error::Georef(format!(
                "raster {s} has a different CRS than raster 0"
            )));
        }
        let nodata = r.nodata();
        depths.extend(r.data().iter().map(|&v| {
            if !v.is_finite() || nodata.is_some_and(|nd| v == nd) {
                0.0
            } else {
                (v as f64).max(0.0)
            }
        }));
    }
    Ok(GriddedRain::new(dims, dt_hours, depths)?)
}

/// Wrap a [`HazardField`] into a georeferenced `Raster<f32>`.
pub fn hazard_to_raster(field: &HazardField, georef: &Georef) -> Raster<f32> {
    let dims = field.dims();
    let data: Vec<f32> = field.probability().iter().map(|&p| p as f32).collect();
    let array =
        Array2::from_shape_vec((dims.nrows, dims.ncols), data).expect("HazardField len matches dims");
    let mut raster = Raster::from_array(array);
    raster.set_transform(georef.transform);
    raster.set_crs(georef.crs.clone());
    raster
}

/// Write a [`HazardField`] to a georeferenced GeoTIFF (probability per cell).
pub fn write_hazard_geotiff<P: AsRef<std::path::Path>>(
    field: &HazardField,
    georef: &Georef,
    path: P,
) -> Result<()> {
    write_geotiff(&hazard_to_raster(field, georef), path, None)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raster_2x3(vals: [f32; 6], nodata: Option<f32>) -> Raster<f32> {
        let mut r = Raster::from_array(Array2::from_shape_vec((2, 3), vals.to_vec()).unwrap());
        r.set_transform(GeoTransform::new(350_000.0, 6_300_000.0, 30.0, 30.0));
        r.set_crs(None);
        r.set_nodata(nodata);
        r
    }

    #[test]
    fn susceptibility_clamps_and_zeroes_nodata() {
        // last cell is nodata (-1), one value out of range (1.4) is clamped.
        let r = raster_2x3([0.1, 0.5, 1.4, 0.0, 0.9, -1.0], Some(-1.0));
        let susc = susceptibility_from_raster(&r).unwrap();
        assert_eq!(susc.dims(), GridDims::new(3, 2));
        let expected = [0.1f32, 0.5, 1.0, 0.0, 0.9, 0.0];
        for (got, &exp) in susc.values().iter().zip(&expected) {
            assert!((got - exp as f64).abs() < 1e-6, "{got} vs {exp}");
        }
    }

    #[test]
    fn gridded_rain_stacks_steps() {
        let r0 = raster_2x3([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], None);
        let r1 = raster_2x3([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], None);
        let rain = gridded_rain_from_rasters(&[r0, r1], 24.0).unwrap();
        use nowcast_core::Forcing;
        assert_eq!(rain.n_steps(), 2);
        assert_eq!(rain.depth_mm(0, 0), 0.0);
        assert_eq!(rain.depth_mm(5, 0), 5.0);
        assert_eq!(rain.depth_mm(2, 1), 12.0);
    }

    #[test]
    fn gridded_rain_rejects_a_misgeoreferenced_tile() {
        // Same 2×3 shape, but the second raster comes from another grid: a
        // shifted origin (and separately, a different pixel size) must error
        // instead of stacking silently.
        let r0 = raster_2x3([0.0; 6], None);
        let mut shifted = raster_2x3([1.0; 6], None);
        shifted.set_transform(GeoTransform::new(350_900.0, 6_300_000.0, 30.0, 30.0));
        assert!(matches!(
            gridded_rain_from_rasters(&[r0.clone(), shifted], 24.0),
            Err(Error::Georef(_))
        ));

        let mut coarser = raster_2x3([1.0; 6], None);
        coarser.set_transform(GeoTransform::new(350_000.0, 6_300_000.0, 90.0, 90.0));
        assert!(matches!(
            gridded_rain_from_rasters(&[r0.clone(), coarser], 24.0),
            Err(Error::Georef(_))
        ));

        // Different declared CRS also errors; an undeclared one is tolerated.
        let mut other_crs = raster_2x3([1.0; 6], None);
        other_crs.set_crs(Some(CRS::from_epsg(32719)));
        let mut base_crs = r0.clone();
        base_crs.set_crs(Some(CRS::from_epsg(4326)));
        assert!(matches!(
            gridded_rain_from_rasters(&[base_crs, other_crs.clone()], 24.0),
            Err(Error::Georef(_))
        ));
        assert!(gridded_rain_from_rasters(&[r0, other_crs], 24.0).is_ok());
    }

    #[test]
    fn gridded_rain_zeroes_non_finite_pixels() {
        // A corrupted (+inf) pixel must be treated like nodata (zeroed), not
        // propagated into the forcing where it would saturate an I-D window.
        let r0 = raster_2x3([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], None);
        let r1 = raster_2x3([10.0, f32::INFINITY, 12.0, 13.0, f32::NEG_INFINITY, 15.0], None);
        let rain = gridded_rain_from_rasters(&[r0, r1], 24.0).unwrap();
        use nowcast_core::Forcing;
        assert_eq!(rain.depth_mm(1, 1), 0.0);
        assert_eq!(rain.depth_mm(4, 1), 0.0);
        assert_eq!(rain.depth_mm(2, 1), 12.0);
    }

    #[test]
    fn hazard_roundtrips_through_geotiff() {
        let dims = GridDims::new(3, 2);
        let field = HazardField::new(0, dims, vec![0.0, 0.25, 0.5, 0.75, 0.9, 1.0]).unwrap();
        let georef = Georef {
            transform: GeoTransform::new(350_000.0, 6_300_000.0, 30.0, 30.0),
            crs: None,
        };
        let path = std::env::temp_dir().join("nowcast_surtgis_roundtrip.tif");
        write_hazard_geotiff(&field, &georef, &path).unwrap();

        let back: Raster<f32> = read_geotiff(&path, Some(0)).unwrap();
        assert_eq!(back.shape(), (2, 3));
        let got: Vec<f64> = back.data().iter().map(|&v| v as f64).collect();
        for (a, b) in got.iter().zip(field.probability()) {
            assert!((a - b).abs() < 1e-6, "{a} vs {b}");
        }
        let _ = std::fs::remove_file(&path);
    }
}
