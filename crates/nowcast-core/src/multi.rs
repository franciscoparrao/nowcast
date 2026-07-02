//! Composable triggers: combine several dynamic signals (rainfall, ground
//! deformation, …) into one hazard.
//!
//! The single-signal [`Nowcast`](crate::Nowcast) hard-wires the rainfall
//! intensity–duration trigger. This module generalises that: a [`Trigger`] is
//! anything that yields a factor in `[0, 1]` per cell and step, and a
//! [`MultiNowcast`] fuses several of them (e.g. *rain I–D* **or** *InSAR
//! deformation rate*) before modulating susceptibility:
//!
//! ```text
//!     hazard(cell, t) = susceptibility(cell) × combine(trigger_1, …, trigger_k)
//! ```
//!
//! Concrete triggers here: [`IdTrigger`] (rainfall I–D, rolling windows) and
//! [`ThresholdTrigger`] (a duration-independent value/threshold exceedance —
//! used for deformation rate, discharge, anything already a rate).

use crate::error::{Error, Result};
use crate::forcing::Forcing;
use crate::grid::{GridDims, SusceptibilityMap};
use crate::nowcast::{Alert, HazardField};
use crate::threshold::IdThreshold;
use crate::trigger::TriggerModel;

/// A dynamic trigger: a factor in `[0, 1]` per cell and step.
pub trait Trigger {
    fn dims(&self) -> GridDims;
    fn n_steps(&self) -> usize;
    /// Trigger factor at `cell` / `step`, in `[0, 1]`.
    fn factor(&self, cell: usize, step: usize) -> f64;
}

/// Rainfall intensity–duration trigger: the rolling-window max exceedance of
/// `I = a·D^-b`, mapped through a [`TriggerModel`]. The composable form of the
/// logic inside [`Nowcast`](crate::Nowcast).
pub struct IdTrigger<F: Forcing> {
    forcing: F,
    threshold: IdThreshold,
    model: TriggerModel,
    max_window_steps: usize,
    /// Flat per-cell prefix sums (`prefix[c * (n_steps + 1) + t]` = total depth
    /// over steps `0..t`), the same single-allocation layout the batch engine
    /// caches.
    prefix: Vec<f64>,
}

impl<F: Forcing> IdTrigger<F> {
    pub fn new(
        forcing: F,
        threshold: IdThreshold,
        model: TriggerModel,
        max_window_steps: usize,
    ) -> Result<Self> {
        if max_window_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_window_steps",
                reason: "must be >= 1".into(),
            });
        }
        let n_steps = forcing.n_steps();
        let row_len = n_steps + 1;
        let n_cells = forcing.dims().len();
        let mut prefix = vec![0.0; n_cells * row_len];
        for (c, row) in prefix.chunks_exact_mut(row_len).enumerate() {
            for t in 0..n_steps {
                row[t + 1] = row[t] + forcing.depth_mm(c, t);
            }
        }
        Ok(Self {
            forcing,
            threshold,
            model,
            max_window_steps,
            prefix,
        })
    }
}

impl<F: Forcing> Trigger for IdTrigger<F> {
    fn dims(&self) -> GridDims {
        self.forcing.dims()
    }
    fn n_steps(&self) -> usize {
        self.forcing.n_steps()
    }
    fn factor(&self, cell: usize, step: usize) -> f64 {
        let row_len = self.forcing.n_steps() + 1;
        let row = &self.prefix[cell * row_len..(cell + 1) * row_len];
        let best = self
            .threshold
            .worst_window(
                self.forcing.dt_hours(),
                self.max_window_steps.min(step + 1),
                |m| row[step + 1] - row[step + 1 - m],
            )
            .0;
        self.model.factor(best)
    }
}

/// Spatially variable intensity–duration thresholds: one [`IdThreshold`] per
/// cell, sharing the same rolling-window kernel as [`IdTrigger`].
///
/// The composable form for **regionalised threshold maps** — the literature
/// shows strong I–D variation with lithology and climate zone, and a single
/// global `(a, b)` is exactly what the Maipo backtest found wanting (Caine
/// never fires; the regional intercept does). Build the per-cell curves from a
/// classified map upstream and hand them in row-major cell order.
pub struct IdMapTrigger<F: Forcing> {
    forcing: F,
    /// One threshold per cell, row-major.
    thresholds: Vec<IdThreshold>,
    model: TriggerModel,
    max_window_steps: usize,
    /// Flat per-cell prefix sums (`prefix[c * (n_steps + 1) + t]`).
    prefix: Vec<f64>,
}

