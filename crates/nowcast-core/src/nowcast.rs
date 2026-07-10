//! The nowcast engine: combine static susceptibility with a dynamic trigger to
//! produce a time-varying hazard-probability field and threshold alerts.
//!
//! For each time step `t` and cell `c`:
//!
//! 1. Over rolling windows of `m = 1..=max_window_steps` ending at `t`,
//!    accumulate the water-input depth, form the mean intensity
//!    `I = depth / (m · dt)` and duration `D = m · dt`, and evaluate the I–D
//!    exceedance `E_m`. Take the worst (max) exceedance over all window lengths.
//! 2. Map that exceedance to a trigger factor `T ∈ [0, 1]`.
//! 3. Modulate susceptibility: `P(c, t) = susceptibility(c) · T`.
//!
//! Step 1 uses per-cell prefix sums so each window sum is O(1); the whole run is
//! O(cells · steps · max_window) time. The prefix buffer is computed **once**
//! (lazily, shared by `run`, `hazard_at` and `explain`) and costs
//! `8 · cells · (steps + 1)` bytes — the engine's dominant memory footprint, so
//! coarsen a fine grid to the forcing's resolution before running continental
//! rasters. `explain` on a cold engine skips the grid-wide buffer entirely and
//! prefixes only the queried cell.

use std::sync::OnceLock;

use crate::error::{Error, Result};
use crate::explain::Explanation;
use crate::forcing::Forcing;
use crate::grid::{GridDims, SusceptibilityMap};
use crate::threshold::IdThreshold;
use crate::trigger::TriggerModel;

/// Hazard-probability field for one time step (row-major, one value per cell).
#[derive(Debug, Clone)]
pub struct HazardField {
    pub step: usize,
    dims: GridDims,
    probability: Vec<f64>,
}

impl HazardField {
    /// Build a hazard field from a row-major probability vector.
    ///
    /// This is the general `susceptibility × trigger` combiner output and is
    /// public so other hazard paths (e.g. the flood provider's discharge trigger
    /// in `nowcast-rainflow`) can produce the same field type. Fails if the
    /// length does not match `dims` or any value lies outside `[0, 1]`.
    pub fn new(step: usize, dims: GridDims, probability: Vec<f64>) -> Result<Self> {
        if probability.len() != dims.len() {
            return Err(Error::GridSizeMismatch {
                expected: dims.len(),
                got: probability.len(),
                ncols: dims.ncols,
                nrows: dims.nrows,
            });
        }
        if let Some((cell, v)) = probability
            .iter()
            .enumerate()
            .find(|(_, v)| !(0.0..=1.0).contains(*v))
        {
            return Err(Error::InvalidParameter {
                name: "probability",
                reason: format!("hazard at cell {cell} is {v}, expected within [0, 1]"),
            });
        }
        Ok(Self {
            step,
            dims,
            probability,
        })
    }

    /// Build a field that carries `probability` on the `true` cells of a
    /// row-major mask and `0` elsewhere — the shared "downscale a coarse alert
    /// onto a physical footprint" combiner used by the refinement adapters
    /// (hydroflux inundation depth, swarm debris-flow runout). Fails if the mask
    /// length does not match `dims` or `probability` lies outside `[0, 1]`.
    pub fn masked(step: usize, dims: GridDims, mask: &[bool], probability: f64) -> Result<Self> {
        if mask.len() != dims.len() {
            return Err(Error::GridSizeMismatch {
                expected: dims.len(),
                got: mask.len(),
                ncols: dims.ncols,
                nrows: dims.nrows,
            });
        }
        if !(0.0..=1.0).contains(&probability) {
            return Err(Error::InvalidParameter {
                name: "probability",
                reason: format!("mask probability is {probability}, expected within [0, 1]"),
            });
        }
        Ok(Self {
            step,
            dims,
            probability: mask
                .iter()
                .map(|&hit| if hit { probability } else { 0.0 })
                .collect(),
        })
    }

    pub fn dims(&self) -> GridDims {
        self.dims
    }

    pub fn probability(&self) -> &[f64] {
        &self.probability
    }

    /// Largest hazard probability anywhere on the field at this step.
    pub fn max_probability(&self) -> f64 {
        self.probability.iter().copied().fold(0.0, f64::max)
    }

