//! # nowcast-swarm
//!
//! Agent-based **event refinement** for the landslide / debris-flow path. The
//! nowcast flags *when* and *where* an intensity–duration threshold is crossed;
//! this crate then runs the `swarm-abm` debris-flow model (rain + flow agents
//! over the terrain raster, calibrated on the 2015 Atacama event) to simulate
//! the actual **runout footprint** — which slopes the flow reaches and where it
//! stops.
//!
//! It is the landslide-side counterpart of [`nowcast-hydroflux`] (which refines
//! *floods* with 2D shallow water): a one-way, on-demand coupling that runs the
//! expensive process model only where the cheap nowcast already alerted, and
//! downscales the coarse probability onto the physical footprint.
//!
//! [`nowcast-hydroflux`]: https://docs.rs/nowcast-hydroflux
//!
//! Grid convention: `swarm_abm::Grid2D` is indexed by `Pos { x, y }` (x = col,
//! y = row); the nowcast [`GridDims`] is `{ncols, nrows}` with flat index
//! `y * ncols + x`. The two line up cell-for-cell.

use std::sync::Arc;

use debris_flow::{DebrisFlowModel, Params};
use nowcast_core::{GridDims, HazardField, Result, SusceptibilityMap};
use swarm_abm::prelude::{Activation, Grid2D, Pos, Schedule, Simulation};

// Re-export the model building blocks so callers need only this crate.
pub use debris_flow::raster::{CopiapoData, Window, load};
pub use debris_flow::{Layers, Params as DebrisParams};
pub use swarm_abm::prelude::Grid2D as SwarmGrid;

/// Convert a nowcast [`SusceptibilityMap`] into a `swarm_abm::Grid2D<f32>`
/// (e.g. to assemble the debris-flow model's susceptibility layer).
pub fn grid_from_susceptibility(s: &SusceptibilityMap) -> Grid2D<f32> {
    let d = s.dims();
    Grid2D::from_fn(d.ncols, d.nrows, |p: Pos| s.get(p.y * d.ncols + p.x) as f32)
}

/// The simulated debris-flow runout: which cells the flow reached.
#[derive(Debug, Clone)]
pub struct Runout {
    dims: GridDims,
    footprint: Vec<bool>,
    pixel_size: f64,
}

impl Runout {
    /// Build from the model's `Grid2D<bool>` footprint (row-major flatten).
    /// Errors on a non-positive/non-finite `pixel_size` — this public
    /// constructor used to bypass the guard [`run_runout`] enforces, and a
    /// `NaN` (or a negative value, whose sign cancels when squared) silently
    /// corrupts every area statistic downstream.
    pub fn from_footprint(footprint: &Grid2D<bool>, pixel_size: f64) -> Result<Self> {
        validate_pixel_size(pixel_size)?;
        let (w, h) = (footprint.width(), footprint.height());
        let mut flat = Vec::with_capacity(w * h);
        for y in 0..h {
            for x in 0..w {
                flat.push(footprint[Pos { x, y }]);
            }
        }
        Ok(Self {
            dims: GridDims::new(w, h),
            footprint: flat,
            pixel_size,
        })
    }

    pub fn dims(&self) -> GridDims {
        self.dims
    }

    /// Row-major mask: `true` where the flow reached.
    pub fn footprint(&self) -> &[bool] {
        &self.footprint
    }

    /// Number of cells reached by the flow.
    pub fn affected_cells(&self) -> usize {
        self.footprint.iter().filter(|&&f| f).count()
    }

    /// Inundated area in km² (using the model's pixel size in metres).
    pub fn affected_km2(&self) -> f64 {
        self.affected_cells() as f64 * self.pixel_size * self.pixel_size / 1.0e6
    }

    /// Downscale a coarse nowcast probability onto the physical footprint: the
    /// probability where the flow reached, `0` elsewhere — the landslide analogue
    /// of `nowcast-hydroflux`'s depth-gated refinement. Errors if `nowcast_prob`
    /// lies outside `[0, 1]` (the caller's probability is not assumed valid).
    pub fn refined_hazard(&self, step: usize, nowcast_prob: f64) -> Result<HazardField> {
        HazardField::masked(step, self.dims, &self.footprint, nowcast_prob)
    }
}

