//! # nowcast-firespread
//!
//! Wildfire as a **parallel hazard path** plus a **post-fire cascade**, the fire
//! analogue of `nowcast-hydroflux` (physical coupling) extended with the feedback
//! that makes fire special for geohazards.
//!
//! Two capabilities:
//!
//! 1. **Fire hazard path.** [`run_fire`] drives the `firespread` engine (Rothermel
//!    surface spread + minimum-travel-time front) over the nowcast grid and returns
//!    a [`FireField`]: per-cell arrival time, rate of spread and Byram fireline
//!    intensity. [`FireField::fire_hazard`] maps intensity to a `[0, 1]`
//!    [`HazardField`], the wildfire counterpart of the rainfall hazard.
//!
//! 2. **Post-fire cascade.** A burn scar sharply raises debris-flow and shallow-
//!    landslide susceptibility (loss of canopy and root cohesion, hydrophobic
//!    soils): the rainfall intensity needed to trigger a post-fire debris flow is
//!    far below the unburned threshold. [`post_fire_susceptibility`] amplifies the
//!    static susceptibility inside the burned footprint, so a *subsequent* rainfall
//!    nowcast (the ordinary `nowcast-core` path) sees the elevated hazard on the
//!    scar. This is a one-way cascade fire → susceptibility → rainfall nowcast.
//!
//! `nowcast-core` stays dependency-light and offline; this adapter pulls the
//! wildfire stack, so it builds online once (then from cache).

use firespread_core::simulate;
use nowcast_core::{Error, GridDims, HazardField, Result, SusceptibilityMap};

// Re-export the building blocks so callers need only this crate.
pub use firespread_core::{FuelModel, Landscape, Moisture, SpreadResult, Weather};

fn map_err(e: firespread_core::FireError) -> Error {
    // A failed simulation is an engine error, not an invalid nowcast parameter.
    Error::Engine {
        engine: "firespread",
        reason: e.to_string(),
    }
}

/// A fire-spread result mapped onto the nowcast grid (row-major, `GridDims`).
#[derive(Debug, Clone)]
pub struct FireField {
    result: SpreadResult,
    dims: GridDims,
    horizon_min: f64,
}

impl FireField {
    /// Grid of the burned field; `GridDims::new(land.cols(), land.rows())`.
    pub fn dims(&self) -> GridDims {
        self.dims
    }

    /// Time horizon (minutes since ignition) defining "burned".
    pub fn horizon_min(&self) -> f64 {
        self.horizon_min
    }

    /// Raw firespread output (arrival time, ROS, intensity per cell).
    pub fn result(&self) -> &SpreadResult {
        &self.result
    }

    /// Cells reached by the fire within the horizon.
    pub fn burned_cells(&self) -> usize {
        self.result.burned_cells(self.horizon_min)
    }

    /// Burned area within the horizon, hectares.
    pub fn burned_area_ha(&self) -> f64 {
        self.result.burned_area_ha(self.horizon_min)
    }

    /// Burned mask (row-major): `true` where the fire arrived within the horizon.
    pub fn burned_mask(&self) -> Vec<bool> {
        self.result
            .arrival_min
            .iter()
            .map(|&t| t.is_finite() && t <= self.horizon_min)
            .collect()
    }

    /// Fire hazard as the normalized Byram fireline intensity in `[0, 1]` over
    /// burned cells (0 elsewhere); `intensity_ref_kw_m` saturates the scale (a
    /// common reference is 1730 kW/m, the limit of direct manual attack).
    pub fn fire_hazard(&self, intensity_ref_kw_m: f64) -> Result<HazardField> {
        if !intensity_ref_kw_m.is_finite() || intensity_ref_kw_m <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "intensity_ref_kw_m",
                reason: "must be a positive, finite reference intensity".into(),
            });
        }
        let burned = self.burned_mask();
        let prob: Vec<f64> = self
            .result
            .intensity_head
            .iter()
            .zip(&burned)
            .map(|(&i, &b)| if b { (i / intensity_ref_kw_m).clamp(0.0, 1.0) } else { 0.0 })
            .collect();
        HazardField::new(0, self.dims, prob)
    }
}

/// Run firespread on `land` and map the result onto the nowcast grid.
///
/// `ignitions` are `(row, col)` cells; `horizon_min` bounds the simulation and
/// defines the burned footprint. The nowcast grid is
/// `GridDims::new(land.cols(), land.rows())`, so cell index `row*cols + col`
/// agrees with `firespread`'s row-major arrays.
pub fn run_fire(
    land: &Landscape,
    weather: &Weather,
    ignitions: &[(usize, usize)],
    horizon_min: f64,
) -> Result<FireField> {
    // A NaN or non-positive horizon lets the travel-time solver propagate
    // nothing (not even the ignition cells "burn"), so the whole post-fire
    // cascade silently no-ops. Reject it as the parameter error it is.
    if !horizon_min.is_finite() || horizon_min <= 0.0 {
        return Err(Error::InvalidParameter {
            name: "horizon_min",
            reason: format!("must be finite and > 0 minutes, got {horizon_min}"),
        });
    }
    let result = simulate(land, weather, ignitions, horizon_min).map_err(map_err)?;
    Ok(FireField {
        dims: GridDims::new(land.cols(), land.rows()),
        result,
        horizon_min,
    })
}

