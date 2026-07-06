//! # nowcast-rainflow
//!
//! v0.2 **flood** provider. The `rainflow` rainfall–runoff engine routes
//! precipitation through a catchment into a discharge hydrograph; this crate
//! turns that hydrograph into a time-varying flood hazard.
//!
//! ## A different trigger from the landslide path
//!
//! Landslide nowcasting (the `nowcast-core` engine) uses a rainfall
//! intensity–duration threshold: it accumulates water input over rolling
//! windows because what matters is how much rain fell over how long. A flood is
//! different — the catchment's routing has *already* integrated the rainfall,
//! so the trigger is simply whether the **discharge exceeds a flood threshold**
//! `Q_c` (bankfull / a high-flow quantile / a return-period level). There is no
//! duration power law and each step is independent.
//!
//! Both paths share the same structure, though:
//!
//! ```text
//!     hazard(cell, t) = flood_susceptibility(cell) × trigger_factor(Q(t) / Q_c)
//! ```
//!
//! so this crate reuses [`SusceptibilityMap`], [`TriggerModel`], [`HazardField`]
//! and [`Alert`] from the core, swapping only how exceedance is computed.
//! [`RainflowForcing`] also implements the core [`Forcing`] trait (discharge
//! broadcast over the grid) so a hydrograph can compose with anything that
//! expects a forcing.

use nowcast_core::{
    Alert, Driver, Forcing, GridDims, HazardField, SusceptibilityMap, TriggerModel,
};
use rainflow_core::{Gr4j, Gr4jParams};
use thiserror::Error;

