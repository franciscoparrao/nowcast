//! # nowcast-snowmelt
//!
//! v0.2 [`Forcing`](nowcast_core::Forcing) provider that drives a nowcast with
//! **distributed** water input — per-cell rain + snowmelt runoff — from the
//! `snowmelt-rs` degree-day snow model. This is the rain-on-snow path the v0.1
//! single-gauge `UniformRain` cannot represent: water reaching the ground is the
//! sum of liquid precipitation and melt, and melt varies strongly across a DEM
//! with the temperature lapse rate.
//!
//! ## Bridging two execution models
//!
//! `snowmelt_core::SnowModel` is a **stateful, sequential** simulation (SWE
//! accumulates step to step), whereas `nowcast_core::Forcing` requires
//! **random access** (`depth_mm(cell, step)`) so the intensity–duration logic
//! can accumulate rolling windows. The bridge is to pre-run the snow model over
//! the whole meteo series once and store the per-cell runoff in a dense buffer
//! the nowcast then indexes freely.
//!
//! Memory is `n_steps × n_cells × 8` bytes — fine for catchment grids over
//! seasonal series; stream the run if you ever need continental × decadal.
//!
//! ## Naming
//!
//! `snowmelt_core` also exports a type named `Forcing` (its meteorological
//! input: temperature + precipitation). To avoid the clash with the nowcast
//! trait, it is re-exported here as [`MeteoForcing`].
//!
//! ```no_run
//! use ndarray::Array2;
//! use nowcast_core::{Forcing, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};
//! use nowcast_snowmelt::{Dem, DegreeDayParams, MeteoForcing, SnowModel, SnowmeltForcing};
//!
//! // A DEM and a snow model.
//! let dem = Dem::new(Array2::from_shape_fn((20, 20), |(i, j)| 1500.0 + 40.0 * (i + j) as f64))?;
//! let model = SnowModel::new(dem, DegreeDayParams::default())?;
//!
//! // A meteo series (one step per day): here a warm storm melting the pack.
//! let series = vec![MeteoForcing::Uniform { t_ref: 6.0, z_ref: 1500.0, precip: 25.0 }; 5];
//! let forcing = SnowmeltForcing::run(model, &series, 1.0)?; // dt = 1 day
//!
//! // Drive a nowcast with the distributed rain+melt field.
//! let susc = SusceptibilityMap::uniform(forcing.dims(), 0.7).unwrap();
//! let nowcast = Nowcast::new(susc, forcing, IdThreshold::new(4.0, 0.39).unwrap(),
//!                            TriggerModel::default(), 7).unwrap();
//! let alerts = nowcast.alerts(0.5);
//! # Ok::<(), snowmelt_core::SnowmeltError>(())
//! ```

use nowcast_core::{Forcing, GridDims};
use snowmelt_core::Result as SnowResult;

// Re-export the snowmelt building blocks so callers need only this crate.
// `MeteoForcing` is snowmelt's `Forcing` (temperature + precipitation input),
// renamed to avoid clashing with the nowcast `Forcing` trait.
pub use snowmelt_core::{
    Dem, DegreeDayParams, Forcing as MeteoForcing, SnowModel, SnowmeltError,
};

/// A nowcast [`Forcing`] backed by a pre-run distributed snow simulation.
///
/// Holds the per-cell rain + snowmelt runoff (mm) for every step, in row-major
/// order matching [`GridDims`] (and therefore a
/// [`SusceptibilityMap`](nowcast_core::SusceptibilityMap) built on the same DEM
/// grid).
#[derive(Debug, Clone)]
pub struct SnowmeltForcing {
    dims: GridDims,
    dt_hours: f64,
    n_steps: usize,
    /// Row-major, `n_steps * n_cells`; `runoff[step * n_cells + cell]`.
    runoff: Vec<f64>,
}

impl SnowmeltForcing {
    /// Run `model` over the meteo `series` (one [`MeteoForcing`] per step) at a
    /// time step of `dt_days`, collecting per-cell rain + snowmelt runoff (mm)
    /// into a buffer the nowcast can random-access.
    ///
    /// Nodata cells (`NaN` elevation propagating `NaN` runoff) and any negative
    /// values are clamped to `0.0` water input, so they never poison the
    /// rolling-window accumulation downstream.
    pub fn run(mut model: SnowModel, series: &[MeteoForcing], dt_days: f64) -> SnowResult<Self> {
        let (rows, cols) = model.dem().shape();
        let dims = GridDims::new(cols, rows);
        let n_cells = dims.len();

        let mut runoff = Vec::with_capacity(series.len() * n_cells);
        for meteo in series {
            let out = model.step_days(meteo, dt_days)?;
            let r = out.runoff();
            // Explicit row-major traversal: flat = i * cols + j, matching
            // GridDims::index(col=j, row=i). Independent of ndarray's layout.
            for i in 0..rows {
                for j in 0..cols {
                    let v = r[[i, j]];
                    runoff.push(if v.is_finite() { v.max(0.0) } else { 0.0 });
                }
            }
        }

        Ok(Self {
            dims,
            dt_hours: dt_days * 24.0,
            n_steps: series.len(),
            runoff,
        })
    }