/// Run the debris-flow ABM over `layers` and return its runout [`Runout`].
///
/// `layers` is the model's input stack (DEM, slope, rain, susceptibility, …);
/// `params` are the physical parameters ([`Params::default`] or a calibrated
/// set); `steps` bounds the simulated hours (one ABM step ≡ one hour in the
/// debris-flow model). Errors on a non-positive/non-finite `pixel_size`, which
/// would silently corrupt every area statistic downstream.
pub fn run_runout(
    layers: Arc<Layers>,
    params: Params,
    pixel_size: f64,
    seed: u64,
    steps: u64,
) -> Result<Runout> {
    // Fail fast, before the (expensive) simulation; `from_footprint` re-checks
    // for its own direct callers.
    validate_pixel_size(pixel_size)?;
    let model = DebrisFlowModel::new(layers, params, pixel_size, seed);
    let mut sim = Simulation::new(model, seed).with_schedule(Schedule::new(Activation::Ordered));
    sim.run(steps);
    Runout::from_footprint(&sim.model.footprint, pixel_size)
}

/// Shared guard: a pixel size must be a positive, finite number of metres.
fn validate_pixel_size(pixel_size: f64) -> Result<()> {
    if !pixel_size.is_finite() || pixel_size <= 0.0 {
        return Err(nowcast_core::Error::InvalidParameter {
            name: "pixel_size",
            reason: format!("must be finite and > 0 metres, got {pixel_size}"),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn susceptibility_bridges_to_grid() {
        let dims = GridDims::new(3, 2); // 3 cols, 2 rows
        let s = SusceptibilityMap::new(dims, vec![0.1, 0.2, 0.3, 0.4, 0.5, 0.6]).unwrap();
        let g = grid_from_susceptibility(&s);
        assert_eq!((g.width(), g.height()), (3, 2));
        assert!((g[Pos { x: 0, y: 0 }] - 0.1).abs() < 1e-6);
        assert!((g[Pos { x: 2, y: 0 }] - 0.3).abs() < 1e-6);
        assert!((g[Pos { x: 1, y: 1 }] - 0.5).abs() < 1e-6); // flat 1*3+1 = 4 → 0.5
    }

    #[test]
    fn footprint_refines_to_hazard() {
        // Hand-built footprint: a 2x2 with two reached cells.
        let mut fp = Grid2D::from_fn(2, 2, |_p: Pos| false);
        fp[Pos { x: 0, y: 0 }] = true;
        fp[Pos { x: 1, y: 1 }] = true;
        let runout = Runout::from_footprint(&fp, 30.0).unwrap();
        // The public constructor enforces the same pixel-size guard as
        // run_runout: NaN and negative sizes are errors, not corrupt areas.
        assert!(Runout::from_footprint(&fp, f64::NAN).is_err());
        assert!(Runout::from_footprint(&fp, -30.0).is_err());
        assert!(Runout::from_footprint(&fp, 0.0).is_err());

        assert_eq!(runout.affected_cells(), 2);
        assert!((runout.affected_km2() - 2.0 * 900.0 / 1.0e6).abs() < 1e-12);

        let hz = runout.refined_hazard(0, 0.8).unwrap();
        assert_eq!(hz.probability(), &[0.8, 0.0, 0.0, 0.8]);
        // A probability outside [0,1] is rejected, not a panic.
        assert!(runout.refined_hazard(0, 1.0001).is_err());
    }

    #[test]
    fn runs_on_a_synthetic_stack() {
        // A small, sane layer stack so the ABM runs end-to-end without panicking.
        let (w, h) = (24usize, 24usize);
        let g = |f: fn(Pos) -> f32| Grid2D::from_fn(w, h, f);
        let layers = Layers {
            dem: Grid2D::from_fn(w, h, |p| 1000.0 - p.y as f32 * 4.0),
            slope: g(|_| 12.0),
            rain: vec![Grid2D::from_fn(w, h, |_| 60.0)],
            isotherm: g(|_| 4000.0),
            sediment: g(|_| 1.0),
            susceptibility: g(|_| 0.6),
            streams: g(|_| 0.0),
        };
        let runout = run_runout(Arc::new(layers), Params::default(), 30.0, 42, 12).unwrap();
        assert_eq!(runout.dims(), GridDims::new(24, 24));
        // affected_km2 is well-defined regardless of how far the flow spread.
        assert!(runout.affected_km2() >= 0.0);
    }
}
