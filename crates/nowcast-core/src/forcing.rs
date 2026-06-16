//! Dynamic forcing interface and observed-series implementations.
//!
//! A [`Forcing`] is the time-varying trigger that modulates the static
//! susceptibility: rainfall today, and rain + snowmelt or routed discharge once
//! the native providers (`rainflow`, `snowmelt-rs`) are wired in at v0.2.
//!
//! The interface deliberately uses **random access** (`depth_mm(cell, step)`)
//! rather than a pull/iterator model. The intensity–duration logic needs to
//! accumulate water input over rolling windows, and backtesting replays a whole
//! dated series — both want the full record addressable, not a one-shot stream.
//!
//! Naming note: `snowmelt-core` also exposes a type called `Forcing` (a
//! meteorological-input enum, one layer below this one). The v0.2 adapter crate
//! aliases it to avoid the clash; this `Forcing` is the forcing→hazard layer.

use crate::error::{Error, Result};
use crate::grid::GridDims;

/// A source of dynamic water-input forcing over a grid and a time axis.
///
/// `depth_mm` returns the water-input depth (mm) deposited at a cell during a
/// time step: rainfall, or rain + snowmelt. Cells and steps are 0-indexed.
pub trait Forcing {
    /// Spatial grid the forcing is defined on.
    fn dims(&self) -> GridDims;

    /// Number of time steps in the series.
    fn n_steps(&self) -> usize;

    /// Length of one time step, in hours (e.g. `1.0` hourly, `24.0` daily).
    fn dt_hours(&self) -> f64;

    /// Water-input depth (mm) at `cell` during `step`.
    fn depth_mm(&self, cell: usize, step: usize) -> f64;
}

/// Single-gauge observed rainfall applied uniformly to every cell.
///
/// This is the decoupled v0.1 entry point: read one observed series (CR2/DGA
/// gauge) and broadcast it across a susceptibility raster, with no dependency on
/// the upstream Rust engines. Spatial distribution arrives with the native
/// providers in v0.2.
#[derive(Debug, Clone)]
pub struct UniformRain {
    dims: GridDims,
    dt_hours: f64,
    /// Per-step rainfall depth (mm).
    depths_mm: Vec<f64>,
}

impl UniformRain {
    pub fn new(dims: GridDims, dt_hours: f64, depths_mm: Vec<f64>) -> Result<Self> {
        if !dt_hours.is_finite() || dt_hours <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "dt_hours",
                reason: format!("must be > 0, got {dt_hours}"),
            });
        }
        if depths_mm.iter().any(|d| *d < 0.0 || d.is_nan()) {
            return Err(Error::InvalidParameter {
                name: "depths_mm",
                reason: "rainfall depths must be finite and non-negative".to_string(),
            });
        }
        Ok(Self {
            dims,
            dt_hours,
            depths_mm,
        })
    }

    /// Parse a single observed rainfall column from CSV text.
    ///
    /// Minimal `std`-only parser (no `csv` dependency in the core): one record
    /// per line, fields split on `,`. `column` is the 0-based field holding the
    /// rainfall depth (mm). Lines whose target field is empty or non-numeric are
    /// skipped, which tolerates a header row and gappy gauge exports.
    pub fn from_csv(
        text: &str,
        column: usize,
        dims: GridDims,
        dt_hours: f64,
    ) -> Result<Self> {
        let mut depths = Vec::new();
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let field = line.split(',').nth(column).map(str::trim).unwrap_or("");
            match field.parse::<f64>() {
                Ok(v) if v.is_finite() => depths.push(v),
                _ => continue, // header / blank / sentinel → skip
            }
        }
        if depths.is_empty() {
            return Err(Error::Parse(format!(
                "no numeric values found in column {column}"
            )));
        }
        Self::new(dims, dt_hours, depths)
    }
}

impl Forcing for UniformRain {
    fn dims(&self) -> GridDims {
        self.dims
    }

    fn n_steps(&self) -> usize {
        self.depths_mm.len()
    }

    fn dt_hours(&self) -> f64 {
        self.dt_hours
    }

    fn depth_mm(&self, _cell: usize, step: usize) -> f64 {
        self.depths_mm[step]
    }
}