impl<F: Forcing> IdMapTrigger<F> {
    pub fn new(
        forcing: F,
        thresholds: Vec<IdThreshold>,
        model: TriggerModel,
        max_window_steps: usize,
    ) -> Result<Self> {
        if max_window_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_window_steps",
                reason: "must be >= 1".into(),
            });
        }
        let n_cells = forcing.dims().len();
        if thresholds.len() != n_cells {
            return Err(Error::GridSizeMismatch {
                expected: n_cells,
                got: thresholds.len(),
                ncols: forcing.dims().ncols,
                nrows: forcing.dims().nrows,
            });
        }
        let n_steps = forcing.n_steps();
        let row_len = n_steps + 1;
        let mut prefix = vec![0.0; n_cells * row_len];
        for (c, row) in prefix.chunks_exact_mut(row_len).enumerate() {
            for t in 0..n_steps {
                row[t + 1] = row[t] + forcing.depth_mm(c, t);
            }
        }
        Ok(Self {
            forcing,
            thresholds,
            model,
            max_window_steps,
            prefix,
        })
    }
}

impl<F: Forcing> Trigger for IdMapTrigger<F> {
    fn dims(&self) -> GridDims {
        self.forcing.dims()
    }
    fn n_steps(&self) -> usize {
        self.forcing.n_steps()
    }
    fn factor(&self, cell: usize, step: usize) -> f64 {
        let row_len = self.forcing.n_steps() + 1;
        let row = &self.prefix[cell * row_len..(cell + 1) * row_len];
        let best = self.thresholds[cell]
            .worst_window(
                self.forcing.dt_hours(),
                self.max_window_steps.min(step + 1),
                |m| row[step + 1] - row[step + 1 - m],
            )
            .0;
        self.model.factor(best)
    }
}

/// Antecedent-wetness trigger: an exponentially decayed **antecedent
/// precipitation index** (API) per cell, mapped through a [`TriggerModel`]
/// against a critical wetness.
///
/// `API_t = decay · API_{t−1} + depth_{t−1}` with `decay ∈ (0, 1)` per step —
/// the index at step `t` summarises the water that fell **before** `t`
/// (half-life `= dt · ln 2 / (−ln decay)`), deliberately excluding the current
/// step so it never double-counts the burst the I–D trigger scores.
///
/// This is the "cause" half of a dual cause–trigger threshold (Bogaard & Greco
/// 2018): compose it with the storm-scale [`IdTrigger`] under
/// [`Combine::Product`] so the hazard needs BOTH a wet antecedent state and a
/// triggering burst — the standard recipe against the wet-season structural
/// false alarms the Maipo backtest measured (FAR ≈ 0.9).
pub struct AntecedentTrigger<F: Forcing> {
    forcing: F,
    crit_mm: f64,
    model: TriggerModel,
    /// Flat per-cell API series (`api[c * n_steps + t]`), precomputed because
    /// the recursion is sequential per cell.
    api: Vec<f64>,
}

impl<F: Forcing> AntecedentTrigger<F> {
    /// `decay` is the per-step retention in `(0, 1)`; `crit_mm` the antecedent
    /// depth (mm) at which the factor reaches 0.5.
    pub fn new(forcing: F, decay: f64, crit_mm: f64, model: TriggerModel) -> Result<Self> {
        if !decay.is_finite() || decay <= 0.0 || decay >= 1.0 {
            return Err(Error::InvalidParameter {
                name: "decay",
                reason: format!("per-step retention must be within (0, 1), got {decay}"),
            });
        }
        if !crit_mm.is_finite() || crit_mm <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "crit_mm",
                reason: format!("critical antecedent depth must be finite and > 0, got {crit_mm}"),
            });
        }
        let n_cells = forcing.dims().len();
        let n_steps = forcing.n_steps();
        let mut api = vec![0.0; n_cells * n_steps];
        for c in 0..n_cells {
            let row = &mut api[c * n_steps..(c + 1) * n_steps];
            let mut state = 0.0;
            for (t, slot) in row.iter_mut().enumerate() {
                *slot = state; // antecedent: everything BEFORE step t
                state = decay * state + forcing.depth_mm(c, t);
            }
        }
        Ok(Self {
            forcing,
            crit_mm,
            model,
            api,
        })
    }

    /// The antecedent precipitation index (mm) at a cell/step.
    pub fn api(&self, cell: usize, step: usize) -> f64 {
        self.api[cell * self.forcing.n_steps() + step]
    }
}