    /// An [`Alert`] for this step if any cell reaches `level`, else `None`.
    /// Errors if `level` is not a probability in `[0, 1]` — with a `NaN` every
    /// `p >= level` comparison is false, so alerting would be silently disabled
    /// (the failure mode an alerting engine must never have).
    pub fn alert(&self, level: f64) -> Result<Option<Alert>> {
        validate_alert_level(level)?;
        let n_cells = self.probability.iter().filter(|&&p| p >= level).count();
        Ok((n_cells > 0).then(|| Alert {
            step: self.step,
            n_cells,
            fraction: n_cells as f64 / self.dims.len() as f64,
            max_probability: self.max_probability(),
        }))
    }
}

/// Alert levels are compared against hazard probabilities in `[0, 1]`; reject
/// anything else up front (shared by [`HazardField::alert`], the multi-trigger
/// and flood engines, and [`ensemble_hazard`](crate::ensemble_hazard)).
pub(crate) fn validate_alert_level(level: f64) -> Result<()> {
    if !level.is_finite() || !(0.0..=1.0).contains(&level) {
        return Err(Error::InvalidParameter {
            name: "alert_level",
            reason: format!("must be a probability in [0, 1], got {level}"),
        });
    }
    Ok(())
}

/// A step whose hazard field reached or exceeded the alert level somewhere.
#[derive(Debug, Clone, Copy, PartialEq)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct Alert {
    pub step: usize,
    /// Number of cells at or above the alert level.
    pub n_cells: usize,
    /// Fraction of the grid at or above the alert level, in `[0, 1]`.
    pub fraction: f64,
    /// Peak hazard probability at this step.
    pub max_probability: f64,
}

/// The combined susceptibility × trigger nowcast engine.
pub struct Nowcast<F: Forcing> {
    susceptibility: SusceptibilityMap,
    forcing: F,
    threshold: IdThreshold,
    trigger: TriggerModel,
    max_window_steps: usize,
    /// Lazily computed per-cell prefix sums, flat and row-major per cell:
    /// `prefix[c * (n_steps + 1) + t]` is the total depth over steps `0..t`.
    /// One allocation, computed at most once per engine (see module docs).
    prefix: OnceLock<Vec<f64>>,
}

