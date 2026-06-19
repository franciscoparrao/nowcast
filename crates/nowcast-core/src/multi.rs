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
    /// Per-cell prefix sums of depth (`prefix[c][t]` = total over steps `0..t`).
    prefix: Vec<Vec<f64>>,
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
        let n_cells = forcing.dims().len();
        let mut prefix = vec![vec![0.0; n_steps + 1]; n_cells];
        for (c, row) in prefix.iter_mut().enumerate() {
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
        let dt = self.forcing.dt_hours();
        let max_m = self.max_window_steps.min(step + 1);
        let row = &self.prefix[cell];
        let mut best = 0.0_f64;
        for m in 1..=max_m {
            let depth = row[step + 1] - row[step + 1 - m];
            let duration_h = m as f64 * dt;
            let e = self.threshold.exceedance(depth / duration_h, duration_h);
            if e > best {
                best = e;
            }
        }
        self.model.factor(best)
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
    pub fn hazard_at(&self, step: usize) -> HazardField {
        let dims = self.susceptibility.dims();
        let mut probability = vec![0.0; dims.len()];
        let mut fs = Vec::with_capacity(self.triggers.len());
        for (cell, p) in probability.iter_mut().enumerate() {
            fs.clear();
            fs.extend(self.triggers.iter().map(|t| t.factor(cell, step)));
            *p = self.susceptibility.get(cell) * self.combine.apply(&fs);
        }
        HazardField::new(step, dims, probability).expect("hazard within [0,1]")
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