impl<F: Forcing> Trigger for AntecedentTrigger<F> {
    fn dims(&self) -> GridDims {
        self.forcing.dims()
    }
    fn n_steps(&self) -> usize {
        self.forcing.n_steps()
    }
    fn factor(&self, cell: usize, step: usize) -> f64 {
        self.model.factor(self.api(cell, step) / self.crit_mm)
    }
}

/// Duration-independent threshold trigger: `factor = model(value / crit)`, where
/// `value` is the forcing's per-cell per-step scalar. Use for InSAR deformation
/// rate (mm/yr), routed discharge, or any signal already expressed as a rate.
pub struct ThresholdTrigger<F: Forcing> {
    forcing: F,
    crit: f64,
    model: TriggerModel,
}

impl<F: Forcing> ThresholdTrigger<F> {
    pub fn new(forcing: F, crit: f64, model: TriggerModel) -> Result<Self> {
        if !crit.is_finite() || crit <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "crit",
                reason: format!("threshold must be finite and > 0, got {crit}"),
            });
        }
        Ok(Self {
            forcing,
            crit,
            model,
        })
    }
}

impl<F: Forcing> Trigger for ThresholdTrigger<F> {
    fn dims(&self) -> GridDims {
        self.forcing.dims()
    }
    fn n_steps(&self) -> usize {
        self.forcing.n_steps()
    }
    fn factor(&self, cell: usize, step: usize) -> f64 {
        self.model.factor(self.forcing.depth_mm(cell, step) / self.crit)
    }
}

/// How to fuse several trigger factors into one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Combine {
    /// The strongest trigger wins (`max`).
    Max,
    /// Independent triggers, any may fire: `1 − ∏(1 − fᵢ)`.
    NoisyOr,
    /// All must concur: `∏ fᵢ`.
    Product,
}

impl Combine {
    pub fn apply(&self, factors: &[f64]) -> f64 {
        match self {
            Combine::Max => factors.iter().copied().fold(0.0, f64::max),
            Combine::NoisyOr => 1.0 - factors.iter().map(|f| 1.0 - f).product::<f64>(),
            Combine::Product => factors.iter().product(),
        }
    }
}

/// A nowcast driven by several composable [`Trigger`]s.
pub struct MultiNowcast {
    susceptibility: SusceptibilityMap,
    triggers: Vec<Box<dyn Trigger>>,
    combine: Combine,
    n_steps: usize,
}

impl MultiNowcast {
    /// Build from a susceptibility map and one or more triggers, all on the same
    /// grid and step count.
    pub fn new(
        susceptibility: SusceptibilityMap,
        triggers: Vec<Box<dyn Trigger>>,
        combine: Combine,
    ) -> Result<Self> {
        let first = triggers.first().ok_or(Error::InvalidParameter {
            name: "triggers",
            reason: "need at least one trigger".into(),
        })?;
        let dims = susceptibility.dims();
        let n_steps = first.n_steps();
        for t in &triggers {
            if t.dims() != dims {
                return Err(Error::GridMismatch {
                    susc_cols: dims.ncols,
                    susc_rows: dims.nrows,
                    forc_cols: t.dims().ncols,
                    forc_rows: t.dims().nrows,
                });
            }
            if t.n_steps() != n_steps {
                return Err(Error::InvalidParameter {
                    name: "n_steps",
                    reason: format!("triggers disagree: {} vs {n_steps}", t.n_steps()),
                });
            }
        }
        Ok(Self {
            susceptibility,
            triggers,
            combine,
            n_steps,
        })
    }