    /// Per-cell runoff field (mm) at a step, as a row-major slice — handy for
    /// inspection or writing a raster.
    ///
    /// # Panics
    ///
    /// Panics if `step >= n_steps` — the same documented in-range contract as
    /// [`Forcing::depth_mm`](nowcast_core::Forcing::depth_mm).
    pub fn runoff_at(&self, step: usize) -> &[f64] {
        let n = self.dims.len();
        &self.runoff[step * n..(step + 1) * n]
    }
}

impl Forcing for SnowmeltForcing {
    fn dims(&self) -> GridDims {
        self.dims
    }

    fn n_steps(&self) -> usize {
        self.n_steps
    }

    fn dt_hours(&self) -> f64 {
        self.dt_hours
    }

    fn depth_mm(&self, cell: usize, step: usize) -> f64 {
        self.runoff[step * self.dims.len() + cell]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ndarray::Array2;
    use nowcast_core::{IdThreshold, Nowcast, SusceptibilityMap, TriggerModel};

    /// 2 rows × 3 cols DEM, elevation increasing with i+j (1000 m … 1300 m).
    fn dem_2x3() -> Dem {
        Dem::new(Array2::from_shape_fn((2, 3), |(i, j)| {
            1000.0 + 100.0 * (i + j) as f64
        }))
        .unwrap()
    }

    #[test]
    fn warm_rain_runoff_matches_precip() {
        let model = SnowModel::new(dem_2x3(), DegreeDayParams::default()).unwrap();
        // Warm everywhere (even uphill via lapse) → all precip falls as rain,
        // no snowpack so no melt; runoff == precip on every cell.
        let series = vec![MeteoForcing::Uniform {
            t_ref: 12.0,
            z_ref: 1000.0,
            precip: 20.0,
        }];
        let f = SnowmeltForcing::run(model, &series, 1.0).unwrap();

        assert_eq!(f.dims(), GridDims::new(3, 2));
        assert_eq!(f.n_steps(), 1);
        assert_eq!(f.dt_hours(), 24.0);
        for cell in 0..f.dims().len() {
            assert!(
                (f.depth_mm(cell, 0) - 20.0).abs() < 1e-6,
                "cell {cell} runoff = {}",
                f.depth_mm(cell, 0)
            );
        }
    }

    #[test]
    fn melt_is_spatially_distributed() {
        let model = SnowModel::new(dem_2x3(), DegreeDayParams::default()).unwrap();
        let series = vec![
            // Day 1: cold snowfall → accumulates as SWE, ~no runoff.
            MeteoForcing::Uniform {
                t_ref: -5.0,
                z_ref: 1000.0,
                precip: 30.0,
            },
            // Day 2: warm, dry → degree-day melt; warmer (lower) cells melt more.
            MeteoForcing::Uniform {
                t_ref: 8.0,
                z_ref: 1000.0,
                precip: 0.0,
            },
        ];
        let f = SnowmeltForcing::run(model, &series, 1.0).unwrap();

        // Day 1: snow, essentially no liquid water reaches the ground.
        for cell in 0..f.dims().len() {
            assert!(f.depth_mm(cell, 0) < 1.0, "snowy day produced runoff");
        }
        // Day 2: melt everywhere, and the lowest/warmest cell (flat 0, 1000 m)
        // releases at least as much as the highest/coldest (flat 5, 1300 m).
        let low = f.depth_mm(0, 1);
        let high = f.depth_mm(5, 1);
        assert!(low > 0.0, "no melt on warm day");
        assert!(
            low >= high,
            "warmer cell should melt at least as much: low={low} high={high}"
        );
    }

    #[test]
    fn drives_a_nowcast_end_to_end() {
        let model = SnowModel::new(dem_2x3(), DegreeDayParams::default()).unwrap();
        let series = vec![
            MeteoForcing::Uniform {
                t_ref: -5.0,
                z_ref: 1000.0,
                precip: 40.0,
            },
            MeteoForcing::Uniform {
                t_ref: 10.0,
                z_ref: 1000.0,
                precip: 0.0,
            },
        ];
        let f = SnowmeltForcing::run(model, &series, 1.0).unwrap();
        let dims = f.dims();
        let susc = SusceptibilityMap::uniform(dims, 0.8).unwrap();
        // a=3 mm/h: a ~30 mm/day melt pulse (≈1.25 mm/h over 24 h) clears the curve.
        let nowcast = Nowcast::new(
            susc,
            f,
            IdThreshold::new(3.0, 0.39).unwrap(),
            TriggerModel::default(),
            7,
        )
        .unwrap();
        let fields = nowcast.run();
        assert!(fields[0].max_probability() < 0.2, "snowy day should be quiet");
        assert!(
            fields[1].max_probability() > 0.4,
            "melt pulse should raise hazard, got {}",
            fields[1].max_probability()
        );
    }
}
