//! # nowcast-hydroflux
//!
//! v0.2 **physical refinement** of a flood alert. The nowcast is fast and coarse:
//! it flags *when* and roughly *where* discharge crosses a flood threshold. This
//! crate drives the `hydroflux` 2D shallow-water solver (HLLC + Audusse + Manning)
//! on the local DEM of an alerted zone to turn that probabilistic flag into a
//! **physical inundation depth field** — where the water actually goes, and how
//! deep.
//!
//! The coupling is one-way and on-demand: run the (expensive) hydrodynamics only
//! where the (cheap) nowcast already raised an alert. [`discharge_to_inflow_m3s`]
//! converts a routed discharge (mm/day, e.g. from `nowcast-rainflow`) into the
//! volumetric inflow the solver consumes, and [`DepthField::refined_hazard`]
//! downscales the coarse alert probability onto the fine inundation footprint.
//!
//! `nowcast-core` stays dependency-light and offline; this adapter pulls the
//! hydrodynamic stack, so it builds online once (then from cache).

use hydroflux_solver_2d::{
    apply_point_sources, apply_rain, cfl_time_step_with_bcs, manning_friction_step, ssprk2_step,
    Conserved2D, GRAVITY,
};
use ndarray::Array2;
use nowcast_core::{Error, GridDims, HazardField, Result};

// Re-export the building blocks so callers need only this crate —
// `Inundation::new` takes a `Boundaries2D` (built from `Boundary` per `Side`),
// so those must be constructible without importing the solver directly.
pub use hydroflux_solver_2d::{
    Boundaries2D, Boundary, Conserved2D as State, Mesh2D, PointSource, Side, H_DRY,
};

/// Convert a routed discharge (mm/day spread over `area_km2`) into a volumetric
/// inflow (m³/s) for a Hydroflux point source.
pub fn discharge_to_inflow_m3s(mm_day: f64, area_km2: f64) -> f64 {
    mm_day * 1e-3 * (area_km2 * 1e6) / 86_400.0
}

/// Inundation depth (m) per cell, row-major, matching [`GridDims`].
#[derive(Debug, Clone)]
pub struct DepthField {
    dims: GridDims,
    depth: Vec<f64>,
}

impl DepthField {
    /// Builds the field and reports whether any cell went non-finite (`NaN`/`inf`,
    /// a numerically unstable solver state). `f64::max` silently turns a `NaN`
    /// depth into `0.0` (looks "dry"), so that laundering is detected here
    /// rather than passed through: unstable cells are still reported as `0.0`
    /// (a representable placeholder), but the caller learns not to trust the
    /// field via [`IntegrationStats::unstable`].
    fn from_states(states: &Array2<Conserved2D>) -> (Self, bool) {
        let (nr, nc) = states.dim();
        let mut depth = Vec::with_capacity(nr * nc);
        let mut unstable = false;
        for i in 0..nr {
            for j in 0..nc {
                let h = states[[i, j]].h;
                if !h.is_finite() {
                    unstable = true;
                    depth.push(0.0);
                } else {
                    depth.push(h.max(0.0));
                }
            }
        }
        (
            Self {
                dims: GridDims::new(nc, nr),
                depth,
            },
            unstable,
        )
    }

    pub fn dims(&self) -> GridDims {
        self.dims
    }

    pub fn depth(&self) -> &[f64] {
        &self.depth
    }

    /// Deepest water anywhere on the field (m).
    pub fn max_depth(&self) -> f64 {
        self.depth.iter().copied().fold(0.0, f64::max)
    }

    /// Mean water depth over all cells (m).
    pub fn mean_depth(&self) -> f64 {
        self.depth.iter().sum::<f64>() / self.depth.len() as f64
    }

    /// Fraction of the grid inundated at or above `min_depth` (m), in `[0, 1]`.
    pub fn inundated_fraction(&self, min_depth: f64) -> f64 {
        let n = self.depth.iter().filter(|&&h| h >= min_depth).count();
        n as f64 / self.depth.len() as f64
    }