impl<F: Forcing> Nowcast<F> {
    /// Build a nowcast. The forcing grid must match the susceptibility grid.
    ///
    /// `max_window_steps` bounds the longest rolling I–D window considered; it
    /// is clamped to the number of available steps.
    pub fn new(
        susceptibility: SusceptibilityMap,
        forcing: F,
        threshold: IdThreshold,
        trigger: TriggerModel,
        max_window_steps: usize,
    ) -> Result<Self> {
        let sd = susceptibility.dims();
        let fd = forcing.dims();
        if sd != fd {
            return Err(Error::GridMismatch {
                susc_cols: sd.ncols,
                susc_rows: sd.nrows,
                forc_cols: fd.ncols,
                forc_rows: fd.nrows,
            });
        }
        if max_window_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_window_steps",
                reason: "must be >= 1".to_string(),
            });
        }
        Ok(Self {
            susceptibility,
            forcing,
            threshold,
            trigger,
            max_window_steps,
            prefix: OnceLock::new(),
        })
    }

    fn n_cells(&self) -> usize {
        self.susceptibility.dims().len()
    }

    /// Length of one cell's prefix row (`n_steps + 1`).
    fn row_len(&self) -> usize {
        self.forcing.n_steps() + 1
    }

    /// Per-cell prefix sums of water-input depth, computed once and cached:
    /// `prefix[c * row_len + t]` is the total depth over steps `0..t`. Shared by
    /// `run`, `hazard_at` and (when already materialised) `explain`.
    fn depth_prefix_sums(&self) -> &[f64] {
        self.prefix.get_or_init(|| {
            let n_steps = self.forcing.n_steps();
            let row_len = n_steps + 1;
            let mut prefix = vec![0.0; self.n_cells() * row_len];
            for (c, row) in prefix.chunks_exact_mut(row_len).enumerate() {
                for t in 0..n_steps {
                    row[t + 1] = row[t] + self.forcing.depth_mm(c, t);
                }
            }
            prefix
        })
    }

    /// Prefix row of a single cell, without materialising the grid-wide buffer —
    /// the O(n_steps) path behind [`Nowcast::explain`] on a cold engine.
    fn cell_prefix(&self, cell: usize) -> Vec<f64> {
        let n_steps = self.forcing.n_steps();
        let mut row = vec![0.0; n_steps + 1];
        for t in 0..n_steps {
            row[t + 1] = row[t] + self.forcing.depth_mm(cell, t);
        }
        row
    }

    /// Worst exceedance and the rolling-window length (in steps) that produced
    /// it, over one cell's prefix `row` at step `t`.
    fn dominant_window(&self, row: &[f64], t: usize) -> (f64, usize) {
        self.threshold.worst_window(
            self.forcing.dt_hours(),
            self.max_window_steps.min(t + 1),
            |m| row[t + 1] - row[t + 1 - m],
        )
    }

    /// Exact attribution of the hazard at `cell` / `step`: its two factors and
    /// the intensity–duration window driving the trigger. See [`Explanation`].
    /// Errors if `cell` or `step` is out of range.
    ///
    /// Cost: O(n_steps) — it reuses the cached prefix buffer when some run
    /// already materialised it, and otherwise prefixes only the queried cell
    /// (never the whole grid).
    pub fn explain(&self, cell: usize, step: usize) -> Result<Explanation> {
        let n_cells = self.n_cells();
        if cell >= n_cells {
            return Err(Error::OutOfRange { name: "cell", index: cell, len: n_cells });
        }
        let n_steps = self.forcing.n_steps();
        if step >= n_steps {
            return Err(Error::OutOfRange { name: "step", index: step, len: n_steps });
        }
        let owned;
        let row: &[f64] = match self.prefix.get() {
            Some(prefix) => {
                let rl = self.row_len();
                &prefix[cell * rl..(cell + 1) * rl]
            }
            None => {
                owned = self.cell_prefix(cell);
                &owned
            }
        };
        let (e, m) = self.dominant_window(row, step);
        let dt = self.forcing.dt_hours();
        let duration_h = m as f64 * dt;
        let window_depth = row[step + 1] - row[step + 1 - m];
        let mean_intensity = window_depth / duration_h;
        Ok(Explanation::new(
            cell,
            step,
            self.susceptibility.get(cell),
            self.trigger.factor(e),
            duration_h,
            mean_intensity,
            self.threshold.critical_intensity(duration_h),
            e,
        ))
    }

    /// Counterfactual: the mean rainfall intensity (mm/h) sustained over
    /// `duration_h` that would lift `cell`'s hazard to `alert_level`. `Ok(None)`
    /// if the cell's susceptibility alone cannot reach the level (terrain-capped).
    /// Errors if `cell` is out of range or `duration_h` is not finite and > 0.
    pub fn intensity_to_alert(
        &self,
        cell: usize,
        alert_level: f64,
        duration_h: f64,
    ) -> Result<Option<f64>> {
        let n_cells = self.n_cells();
        if cell >= n_cells {
            return Err(Error::OutOfRange { name: "cell", index: cell, len: n_cells });
        }
        if !duration_h.is_finite() || duration_h <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "duration_h",
                reason: format!("must be finite and > 0, got {duration_h}"),
            });
        }
        let susc = self.susceptibility.get(cell);
        let factor_needed = alert_level / susc;
        if !(0.0..1.0).contains(&factor_needed) {
            return Ok(None); // susc too low: even a saturated trigger can't reach it
        }
        let e_needed = self.trigger.exceedance_for_factor(factor_needed);
        Ok(Some(self.threshold.intensity_for_exceedance(e_needed, duration_h)))
    }

    /// Compute the hazard field for a single time step. The prefix buffer is
    /// computed on first use and cached, so repeated calls (or a later `run`)
    /// do not recompute it. Errors if `step` is out of range.
    pub fn hazard_at(&self, step: usize) -> Result<HazardField> {
        let n_steps = self.forcing.n_steps();
        if step >= n_steps {
            return Err(Error::OutOfRange { name: "step", index: step, len: n_steps });
        }
        Ok(self.hazard_at_with(self.depth_prefix_sums(), step))
    }

    fn hazard_at_with(&self, prefix: &[f64], step: usize) -> HazardField {
        hazard_field(
            &self.susceptibility,
            &self.threshold,
            &self.trigger,
            self.forcing.dt_hours(),
            self.max_window_steps,
            prefix,
            self.row_len(),
            step,
        )
    }

    /// Run the full nowcast, returning one [`HazardField`] per time step.
    ///
    /// Steps are independent given the read-only prefix sums. With the
    /// `parallel` feature the per-step loop runs on Rayon; the closure captures
    /// only the shared prefix buffer and `Copy`/`Sync` parameters (never the
    /// forcing), so no `F: Sync` bound is imposed and the output is identical to
    /// the serial path (Rayon's indexed `collect` preserves order).
    pub fn run(&self) -> Vec<HazardField> {
        let prefix = self.depth_prefix_sums();
        let n_steps = self.forcing.n_steps();
        #[cfg(feature = "parallel")]
        {
            use rayon::prelude::*;
            let susc = &self.susceptibility;
            let threshold = self.threshold;
            let trigger = self.trigger;
            let dt = self.forcing.dt_hours();
            let mw = self.max_window_steps;
            let rl = self.row_len();
            (0..n_steps)
                .into_par_iter()
                .map(|t| hazard_field(susc, &threshold, &trigger, dt, mw, prefix, rl, t))
                .collect()
        }
        #[cfg(not(feature = "parallel"))]
        {
            (0..n_steps).map(|t| self.hazard_at_with(prefix, t)).collect()
        }
    }

    /// Run the nowcast and emit an [`Alert`] for every step whose peak hazard
    /// reaches `level`. Errors if `level` is not a probability in `[0, 1]`
    /// (validated before the run, so a bad level costs nothing).
    pub fn alerts(&self, level: f64) -> Result<Vec<Alert>> {
        validate_alert_level(level)?;
        Ok(self
            .run()
            .into_iter()
            .filter_map(|field| field.alert(level).expect("level validated above"))
            .collect())
    }
}