    pub fn n_steps(&self) -> usize {
        self.n_steps
    }

    /// Per-trigger factors at a cell/step (for traceability), in trigger order.
    pub fn trigger_factors(&self, cell: usize, step: usize) -> Vec<f64> {
        self.triggers.iter().map(|t| t.factor(cell, step)).collect()
    }

    /// Combined hazard field at a step.
    ///
    /// The [`Trigger`] contract says factors lie in `[0, 1]`, but that cannot be
    /// enforced on third-party implementations — so a misbehaving trigger
    /// (factor outside the interval, or `NaN`) is clamped to `[0, 1]` (`NaN` →
    /// `0`) rather than panicking the library.
    pub fn hazard_at(&self, step: usize) -> HazardField {
        let dims = self.susceptibility.dims();
        let mut probability = vec![0.0; dims.len()];
        let mut fs = Vec::with_capacity(self.triggers.len());
        for (cell, p) in probability.iter_mut().enumerate() {
            fs.clear();
            fs.extend(self.triggers.iter().map(|t| t.factor(cell, step)));
            let combined = self.combine.apply(&fs);
            let combined = if combined.is_nan() { 0.0 } else { combined.clamp(0.0, 1.0) };
            *p = self.susceptibility.get(cell) * combined;
        }
        HazardField::new(step, dims, probability)
            .expect("susceptibility and clamped factor are both within [0,1]")
    }

    pub fn run(&self) -> Vec<HazardField> {
        (0..self.n_steps).map(|t| self.hazard_at(t)).collect()
    }