    /// Downscale a coarse nowcast flood probability onto this fine field: the
    /// probability where physically inundated (depth ≥ `min_depth`), else 0.
    /// Same shape as `nowcast-swarm`'s runout refinement — both go through
    /// [`HazardField::masked`], so `nowcast_prob` outside `[0, 1]` is an error,
    /// not a panic.
    pub fn refined_hazard(
        &self,
        step: usize,
        nowcast_prob: f64,
        min_depth: f64,
    ) -> nowcast_core::Result<HazardField> {
        let mask: Vec<bool> = self.depth.iter().map(|&h| h >= min_depth).collect();
        HazardField::masked(step, self.dims, &mask, nowcast_prob)
    }
}

/// What the integration actually did — so a capped run can never masquerade as
/// a completed one.
#[derive(Debug, Clone, Copy)]
pub struct IntegrationStats {
    /// Simulated time actually reached (s); equals the requested duration when
    /// `truncated` is false.
    pub t_reached_s: f64,
    /// Time steps taken.
    pub steps: usize,
    /// `true` if the step cap was hit before reaching the requested duration —
    /// the returned depth field is then a **partial** inundation.
    pub truncated: bool,
    /// `true` if the solver produced a non-finite (`NaN`/`inf`) depth in at
    /// least one cell — a numerically unstable run (degenerate mesh, CFL edge
    /// case). The returned depth field reports those cells as `0.0`, but that
    /// value is a placeholder, not a physical result: do not use the field for
    /// an alert when this is `true`.
    pub unstable: bool,
}

/// A configured 2D inundation run over a DEM mesh.
pub struct Inundation {
    mesh: Mesh2D,
    boundaries: Boundaries2D,
    duration_s: f64,
    cfl: f64,
    dry_tol: f64,
    max_steps: usize,
}

