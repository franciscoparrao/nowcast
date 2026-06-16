//! Minimal raster grid and static susceptibility map.
//!
//! `nowcast-core` keeps its grid representation dependency-free: a flat,
//! row-major `Vec<f64>` plus its dimensions. The v0.2 provider crates
//! (`nowcast-snowmelt`) bridge this to `ndarray::Array2` at their boundary, so
//! the core stays buildable offline with `std` only.

use crate::error::{Error, Result};

/// Dimensions of a row-major raster grid.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GridDims {
    pub ncols: usize,
    pub nrows: usize,
}

impl GridDims {
    pub fn new(ncols: usize, nrows: usize) -> Self {
        Self { ncols, nrows }
    }

    /// Number of cells in the grid.
    pub fn len(&self) -> usize {
        self.ncols * self.nrows
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Flat row-major index for a `(col, row)` pair.
    pub fn index(&self, col: usize, row: usize) -> usize {
        row * self.ncols + col
    }
}

/// Static landslide/flood susceptibility map: one value per cell in `[0, 1]`.
///
/// This is the background field produced upstream (Smelt, an external ML model,
/// or any susceptibility raster). The nowcast modulates it in time with a
/// dynamic trigger; it never changes the susceptibility itself.
#[derive(Debug, Clone)]
pub struct SusceptibilityMap {
    dims: GridDims,
    values: Vec<f64>,
}

impl SusceptibilityMap {
    /// Build a map from a row-major vector of susceptibility values.
    ///
    /// Fails if `values.len()` does not match `dims`, or if any value lies
    /// outside `[0, 1]`.
    pub fn new(dims: GridDims, values: Vec<f64>) -> Result<Self> {
        if values.len() != dims.len() {
            return Err(Error::GridSizeMismatch {
                expected: dims.len(),
                got: values.len(),
                ncols: dims.ncols,
                nrows: dims.nrows,
            });
        }
        for (cell, &v) in values.iter().enumerate() {
            if !(0.0..=1.0).contains(&v) {
                return Err(Error::SusceptibilityOutOfRange { cell, value: v });
            }
        }
        Ok(Self { dims, values })
    }

    /// Build a spatially uniform map (useful for tests and quick experiments).
    pub fn uniform(dims: GridDims, value: f64) -> Result<Self> {
        Self::new(dims, vec![value; dims.len()])
    }

    pub fn dims(&self) -> GridDims {
        self.dims
    }

    pub fn values(&self) -> &[f64] {
        &self.values
    }

    /// Susceptibility at a flat cell index.
    pub fn get(&self, cell: usize) -> f64 {
        self.values[cell]
    }
}