/// Post-fire cascade: multiply susceptibility by `factor` inside the burned
/// footprint, leaving unburned cells unchanged. Result is clamped to `[0, 1]`.
///
/// `factor > 1` models the elevated post-wildfire debris-flow/landslide
/// susceptibility on a burn scar. The returned map feeds straight back into the
/// ordinary rainfall nowcast (`nowcast-core`).
pub fn post_fire_susceptibility(
    base: &SusceptibilityMap,
    fire: &FireField,
    factor: f64,
) -> Result<SusceptibilityMap> {
    if base.dims() != fire.dims() {
        let (b, f) = (base.dims(), fire.dims());
        return Err(Error::GridMismatch {
            susc_cols: b.ncols,
            susc_rows: b.nrows,
            forc_cols: f.ncols,
            forc_rows: f.nrows,
        });
    }
    if !factor.is_finite() || factor < 0.0 {
        return Err(Error::InvalidParameter {
            name: "factor",
            reason: "must be a non-negative, finite amplification".into(),
        });
    }
    let burned = fire.burned_mask();
    let values: Vec<f64> = base
        .values()
        .iter()
        .zip(&burned)
        .map(|(&s, &b)| if b { (s * factor).clamp(0.0, 1.0) } else { s })
        .collect();
    SusceptibilityMap::new(base.dims(), values)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A small, dry, windy chaparral landscape that burns from a central ignition.
    fn burning_field() -> FireField {
        let land = Landscape::uniform(16, 16, 30.0, 4); // NFFL 4 (chaparral)
        let weather = Weather {
            wind_speed_kmh: 30.0,
            wind_from_deg: 270.0,
            moisture: Moisture::DRY_SUMMER,
        };
        run_fire(&land, &weather, &[(8, 8)], 120.0).unwrap()
    }

    #[test]
    fn fire_burns_and_mask_matches_grid() {
        let fire = burning_field();
        let mask = fire.burned_mask();
        assert_eq!(mask.len(), fire.dims().len());
        assert!(fire.burned_cells() > 0, "a dry, windy chaparral field should burn");
        assert_eq!(mask.iter().filter(|&&b| b).count(), fire.burned_cells());
    }

    #[test]
    fn fire_hazard_is_in_unit_interval_and_zero_off_scar() {
        let fire = burning_field();
        let hz = fire.fire_hazard(1730.0).unwrap();
        let burned = fire.burned_mask();
        for (p, b) in hz.probability().iter().zip(&burned) {
            assert!((0.0..=1.0).contains(p));
            if !b {
                assert_eq!(*p, 0.0, "unburned cells carry no fire hazard");
            }
        }
        assert!(fire.fire_hazard(0.0).is_err());
    }

    #[test]
    fn cascade_raises_burned_cells_only() {
        let fire = burning_field();
        let base = SusceptibilityMap::uniform(fire.dims(), 0.4).unwrap();
        let after = post_fire_susceptibility(&base, &fire, 2.0).unwrap();
        let burned = fire.burned_mask();
        for (cell, &b) in burned.iter().enumerate() {
            if b {
                assert!(after.get(cell) > base.get(cell), "burned cell susceptibility must rise");
                assert!(after.get(cell) <= 1.0, "clamped to [0,1]");
            } else {
                assert_eq!(after.get(cell), base.get(cell), "unburned cell unchanged");
            }
        }
    }

    #[test]
    fn cascade_rejects_grid_mismatch() {
        let fire = burning_field();
        let wrong = SusceptibilityMap::uniform(GridDims::new(4, 4), 0.5).unwrap();
        assert!(post_fire_susceptibility(&wrong, &fire, 1.5).is_err());
    }

    #[test]
    fn run_fire_rejects_a_degenerate_horizon() {
        // A NaN or non-positive horizon would make the travel-time solver
        // propagate nothing and the post-fire cascade silently no-op.
        let land = Landscape::uniform(8, 8, 30.0, 4);
        let weather = Weather {
            wind_speed_kmh: 30.0,
            wind_from_deg: 270.0,
            moisture: Moisture::DRY_SUMMER,
        };
        assert!(run_fire(&land, &weather, &[(4, 4)], f64::NAN).is_err());
        assert!(run_fire(&land, &weather, &[(4, 4)], 0.0).is_err());
        assert!(run_fire(&land, &weather, &[(4, 4)], -60.0).is_err());
    }
}