/// Errors from building a flood provider.
#[derive(Debug, Error)]
pub enum Error {
    #[error(transparent)]
    Core(#[from] nowcast_core::Error),
    /// The wrapped rainflow engine failed (params or forcing).
    #[error("rainflow engine: {0}")]
    Rainflow(String),
    /// A discharge or threshold value was not finite / not positive.
    #[error("invalid value for `{name}`: {reason}")]
    Invalid { name: &'static str, reason: String },
}

pub type Result<T> = std::result::Result<T, Error>;

/// A catchment discharge hydrograph exposed as a nowcast [`Forcing`].
///
/// Discharge is lumped (one value per step for the whole catchment), so
/// `depth_mm` broadcasts the same value to every cell — the spatial pattern of a
/// flood lives in the susceptibility map, not the forcing.
#[derive(Debug, Clone)]
pub struct RainflowForcing {
    dims: GridDims,
    dt_hours: f64,
    /// Routed discharge per step, in mm/day (catchment-area-normalised depth).
    discharge_mm_day: Vec<f64>,
}

impl RainflowForcing {
    /// Wrap an existing discharge series (mm/day) over a grid.
    pub fn new(dims: GridDims, dt_days: f64, discharge_mm_day: Vec<f64>) -> Result<Self> {
        if !dt_days.is_finite() || dt_days <= 0.0 {
            return Err(Error::Invalid {
                name: "dt_days",
                reason: format!("must be finite and > 0, got {dt_days}"),
            });
        }
        if discharge_mm_day.iter().any(|q| !q.is_finite() || *q < 0.0) {
            return Err(Error::Invalid {
                name: "discharge",
                reason: "values must be finite and non-negative".into(),
            });
        }
        Ok(Self {
            dims,
            dt_hours: dt_days * 24.0,
            discharge_mm_day,
        })
    }

    /// Run rainflow's GR4J model on a precip/PET series to produce the discharge
    /// hydrograph, then expose it as a forcing.
    pub fn gr4j(
        dims: GridDims,
        dt_days: f64,
        params: Gr4jParams<f64>,
        precip: &[f64],
        pet: &[f64],
    ) -> Result<Self> {
        let model = Gr4j::new(params).map_err(|e| Error::Rainflow(e.to_string()))?;
        let q = model
            .run(precip, pet)
            .map_err(|e| Error::Rainflow(e.to_string()))?;
        Self::new(dims, dt_days, q)
    }

    /// The routed discharge series (mm/day).
    pub fn discharge(&self) -> &[f64] {
        &self.discharge_mm_day
    }
}

impl Forcing for RainflowForcing {
    fn dims(&self) -> GridDims {
        self.dims
    }
    fn n_steps(&self) -> usize {
        self.discharge_mm_day.len()
    }
    fn dt_hours(&self) -> f64 {
        self.dt_hours
    }
    fn depth_mm(&self, _cell: usize, step: usize) -> f64 {
        // Depth of water over the step = rate (mm/day) × step length (days).
        self.discharge_mm_day[step] * (self.dt_hours / 24.0)
    }
}

/// A flood threshold `Q_c` (mm/day): discharge at or above it is a flood.
#[derive(Debug, Clone, Copy)]
pub struct FloodThreshold {
    pub q_crit: f64,
}

impl FloodThreshold {
    pub fn new(q_crit: f64) -> Result<Self> {
        if !q_crit.is_finite() || q_crit <= 0.0 {
            return Err(Error::Invalid {
                name: "q_crit",
                reason: format!("must be finite and > 0, got {q_crit}"),
            });
        }
        Ok(Self { q_crit })
    }

    /// Threshold as the `p`-quantile of a discharge series (`p` in `(0, 1)`),
    /// e.g. `p = 0.95` for a high-flow level. Non-finite values are ignored.
    pub fn quantile(series: &[f64], p: f64) -> Result<Self> {
        if !(0.0..1.0).contains(&p) {
            return Err(Error::Invalid {
                name: "p",
                reason: format!("quantile must be in (0, 1), got {p}"),
            });
        }
        let mut v: Vec<f64> = series.iter().copied().filter(|x| x.is_finite()).collect();
        if v.is_empty() {
            return Err(Error::Invalid {
                name: "series",
                reason: "no finite discharge values".into(),
            });
        }
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let idx = ((v.len() - 1) as f64 * p).round() as usize;
        Self::new(v[idx])
    }

    /// Exceedance ratio `E = Q / Q_c` (`E >= 1` is a flood).
    pub fn exceedance(&self, q: f64) -> f64 {
        q / self.q_crit
    }
}

/// Flood nowcast: `flood_susceptibility × trigger(Q / Q_c)` per step.
#[derive(Debug, Clone)]
pub struct FloodNowcast {
    susceptibility: SusceptibilityMap,
    discharge_mm_day: Vec<f64>,
    threshold: FloodThreshold,
    trigger: TriggerModel,
}

impl FloodNowcast {
    /// Build from a discharge series (mm/day) and a flood-exposure map.
    pub fn new(
        susceptibility: SusceptibilityMap,
        discharge_mm_day: Vec<f64>,
        threshold: FloodThreshold,
        trigger: TriggerModel,
    ) -> Result<Self> {
        if discharge_mm_day.iter().any(|q| !q.is_finite() || *q < 0.0) {
            return Err(Error::Invalid {
                name: "discharge",
                reason: "values must be finite and non-negative".into(),
            });
        }
        Ok(Self {
            susceptibility,
            discharge_mm_day,
            threshold,
            trigger,
        })
    }

    /// Convenience: take the hydrograph straight from a [`RainflowForcing`].
    pub fn from_rainflow(
        susceptibility: SusceptibilityMap,
        forcing: &RainflowForcing,
        threshold: FloodThreshold,
        trigger: TriggerModel,
    ) -> Result<Self> {
        Self::new(
            susceptibility,
            forcing.discharge().to_vec(),
            threshold,
            trigger,
        )
    }

    pub fn n_steps(&self) -> usize {
        self.discharge_mm_day.len()
    }

    fn n_cells(&self) -> usize {
        self.susceptibility.dims().len()
    }

    fn check_step(&self, step: usize) -> Result<()> {
        let n_steps = self.n_steps();
        if step >= n_steps {
            return Err(nowcast_core::Error::OutOfRange { name: "step", index: step, len: n_steps }.into());
        }
        Ok(())
    }

    fn check_cell(&self, cell: usize) -> Result<()> {
        let n_cells = self.n_cells();
        if cell >= n_cells {
            return Err(nowcast_core::Error::OutOfRange { name: "cell", index: cell, len: n_cells }.into());
        }
        Ok(())
    }

    /// Flood-hazard field for one step. Errors if `step` is out of range.
    pub fn hazard_at(&self, step: usize) -> Result<HazardField> {
        self.check_step(step)?;
        let factor = self.trigger.factor(self.threshold.exceedance(self.discharge_mm_day[step]));
        let dims = self.susceptibility.dims();
        let probability = self
            .susceptibility
            .values()
            .iter()
            .map(|s| s * factor)
            .collect();
        // Safe: susceptibility ∈ [0,1] and factor ∈ [0,1] ⇒ product ∈ [0,1].
        Ok(HazardField::new(step, dims, probability).expect("hazard within [0,1]"))
    }

    /// Flood-hazard field for every step.
    pub fn run(&self) -> Vec<HazardField> {
        (0..self.n_steps())
            .map(|t| self.hazard_at(t).expect("t is in 0..n_steps"))
            .collect()
    }

    /// An [`Alert`] for every step whose peak flood hazard reaches `level`.
    pub fn alerts(&self, level: f64) -> Vec<Alert> {
        (0..self.n_steps())
            .filter_map(|t| self.hazard_at(t).expect("t is in 0..n_steps").alert(level))
            .collect()
    }

    /// Exact attribution of the flood hazard at `cell` / `step` — the closed-form
    /// counterpart of the landslide `Nowcast::explain`. Floods carry no I–D
    /// window: the trigger is discharge over threshold, `Q / Q_c`. Errors if
    /// `cell` or `step` is out of range.
    pub fn explain(&self, cell: usize, step: usize) -> Result<FloodExplanation> {
        self.check_cell(cell)?;
        self.check_step(step)?;
        let q = self.discharge_mm_day[step];
        let exceedance = self.threshold.exceedance(q);
        let trigger_factor = self.trigger.factor(exceedance);
        let susceptibility = self.susceptibility.get(cell);
        Ok(FloodExplanation {
            cell,
            step,
            hazard: susceptibility * trigger_factor,
            susceptibility,
            trigger_factor,
            discharge_mm_day: q,
            q_crit: self.threshold.q_crit,
            exceedance,
            driver: Driver::classify(susceptibility, trigger_factor, 0.15),
        })
    }

    /// Counterfactual: the discharge (mm/day) that would lift `cell`'s flood
    /// hazard to `alert_level`. `Ok(None)` if the cell's exposure alone cannot
    /// reach it (exposure-capped). Errors if `cell` is out of range.
    pub fn discharge_to_alert(&self, cell: usize, alert_level: f64) -> Result<Option<f64>> {
        self.check_cell(cell)?;
        let factor_needed = alert_level / self.susceptibility.get(cell);
        if !(0.0..1.0).contains(&factor_needed) {
            return Ok(None);
        }
        Ok(Some(self.trigger.exceedance_for_factor(factor_needed) * self.threshold.q_crit))
    }
}

/// Exact decomposition of a flood `hazard(cell, step)` into its drivers.
#[derive(Debug, Clone, Copy)]
pub struct FloodExplanation {
    pub cell: usize,
    pub step: usize,
    pub hazard: f64,
    /// Flood exposure at the cell (the "terrain" factor).
    pub susceptibility: f64,
    /// Discharge trigger factor in `[0, 1]` (the "hydrology" factor).
    pub trigger_factor: f64,
    pub discharge_mm_day: f64,
    pub q_crit: f64,
    /// Exceedance ratio `Q / Q_c`.
    pub exceedance: f64,
    pub driver: Driver,
}

impl FloodExplanation {
    /// One-line account of why the flood hazard is what it is.
    pub fn summary(&self) -> String {
        let driver = match self.driver {
            Driver::TriggerLimited => "limitado por el caudal (crecida insuficiente)",
            Driver::TerrainLimited => "limitado por la exposición (zona poco inundable)",
            Driver::Balanced => "exposición y caudal comparables",
        };
        format!(
            "celda {} paso {}: peligro {:.2} = exposición {:.2} × caudal {:.2}; \
             Q={:.1} mm/día vs Q_c={:.1} (E={:.2}); {driver}",
            self.cell, self.step, self.hazard, self.susceptibility,
            self.trigger_factor, self.discharge_mm_day, self.q_crit, self.exceedance,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn susc_2cell(a: f64, b: f64) -> SusceptibilityMap {
        SusceptibilityMap::new(GridDims::new(2, 1), vec![a, b]).unwrap()
    }

    #[test]
    fn gr4j_produces_a_hydrograph() {
        let dims = GridDims::new(1, 1);
        let params = Gr4jParams {
            x1: 350.0,
            x2: 0.0,
            x3: 90.0,
            x4: 1.7,
        };
        let precip = vec![0.0, 5.0, 40.0, 30.0, 2.0, 0.0, 0.0];
        let pet = vec![3.0; 7];
        let f = RainflowForcing::gr4j(dims, 1.0, params, &precip, &pet).unwrap();
        assert_eq!(f.n_steps(), 7);
        assert_eq!(f.dt_hours(), 24.0);
        // Some water comes out, and never negative.
        assert!(f.discharge().iter().all(|q| *q >= 0.0));
        assert!(f.discharge().iter().sum::<f64>() > 0.0);
    }

    #[test]
    fn quantile_threshold() {
        let series: Vec<f64> = (0..=100).map(|i| i as f64).collect();
        let t = FloodThreshold::quantile(&series, 0.95).unwrap();
        assert!((t.q_crit - 95.0).abs() < 1e-9);
        assert!((t.exceedance(190.0) - 2.0).abs() < 1e-9);
    }

    #[test]
    fn flood_day_alerts_low_day_quiet() {
        let susc = susc_2cell(0.3, 0.9);
        let discharge = vec![1.0, 2.0, 50.0, 3.0]; // a flood spike at step 2
        let threshold = FloodThreshold::new(20.0).unwrap();
        let nc = FloodNowcast::new(susc, discharge, threshold, TriggerModel::default()).unwrap();

        assert!(nc.hazard_at(0).unwrap().max_probability() < 0.1, "base flow is quiet");
        let peak = nc.hazard_at(2).unwrap();
        assert!(
            peak.max_probability() > 0.8,
            "flood spike should approach susceptibility, got {}",
            peak.max_probability()
        );

        let alerts = nc.alerts(0.5);
        assert_eq!(alerts.len(), 1);
        assert_eq!(alerts[0].step, 2);

        // Out-of-range step is an error, not a panic.
        assert!(nc.hazard_at(4).is_err());
    }

    #[test]
    fn explain_decomposes_a_flood_step() {
        let susc = susc_2cell(0.3, 0.9);
        let nc = FloodNowcast::new(
            susc,
            vec![10.0, 60.0],
            FloodThreshold::new(20.0).unwrap(),
            TriggerModel::default(),
        )
        .unwrap();
        // Flood spike at step 1: discharge clears the threshold on both cells.
        let ex = nc.explain(1, 1).unwrap();
        assert!((ex.exceedance - 3.0).abs() < 1e-9); // 60 / 20
        assert!(ex.trigger_factor > 0.9);
        assert!((ex.hazard - ex.susceptibility * ex.trigger_factor).abs() < 1e-12);
        assert_eq!(ex.driver, Driver::Balanced); // exposure 0.9 ≈ saturated trigger
        // Same flood, low-exposure cell → terrain (exposure) limited.
        assert_eq!(nc.explain(0, 1).unwrap().driver, Driver::TerrainLimited);
        // Low-flow step → trigger-limited.
        assert_eq!(nc.explain(1, 0).unwrap().driver, Driver::TriggerLimited);
        // Out-of-range cell/step is an error, not a panic.
        assert!(nc.explain(2, 0).is_err());
        assert!(nc.explain(0, 2).is_err());
    }

    #[test]
    fn discharge_to_alert_counterfactual() {
        let nc = FloodNowcast::new(
            susc_2cell(0.3, 0.9),
            vec![5.0],
            FloodThreshold::new(20.0).unwrap(),
            TriggerModel::default(),
        )
        .unwrap();
        // Exposed cell (0.9) can reach the 0.5 alert with enough discharge.
        assert!(nc.discharge_to_alert(1, 0.5).unwrap().unwrap() > 0.0);
        // Barely-exposed cell (0.3) is exposure-capped below 0.5.
        assert!(nc.discharge_to_alert(0, 0.5).unwrap().is_none());
        // Out-of-range cell is an error, not a panic.
        assert!(nc.discharge_to_alert(2, 0.5).is_err());
    }

    #[test]
    fn hazard_scales_with_susceptibility() {
        let susc = susc_2cell(0.2, 0.8);
        let nc = FloodNowcast::new(
            susc,
            vec![100.0],
            FloodThreshold::new(20.0).unwrap(),
            TriggerModel::default(),
        )
        .unwrap();
        let p = nc.hazard_at(0).unwrap();
        let v = p.probability();
        assert!((v[1] / v[0] - 0.8 / 0.2).abs() < 1e-9);
    }
}
