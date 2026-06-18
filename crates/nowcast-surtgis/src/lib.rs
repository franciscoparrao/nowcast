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

/// Stack per-step precipitation rasters (mm) into a [`GriddedRain`] forcing. All
/// rasters must share the same shape; `dt_hours` is the step length.
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
        let nodata = r.nodata();
        depths.extend(r.data().iter().map(|&v| {
            if v.is_nan() || nodata.is_some_and(|nd| v == nd) {
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
