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
    Boundaries2D, Conserved2D, GRAVITY,
};
use ndarray::Array2;
use nowcast_core::GridDims;

// Re-export the building blocks so callers need only this crate.
pub use hydroflux_solver_2d::{Boundary, Conserved2D as State, Mesh2D, PointSource, H_DRY};

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
    fn from_states(states: &Array2<Conserved2D>) -> Self {
        let (nr, nc) = states.dim();
        let mut depth = Vec::with_capacity(nr * nc);
        for i in 0..nr {
            for j in 0..nc {
                depth.push(states[[i, j]].h.max(0.0));
            }
        }
        Self {
            dims: GridDims::new(nc, nr),
            depth,
        }
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
    pub fn refined_hazard(&self, nowcast_prob: f64, min_depth: f64) -> Vec<f64> {
        self.depth
            .iter()
            .map(|&h| if h >= min_depth { nowcast_prob } else { 0.0 })
            .collect()
    }
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
    pub fn new(mesh: Mesh2D, boundaries: Boundaries2D, duration_s: f64) -> Self {
        Self {
            mesh,
            boundaries,
            duration_s,
            cfl: 0.4,
            dry_tol: H_DRY,
            max_steps: 200_000,
        }
    }

    pub fn with_cfl(mut self, cfl: f64) -> Self {
        self.cfl = cfl;
        self
    }

    /// Integrate, applying `source(&mut states, dt)` each step before the update.
    fn integrate<F: FnMut(&mut Array2<Conserved2D>, f64)>(&self, mut source: F) -> DepthField {
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
        DepthField::from_states(&states)
    }

    /// Run with a uniform rainfall rate (m/s) over the whole domain.
    pub fn run_rain(&self, rate_m_s: f64) -> DepthField {
        self.integrate(|s, dt| apply_rain(s, rate_m_s, dt))
    }

    /// Run with volumetric point inflows (m³/s) — e.g. the alerting discharge
    /// entering the basin at its inlet cell(s).
    pub fn run_point_sources(&self, sources: &[PointSource]) -> DepthField {
        let (dx, dy) = (self.mesh.dx, self.mesh.dy);
        self.integrate(|s, dt| apply_point_sources(s, sources, dt, dx, dy))
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
        let inund = Inundation::new(flat_mesh(6, 6), Boundaries2D::WALLS, 200.0);
        let rate = 1.0e-3; // m/s
        let field = inund.run_rain(rate);
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
        let inund = Inundation::new(flat_mesh(9, 9), Boundaries2D::WALLS, 120.0);
        let src = vec![PointSource { row: 4, col: 4, q_mass: 5.0 }]; // 5 m³/s inflow
        let field = inund.run_point_sources(&src);
        assert!(field.max_depth() > 0.0, "no water from the inflow");
        assert!(field.inundated_fraction(H_DRY) > 0.0);
    }

    #[test]
    fn refined_hazard_gates_by_physical_inundation() {
        let inund = Inundation::new(flat_mesh(4, 4), Boundaries2D::WALLS, 100.0);
        let field = inund.run_rain(1.0e-3); // ~0.1 m everywhere
        let refined = field.refined_hazard(0.8, 0.01);
        assert!(refined.iter().all(|&p| (p - 0.8).abs() < 1e-9));
    }

    #[test]
    fn discharge_conversion() {
        // 86.4 mm/day over 1 km² = 1 m³/s.
        let q = discharge_to_inflow_m3s(86.4, 1.0);
        assert!((q - 1.0).abs() < 1e-9);
    }
}