/// Worst I–D exceedance for cell `c` at step `t`, over all rolling window lengths
/// up to `max_window`, using the flat precomputed prefix sums (`row_len` values
/// per cell). Free function (no `F` in scope) so both the serial and the Rayon
/// drivers share one implementation.
fn max_exceedance_at(
    threshold: &IdThreshold,
    dt: f64,
    max_window: usize,
    prefix: &[f64],
    row_len: usize,
    c: usize,
    t: usize,
) -> f64 {
    let row = &prefix[c * row_len..(c + 1) * row_len];
    threshold
        .worst_window(dt, max_window.min(t + 1), |m| row[t + 1] - row[t + 1 - m])
        .0
}

/// Hazard field for one step: `susceptibility × trigger_factor(max exceedance)`
/// per cell. Captures only `Sync` inputs, so the parallel `run` need not bound
/// `F: Sync`.
#[allow(clippy::too_many_arguments)] // internal plumbing between the two drivers
fn hazard_field(
    susc: &SusceptibilityMap,
    threshold: &IdThreshold,
    trigger: &TriggerModel,
    dt: f64,
    max_window: usize,
    prefix: &[f64],
    row_len: usize,
    step: usize,
) -> HazardField {
    let dims = susc.dims();
    let mut probability = vec![0.0; dims.len()];
    for (c, p) in probability.iter_mut().enumerate() {
        let e = max_exceedance_at(threshold, dt, max_window, prefix, row_len, c, step);
        *p = susc.get(c) * trigger.factor(e);
    }
    HazardField {
        step,
        dims,
        probability,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::forcing::UniformRain;

    fn single_cell(depths: Vec<f64>, susc: f64) -> Nowcast<UniformRain> {
        let dims = GridDims::new(1, 1);
        let forcing = UniformRain::new(dims, 1.0, depths).unwrap();
        let susceptibility = SusceptibilityMap::uniform(dims, susc).unwrap();
        Nowcast::new(
            susceptibility,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            24,
        )
        .unwrap()
    }

    #[test]
    fn dry_series_gives_near_zero_hazard() {
        let nc = single_cell(vec![0.0; 10], 0.9);
        let fields = nc.run();
        for f in &fields {
            assert!(f.max_probability() < 0.05, "dry step too hot: {f:?}");
        }
    }

    #[test]
    fn heavy_burst_exceeds_threshold() {
        // 40 mm in one hour: I = 40 mm/h, D = 1 h. Caine I_crit(1) = 14.82 mm/h,
        // so E ≈ 2.7 → trigger factor near 1 → hazard ≈ susceptibility.
        let nc = single_cell(vec![0.0, 0.0, 40.0, 0.0], 0.8);
        let fields = nc.run();
        let peak = &fields[2];
        assert!(
            peak.max_probability() > 0.7,
            "expected hazard near susceptibility, got {}",
            peak.max_probability()
        );
        // Hazard relaxes after the burst passes out of the short windows.
        assert!(fields[3].max_probability() < peak.max_probability());
    }

    #[test]
    fn on_threshold_gives_half_susceptibility() {
        // Exactly Caine's critical 1-hour intensity → E = 1 → factor 0.5.
        let crit_1h = IdThreshold::caine().critical_intensity(1.0);
        let nc = single_cell(vec![crit_1h], 0.6);
        let p = nc.hazard_at(0).unwrap().max_probability();
        assert!((p - 0.3).abs() < 1e-6, "expected 0.5 * 0.6 = 0.3, got {p}");
    }

    #[test]
    fn zero_susceptibility_never_alerts() {
        let nc = single_cell(vec![100.0, 100.0], 0.0);
        assert!(nc.alerts(0.1).unwrap().is_empty());
        // A NaN or out-of-[0,1] level is an error, not silently zero alerts.
        assert!(nc.alerts(f64::NAN).is_err());
        assert!(nc.alerts(1.5).is_err());
        assert!(nc.alerts(-0.1).is_err());
    }

    #[test]
    fn alerts_start_at_burst_and_persist_via_antecedent_rain() {
        // A single 50 mm hour. The burst step alerts, and alerts persist into
        // following dry steps because longer rolling windows still carry that
        // depth above the I-D curve (antecedent rainfall) — the intended,
        // physically meaningful behaviour of the multi-window scheme.
        let nc = single_cell(vec![0.0, 50.0, 0.0, 0.0], 0.9);
        let alerts = nc.alerts(0.5).unwrap();
        let steps: Vec<usize> = alerts.iter().map(|a| a.step).collect();

        assert!(!steps.contains(&0), "no rain has fallen yet at step 0");
        assert!(steps.contains(&1), "the 50 mm burst must alert");
        assert_eq!(*steps.first().unwrap(), 1, "first alert is the burst step");
        // The peak is at the burst and relaxes afterwards as the window dilutes.
        assert!(alerts[0].max_probability >= alerts.last().unwrap().max_probability);
        assert_eq!(alerts[0].n_cells, 1);
        assert!((alerts[0].fraction - 1.0).abs() < 1e-9);
    }

    #[test]
    fn explain_attributes_a_heavy_burst_to_terrain_cap() {
        use crate::explain::Driver;
        // 40 mm in one hour on susceptibility 0.8: trigger saturates, so the
        // hazard is capped by the terrain, not the weather.
        let nc = single_cell(vec![0.0, 0.0, 40.0, 0.0], 0.8);
        let ex = nc.explain(0, 2).unwrap();
        assert!(ex.exceedance > 1.0, "burst should exceed the curve");
        assert!(ex.trigger_factor > 0.9);
        assert!((ex.mean_intensity_mm_h - 40.0).abs() < 1.0);
        assert_eq!(ex.driver, Driver::TerrainLimited);
        assert!((ex.hazard - ex.susceptibility * ex.trigger_factor).abs() < 1e-12);
    }

    #[test]
    fn explain_attributes_a_dry_step_to_the_trigger() {
        use crate::explain::Driver;
        let nc = single_cell(vec![0.0, 0.0, 0.0], 0.9);
        let ex = nc.explain(0, 1).unwrap();
        assert!(ex.trigger_factor < 0.1, "no rain → weak trigger");
        assert_eq!(ex.driver, Driver::TriggerLimited);
    }

    #[test]
    fn counterfactual_intensity_to_alert() {
        let nc = single_cell(vec![0.0], 0.8);
        // Susceptible cell: an attainable rainfall reaches the 0.5 alert level.
        let i = nc.intensity_to_alert(0, 0.5, 1.0).unwrap().unwrap();
        assert!(i > 0.0 && i.is_finite());
        // A barely-susceptible cell can never reach 0.5 (terrain-capped at susc).
        let low = single_cell(vec![0.0], 0.3);
        assert!(low.intensity_to_alert(0, 0.5, 1.0).unwrap().is_none());
        // Out-of-range cell and a non-finite/non-positive duration are errors,
        // not a panic or a silently nonsensical Some(NaN)/Some(inf).
        assert!(nc.intensity_to_alert(999, 0.5, 1.0).is_err());
        assert!(nc.intensity_to_alert(0, 0.5, 0.0).is_err());
        assert!(nc.intensity_to_alert(0, 0.5, f64::NAN).is_err());
    }

    #[test]
    fn explain_is_identical_cold_and_warm() {
        // Cold: no run() yet, explain prefixes only the queried cell.
        // Warm: after run(), explain reads the cached grid-wide buffer.
        // Both paths must produce bit-identical attributions.
        let mk = || single_cell(vec![3.0, 12.0, 40.0, 0.0, 7.0], 0.7);
        let cold = mk();
        let cold_ex = cold.explain(0, 3).unwrap();
        let warm = mk();
        let _ = warm.run(); // materialises the cache
        let warm_ex = warm.explain(0, 3).unwrap();
        assert_eq!(cold_ex.hazard.to_bits(), warm_ex.hazard.to_bits());
        assert_eq!(cold_ex.exceedance.to_bits(), warm_ex.exceedance.to_bits());
        assert_eq!(cold_ex.critical_duration_h, warm_ex.critical_duration_h);
        assert_eq!(
            cold_ex.mean_intensity_mm_h.to_bits(),
            warm_ex.mean_intensity_mm_h.to_bits()
        );
    }

    #[test]
    fn explain_rejects_out_of_range_indices() {
        let nc = single_cell(vec![10.0, 20.0], 0.5); // 1 cell, 2 steps
        assert!(nc.explain(0, 1).is_ok());
        assert!(nc.explain(1, 0).is_err(), "cell 1 does not exist");
        assert!(nc.explain(0, 2).is_err(), "step 2 does not exist");
    }

    #[test]
    fn hazard_at_rejects_out_of_range_step() {
        let nc = single_cell(vec![10.0, 20.0], 0.5); // 1 cell, 2 steps
        assert!(nc.hazard_at(1).is_ok());
        assert!(nc.hazard_at(2).is_err(), "step 2 does not exist");
    }

    #[test]
    fn masked_field_carries_probability_on_the_footprint() {
        let dims = GridDims::new(2, 2);
        let f = HazardField::masked(3, dims, &[true, false, false, true], 0.8).unwrap();
        assert_eq!(f.probability(), &[0.8, 0.0, 0.0, 0.8]);
        assert_eq!(f.step, 3);
        // Wrong mask length and out-of-range probability are rejected.
        assert!(HazardField::masked(0, dims, &[true; 3], 0.8).is_err());
        assert!(HazardField::masked(0, dims, &[true; 4], 1.2).is_err());
        assert!(HazardField::masked(0, dims, &[true; 4], f64::NAN).is_err());
    }

    #[test]
    fn grid_mismatch_is_rejected() {
        let forcing = UniformRain::new(GridDims::new(2, 2), 1.0, vec![1.0]).unwrap();
        let susc = SusceptibilityMap::uniform(GridDims::new(3, 3), 0.5).unwrap();
        let err = Nowcast::new(
            susc,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            6,
        );
        assert!(err.is_err());
    }

    #[test]
    fn spatial_field_modulates_by_susceptibility() {
        // Two cells, different susceptibility, same (heavy) uniform rain.
        let dims = GridDims::new(2, 1);
        let forcing = UniformRain::new(dims, 1.0, vec![50.0]).unwrap();
        let susc = SusceptibilityMap::new(dims, vec![0.2, 0.9]).unwrap();
        let nc = Nowcast::new(
            susc,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            6,
        )
        .unwrap();
        let field = nc.hazard_at(0).unwrap();
        let p = field.probability();
        assert!(p[1] > p[0], "higher susceptibility must yield higher hazard");
        // Same trigger factor on both cells → ratio equals susceptibility ratio.
        assert!((p[1] / p[0] - 0.9 / 0.2).abs() < 1e-9);
    }
}