    pub fn alerts(&self, level: f64) -> Vec<Alert> {
        (0..self.n_steps)
            .filter_map(|t| self.hazard_at(t).alert(level))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::forcing::UniformRain;

    #[test]
    fn combine_math() {
        assert!((Combine::Max.apply(&[0.2, 0.7, 0.5]) - 0.7).abs() < 1e-12);
        assert!((Combine::Product.apply(&[0.5, 0.5]) - 0.25).abs() < 1e-12);
        // noisy-OR of 0.5,0.5 = 1 - 0.25 = 0.75
        assert!((Combine::NoisyOr.apply(&[0.5, 0.5]) - 0.75).abs() < 1e-12);
    }

    #[test]
    fn threshold_trigger_exceedance() {
        let dims = GridDims::new(1, 1);
        // "deformation velocity" 20 mm/yr, threshold 10 → exceedance 2 → factor high.
        let f = UniformRain::new(dims, 24.0, vec![20.0]).unwrap();
        let t = ThresholdTrigger::new(f, 10.0, TriggerModel::default()).unwrap();
        assert!(t.factor(0, 0) > 0.9);
    }

    #[test]
    fn noisy_or_fires_if_either_trigger_fires() {
        let dims = GridDims::new(1, 1);
        // Dry rain (no I-D exceedance) but high deformation → hazard via noisy-OR.
        let rain = IdTrigger::new(
            UniformRain::new(dims, 1.0, vec![0.0]).unwrap(),
            IdThreshold::caine(),
            TriggerModel::default(),
            6,
        )
        .unwrap();
        let deform = ThresholdTrigger::new(
            UniformRain::new(dims, 1.0, vec![30.0]).unwrap(), // 30 mm/yr, crit 10
            10.0,
            TriggerModel::default(),
        )
        .unwrap();
        let susc = SusceptibilityMap::uniform(dims, 0.9).unwrap();
        let nc = MultiNowcast::new(
            susc,
            vec![Box::new(rain), Box::new(deform)],
            Combine::NoisyOr,
        )
        .unwrap();
        let field = nc.hazard_at(0);
        assert!(field.max_probability() > 0.8, "deformation alone should fire");
        let factors = nc.trigger_factors(0, 0);
        assert!(factors[0] < 0.1, "rain trigger quiet");
        assert!(factors[1] > 0.9, "deformation trigger hot");
    }

    /// A single IdTrigger under Max must reproduce the batch engine bit-for-bit:
    /// both paths now share the same I-D window kernel
    /// (`IdThreshold::worst_window`), and this pins that equivalence.
    #[test]
    fn id_trigger_matches_nowcast_bit_for_bit() {
        use crate::forcing::GriddedRain;
        use crate::nowcast::Nowcast;

        let dims = GridDims::new(3, 2);
        let n = dims.len();
        let n_steps = 40;
        let mut depths = Vec::with_capacity(n_steps * n);
        let mut x = 7u64;
        for _ in 0..n_steps * n {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
            depths.push(((x >> 33) % 60) as f64); // 0..59 mm
        }
        let forcing = GriddedRain::new(dims, 6.0, depths).unwrap();
        let susc = SusceptibilityMap::new(
            dims,
            (0..n).map(|c| 0.1 + 0.8 * (c as f64 / n as f64)).collect(),
        )
        .unwrap();
        let threshold = IdThreshold::new(5.5, 0.39).unwrap();
        let model = TriggerModel::new(4.0).unwrap();
        let max_window = 7;

        let batch = Nowcast::new(susc.clone(), forcing.clone(), threshold, model, max_window)
            .unwrap()
            .run();
        let trigger = IdTrigger::new(forcing, threshold, model, max_window).unwrap();
        let multi = MultiNowcast::new(susc, vec![Box::new(trigger)], Combine::Max).unwrap();
        let composed = multi.run();

        assert_eq!(batch.len(), composed.len());
        for (b, m) in batch.iter().zip(&composed) {
            assert_eq!(b.step, m.step);
            for (pb, pm) in b.probability().iter().zip(m.probability()) {
                assert_eq!(pb.to_bits(), pm.to_bits(), "step {} diverged", b.step);
            }
        }
    }

    #[test]
    fn misbehaving_trigger_is_clamped_not_panicking() {
        /// A third-party trigger that violates the [0,1] contract.
        struct Rogue;
        impl Trigger for Rogue {
            fn dims(&self) -> GridDims {
                GridDims::new(1, 3)
            }
            fn n_steps(&self) -> usize {
                3
            }
            fn factor(&self, cell: usize, _step: usize) -> f64 {
                [1.5, -0.2, f64::NAN][cell]
            }
        }
        let susc = SusceptibilityMap::uniform(GridDims::new(1, 3), 1.0).unwrap();
        let nc = MultiNowcast::new(susc, vec![Box::new(Rogue)], Combine::Max).unwrap();
        let field = nc.hazard_at(0);
        assert_eq!(field.probability(), &[1.0, 0.0, 0.0]);
    }

    #[test]
    fn id_map_trigger_varies_thresholds_by_cell() {
        use crate::forcing::GriddedRain;
        let dims = GridDims::new(2, 1);
        // Same 20 mm/h burst on both cells.
        let f = GriddedRain::new(dims, 1.0, vec![20.0, 20.0]).unwrap();
        let thresholds = vec![
            IdThreshold::new(5.0, 0.39).unwrap(),  // sensitive cell
            IdThreshold::new(50.0, 0.39).unwrap(), // resistant cell
        ];
        let t = IdMapTrigger::new(f, thresholds, TriggerModel::default(), 6).unwrap();
        assert!(t.factor(0, 0) > 0.9, "20 mm/h is far above a=5");
        assert!(t.factor(1, 0) < 0.1, "20 mm/h is far below a=50");
        // Wrong thresholds length is rejected.
        let f2 = GriddedRain::new(dims, 1.0, vec![1.0, 1.0]).unwrap();
        assert!(
            IdMapTrigger::new(f2, vec![IdThreshold::caine()], TriggerModel::default(), 6).is_err()
        );
    }

    #[test]
    fn id_map_trigger_with_uniform_map_matches_id_trigger_bit_for_bit() {
        use crate::forcing::GriddedRain;
        let dims = GridDims::new(3, 2);
        let n = dims.len();
        let n_steps = 30;
        let mut depths = Vec::with_capacity(n_steps * n);
        let mut x = 13u64;
        for _ in 0..n_steps * n {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
            depths.push(((x >> 33) % 40) as f64);
        }
        let f = GriddedRain::new(dims, 6.0, depths).unwrap();
        let th = IdThreshold::new(5.5, 0.39).unwrap();
        let model = TriggerModel::default();
        let single = IdTrigger::new(f.clone(), th, model, 7).unwrap();
        let mapped = IdMapTrigger::new(f, vec![th; n], model, 7).unwrap();
        for cell in 0..n {
            for step in 0..n_steps {
                assert_eq!(
                    single.factor(cell, step).to_bits(),
                    mapped.factor(cell, step).to_bits(),
                    "cell {cell} step {step} diverged"
                );
            }
        }
    }

    #[test]
    fn antecedent_trigger_tracks_prior_wetness_only() {
        let dims = GridDims::new(1, 1);
        // 3 wet steps, then dry.
        let f = UniformRain::new(dims, 24.0, vec![10.0, 10.0, 10.0, 0.0, 0.0]).unwrap();
        let t = AntecedentTrigger::new(f, 0.5, 10.0, TriggerModel::default()).unwrap();
        // Step 0 has no antecedent — regardless of its own rain.
        assert_eq!(t.api(0, 0), 0.0);
        // api(1) = 10; api(2) = 0.5·10 + 10 = 15; api(3) = 0.5·15 + 10 = 17.5;
        // api(4) = 0.5·17.5 + 0 = 8.75 (decays once the rain stops).
        assert!((t.api(0, 1) - 10.0).abs() < 1e-12);
        assert!((t.api(0, 2) - 15.0).abs() < 1e-12);
        assert!((t.api(0, 3) - 17.5).abs() < 1e-12);
        assert!((t.api(0, 4) - 8.75).abs() < 1e-12);
        // Factor rises with wetness and passes 0.5 exactly at crit (10 mm).
        assert!(t.factor(0, 0) < 0.05);
        assert!((t.factor(0, 1) - 0.5).abs() < 1e-12);
        assert!(t.factor(0, 3) > 0.5);
        // Bad parameters are rejected.
        let f2 = UniformRain::new(dims, 24.0, vec![1.0]).unwrap();
        assert!(AntecedentTrigger::new(f2.clone(), 1.0, 10.0, TriggerModel::default()).is_err());
        assert!(AntecedentTrigger::new(f2, 0.5, 0.0, TriggerModel::default()).is_err());
    }

    #[test]
    fn dual_threshold_product_suppresses_dry_antecedent_bursts() {
        // Two identical 25 mm/h bursts; one lands on a dry history, the other
        // after a wet week. The I-D trigger alone fires on both; the
        // cause × trigger product keeps only the wet-antecedent one — the
        // Bogaard-Greco dual-threshold recipe in one test.
        let dims = GridDims::new(1, 1);
        let rain = vec![0.0, 0.0, 25.0, 0.0, 30.0, 30.0, 30.0, 25.0];
        let mk = || UniformRain::new(dims, 24.0, rain.clone()).unwrap();
        let id = IdTrigger::new(mk(), IdThreshold::new(0.3, 0.39).unwrap(), TriggerModel::default(), 1)
            .unwrap();
        let wet = AntecedentTrigger::new(mk(), 0.8, 40.0, TriggerModel::default()).unwrap();
        let susc = SusceptibilityMap::uniform(dims, 1.0).unwrap();
        let nc = MultiNowcast::new(susc, vec![Box::new(id), Box::new(wet)], Combine::Product).unwrap();

        let dry_burst = nc.hazard_at(2).max_probability();
        let wet_burst = nc.hazard_at(7).max_probability();
        assert!(
            dry_burst < 0.15,
            "dry-antecedent burst must be suppressed, got {dry_burst}"
        );
        assert!(
            wet_burst > 0.5,
            "wet-antecedent burst must fire, got {wet_burst}"
        );
    }

    #[test]
    fn dims_mismatch_is_rejected() {
        let rain = ThresholdTrigger::new(
            UniformRain::new(GridDims::new(2, 2), 1.0, vec![1.0; 4]).unwrap(),
            1.0,
            TriggerModel::default(),
        )
        .unwrap();
        let susc = SusceptibilityMap::uniform(GridDims::new(3, 3), 0.5).unwrap();
        assert!(MultiNowcast::new(susc, vec![Box::new(rain)], Combine::Max).is_err());
    }
}