impl Inundation {
    /// New run on `mesh` with the given boundaries, integrated to `duration_s`
    /// seconds. Defaults: CFL 0.4, dry tolerance [`H_DRY`], 200 000-step cap.
    /// Errors on a non-positive or non-finite duration.
    pub fn new(mesh: Mesh2D, boundaries: Boundaries2D, duration_s: f64) -> Result<Self> {
        if !duration_s.is_finite() || duration_s <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "duration_s",
                reason: format!("must be finite and > 0, got {duration_s}"),
            });
        }
        Ok(Self {
            mesh,
            boundaries,
            duration_s,
            cfl: 0.4,
            dry_tol: H_DRY,
            max_steps: 200_000,
        })
    }

    /// CFL number in `(0, 1]`.
    pub fn with_cfl(mut self, cfl: f64) -> Result<Self> {
        if !cfl.is_finite() || cfl <= 0.0 || cfl > 1.0 {
            return Err(Error::InvalidParameter {
                name: "cfl",
                reason: format!("must be within (0, 1], got {cfl}"),
            });
        }
        self.cfl = cfl;
        Ok(self)
    }

    /// Step cap for the integration (≥ 1). When the cap is hit,
    /// [`IntegrationStats::truncated`] reports it — raise the cap or shorten
    /// the duration.
    pub fn with_max_steps(mut self, max_steps: usize) -> Result<Self> {
        if max_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_steps",
                reason: "must be >= 1".into(),
            });
        }
        self.max_steps = max_steps;
        Ok(self)
    }

    /// Dry-cell tolerance (m, ≥ 0) for the friction step.
    pub fn with_dry_tol(mut self, dry_tol: f64) -> Result<Self> {
        if !dry_tol.is_finite() || dry_tol < 0.0 {
            return Err(Error::InvalidParameter {
                name: "dry_tol",
                reason: format!("must be finite and >= 0, got {dry_tol}"),
            });
        }
        self.dry_tol = dry_tol;
        Ok(self)
    }

    /// Integrate, applying `source(&mut states, dt)` each step before the update.
    fn integrate<F: FnMut(&mut Array2<Conserved2D>, f64)>(
        &self,
        mut source: F,
    ) -> (DepthField, IntegrationStats) {
        let (nr, nc) = self.mesh.bed.dim();
        let mut states = Array2::from_elem((nr, nc), Conserved2D::dry());
        // Fallback step for the dry start, before any wave exists.
        let fallback = self.cfl * self.mesh.dx.min(self.mesh.dy) / (GRAVITY * 0.1).sqrt();
        let (mut t, mut steps) = (0.0_f64, 0usize);
        while t < self.duration_s && steps < self.max_steps {
            let mut dt = cfl_time_step_with_bcs(&states, &self.mesh, self.boundaries, self.cfl);
            if !dt.is_finite() || dt <= 0.0 {
                dt = fallback;
            }
            dt = dt.min(self.duration_s - t);
            source(&mut states, dt);
            ssprk2_step(&mut states, &self.mesh, self.boundaries, dt);
            manning_friction_step(&mut states, &self.mesh, dt, self.dry_tol);
            t += dt;
            steps += 1;
        }
        let (field, unstable) = DepthField::from_states(&states);
        let stats = IntegrationStats {
            t_reached_s: t,
            steps,
            truncated: t < self.duration_s,
            unstable,
        };
        (field, stats)
    }

    /// Run with a uniform rainfall rate (m/s) over the whole domain. The stats
    /// say how far the integration actually got — check
    /// [`IntegrationStats::truncated`] before trusting the field in an alert.
    pub fn run_rain(&self, rate_m_s: f64) -> (DepthField, IntegrationStats) {
        self.integrate(|s, dt| apply_rain(s, rate_m_s, dt))
    }

    /// Run with volumetric point inflows (m³/s) — e.g. the alerting discharge
    /// entering the basin at its inlet cell(s). The stats say how far the
    /// integration actually got — check [`IntegrationStats::truncated`] before
    /// trusting the field in an alert.
    ///
    /// Errors if a source sits outside the mesh (`row`/`col` out of bounds —
    /// the realistic off-by-one when placing the inlet cell from a misaligned
    /// coarse grid used to panic deep inside the solver) or carries a
    /// non-finite `q_mass` (a `NaN` inflow would be laundered to "dry" by the
    /// solver's `max(0.0)` on every step, yielding a completely dry
    /// "inundation" with `unstable = false`).
    pub fn run_point_sources(
        &self,
        sources: &[PointSource],
    ) -> Result<(DepthField, IntegrationStats)> {
        let (nr, nc) = self.mesh.bed.dim();
        for (i, s) in sources.iter().enumerate() {
            if s.row >= nr || s.col >= nc {
                return Err(Error::InvalidParameter {
                    name: "sources",
                    reason: format!(
                        "source {i} at (row {}, col {}) lies outside the {nr}x{nc} mesh",
                        s.row, s.col
                    ),
                });
            }
            if !s.q_mass.is_finite() {
                return Err(Error::InvalidParameter {
                    name: "sources",
                    reason: format!("source {i} has a non-finite inflow q_mass = {}", s.q_mass),
                });
            }
        }
        let (dx, dy) = (self.mesh.dx, self.mesh.dy);
        Ok(self.integrate(|s, dt| apply_point_sources(s, sources, dt, dx, dy)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hydroflux_solver_2d::Boundaries2D;
    use ndarray::Array2;

    fn flat_mesh(nr: usize, nc: usize) -> Mesh2D {
        Mesh2D::new(Array2::zeros((nr, nc)), 10.0, 10.0, 0.03)
    }

    #[test]
    fn closed_basin_rain_conserves_mass() {
        // Walls everywhere + flat bed: rain has nowhere to go, so the mean depth
        // equals (rate × duration). A clean mass-conservation check.
        let inund = Inundation::new(flat_mesh(6, 6), Boundaries2D::WALLS, 200.0).unwrap();
        let rate = 1.0e-3; // m/s
        let (field, stats) = inund.run_rain(rate);
        assert!(!stats.truncated, "6×6 for 200 s must finish well under the cap");
        assert!((stats.t_reached_s - 200.0).abs() < 1e-9);
        let expected = rate * 200.0; // 0.2 m
        assert!(
            (field.mean_depth() - expected).abs() < 0.02,
            "mean depth {} vs expected {expected}",
            field.mean_depth()
        );
        assert!((field.inundated_fraction(0.01) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn point_source_floods_the_domain() {
        let inund = Inundation::new(flat_mesh(9, 9), Boundaries2D::WALLS, 120.0).unwrap();
        let src = vec![PointSource { row: 4, col: 4, q_mass: 5.0 }]; // 5 m³/s inflow
        let (field, stats) = inund.run_point_sources(&src).unwrap();
        assert!(!stats.truncated);
        assert!(field.max_depth() > 0.0, "no water from the inflow");
        assert!(field.inundated_fraction(H_DRY) > 0.0);
    }

    #[test]
    fn point_sources_outside_the_mesh_or_non_finite_are_rejected() {
        let inund = Inundation::new(flat_mesh(4, 4), Boundaries2D::WALLS, 10.0).unwrap();
        // Off-by-one outside the mesh used to panic inside the solver.
        assert!(inund
            .run_point_sources(&[PointSource { row: 99, col: 0, q_mass: 1.0 }])
            .is_err());
        assert!(inund
            .run_point_sources(&[PointSource { row: 0, col: 4, q_mass: 1.0 }])
            .is_err());
        // A NaN inflow used to be laundered to "dry" with unstable = false.
        assert!(inund
            .run_point_sources(&[PointSource { row: 1, col: 1, q_mass: f64::NAN }])
            .is_err());
        // A valid source still runs.
        assert!(inund
            .run_point_sources(&[PointSource { row: 1, col: 1, q_mass: 1.0 }])
            .is_ok());
    }

    #[test]
    fn refined_hazard_gates_by_physical_inundation() {
        let inund = Inundation::new(flat_mesh(4, 4), Boundaries2D::WALLS, 100.0).unwrap();
        let (field, _) = inund.run_rain(1.0e-3); // ~0.1 m everywhere
        let refined = field.refined_hazard(0, 0.8, 0.01).unwrap();
        assert!(refined.probability().iter().all(|&p| (p - 0.8).abs() < 1e-9));
        // A probability outside [0,1] is rejected, not a panic.
        assert!(field.refined_hazard(0, 1.5, 0.01).is_err());
    }

    #[test]
    fn a_capped_run_reports_truncation_instead_of_lying() {
        // One step is nowhere near 300 s of simulated time: the run must say so.
        let inund = Inundation::new(flat_mesh(6, 6), Boundaries2D::WALLS, 300.0)
            .unwrap()
            .with_max_steps(1)
            .unwrap();
        let (_, stats) = inund.run_rain(1.0e-3);
        assert!(stats.truncated, "1-step cap cannot reach 300 s");
        assert_eq!(stats.steps, 1);
        assert!(stats.t_reached_s < 300.0);
    }

    #[test]
    fn builders_reject_bad_parameters() {
        let mk = || Inundation::new(flat_mesh(3, 3), Boundaries2D::WALLS, 10.0).unwrap();
        assert!(Inundation::new(flat_mesh(3, 3), Boundaries2D::WALLS, 0.0).is_err());
        assert!(Inundation::new(flat_mesh(3, 3), Boundaries2D::WALLS, f64::NAN).is_err());
        assert!(mk().with_cfl(0.0).is_err());
        assert!(mk().with_cfl(1.5).is_err());
        assert!(mk().with_max_steps(0).is_err());
        assert!(mk().with_dry_tol(-1.0).is_err());
        assert!(mk().with_cfl(0.9).and_then(|i| i.with_max_steps(10)).is_ok());
    }

    #[test]
    fn nan_depth_is_reported_unstable_not_silently_dry() {
        let mut states = Array2::from_elem((2, 2), Conserved2D::dry());
        states[[0, 0]] = Conserved2D::new(f64::NAN, 0.0, 0.0);
        states[[0, 1]] = Conserved2D::new(0.5, 0.0, 0.0);
        let (field, unstable) = DepthField::from_states(&states);
        assert!(unstable, "a NaN cell must flip the unstable flag");
        assert_eq!(field.depth()[0], 0.0, "NaN is reported as 0.0, not left as NaN");
        assert_eq!(field.depth()[1], 0.5, "a healthy cell is unaffected");
    }

    #[test]
    fn discharge_conversion() {
        // 86.4 mm/day over 1 km² = 1 m³/s.
        let q = discharge_to_inflow_m3s(86.4, 1.0);
        assert!((q - 1.0).abs() < 1e-9);
    }
}
