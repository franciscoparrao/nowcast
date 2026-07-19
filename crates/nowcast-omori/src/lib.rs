//! # nowcast-omori
//!
//! **Seismicity as a trigger**. Rainfall says a slope is being loaded; a hot
//! aftershock sequence says the ground has just been shaken and is primed:
//! post-seismic slope weakening lowers the rain needed to fail for months
//! after a mainshock (well documented after Chi-Chi, Wenchuan, Gorkha).
//! `omori` turns an earthquake catalog into a conditional ETAS rate; this
//! crate exposes that **forecast rate** (events/day, M ≥ m0) as a
//! [`nowcast_core::Forcing`] so it can drive a [`ThresholdTrigger`] and be
//! fused with the rainfall I–D trigger in a `MultiNowcast`:
//!
//! ```text
//!     hazard = susceptibility × ( rain_I-D  ⊕  aftershock_rate )     (noisy-OR)
//! ```
//!
//! The rate series is **causal**: the rate at step `t` uses only catalog
//! events strictly before the step start (first-generation expected rate,
//! `omori_core::etas::expected_count_window`). It is uniform in space (v1);
//! spatial kernels arrive with the gridded-rate work (`surtgis`).
//!
//! Time axes: the catalog is in **days** (omori's convention); `t0_days`
//! anchors the nowcast's step 0 on that axis.
//!
//! Build a seismicity trigger and combine it with rain:
//!
//! ```
//! use nowcast_core::{Combine, GridDims, IdThreshold, IdTrigger, MultiNowcast,
//!                    SusceptibilityMap, TriggerModel, UniformRain};
//! use nowcast_omori::{seismicity_trigger, Catalog, EtasParams, Event};
//!
//! let dims = GridDims::new(2, 1);
//! // An M7.5 mainshock hours before the nowcast starts.
//! let catalog = Catalog::new(vec![Event::new(-0.1, -70.5, -33.5, 10.0, 7.5)]);
//! let params = EtasParams { mu: 0.05, k: 0.03, c: 0.05, alpha: 1.0, p: 1.2 };
//! let seismic = seismicity_trigger(
//!     &params, &catalog, 4.0, dims, 0.0, 24.0, 3, 1.0, TriggerModel::default(),
//! ).unwrap();
//!
//! let rain = IdTrigger::new(UniformRain::new(dims, 24.0, vec![0.0, 0.0, 0.0]).unwrap(),
//!     IdThreshold::caine(), TriggerModel::default(), 3).unwrap();
//! let susc = SusceptibilityMap::uniform(dims, 0.8).unwrap();
//! let nc = MultiNowcast::new(susc, vec![Box::new(rain), Box::new(seismic)], Combine::NoisyOr).unwrap();
//! // The aftershock sequence alerts even with no rain at all.
//! let field = nc.hazard_at(0).unwrap();
//! assert!(field.probability()[0] > 0.5);
//! ```

use nowcast_core::{Error, Forcing, GridDims, Result, ThresholdTrigger, TriggerModel};
use omori_core::etas::expected_count_window;

// Re-export the omori entry points so callers need only this crate.
pub use omori_core::etas::{fit, fit_seeded, forecast, EtasFit, EtasParams, Forecast};
pub use omori_core::{Catalog, Event};

/// A forecast seismicity **rate** series (events/day, M ≥ `m0`), exposed as a
/// [`Forcing`] that is uniform in space and varies per step — the decaying
/// aftershock forcing that biases the trigger after a mainshock.
#[derive(Debug, Clone)]
pub struct SeismicityForcing {
    dims: GridDims,
    dt_hours: f64,
    /// Expected rate per step (events/day), same for every cell.
    rates_per_day: Vec<f64>,
}

impl SeismicityForcing {
    /// Causal expected-rate series from an observed catalog and fitted ETAS
    /// parameters: at each step `i`, the first-generation ETAS rate over
    /// `[t0 + i·dt, t0 + (i+1)·dt)` given **only the events strictly before
    /// the step start**. `t0_days` is the nowcast origin on the catalog's
    /// time axis (days).
    pub fn from_catalog(
        params: &EtasParams,
        catalog: &Catalog,
        m0: f64,
        dims: GridDims,
        t0_days: f64,
        dt_hours: f64,
        n_steps: usize,
    ) -> Result<Self> {
        if !dt_hours.is_finite() || dt_hours <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "dt_hours",
                reason: format!("must be > 0, got {dt_hours}"),
            });
        }
        if n_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "n_steps",
                reason: "must be >= 1".into(),
            });
        }
        if !t0_days.is_finite() {
            return Err(Error::InvalidParameter {
                name: "t0_days",
                reason: format!("must be finite, got {t0_days}"),
            });
        }
        // Events sorted by time; a cursor keeps each step's history causal
        // without re-filtering the whole catalog per step.
        let mut events = catalog.events.clone();
        events.sort_by(|a, b| a.time.partial_cmp(&b.time).unwrap_or(std::cmp::Ordering::Equal));
        let dt_days = dt_hours / 24.0;
        let mut rates_per_day = Vec::with_capacity(n_steps);
        let mut cursor = 0usize;
        for i in 0..n_steps {
            let t1 = t0_days + i as f64 * dt_days;
            let t2 = t1 + dt_days;
            while cursor < events.len() && events[cursor].time < t1 {
                cursor += 1;
            }
            let history = Catalog::new(events[..cursor].to_vec());
            let expected = expected_count_window(params, &history, t1, t2, m0);
            if !expected.is_finite() || expected < 0.0 {
                return Err(Error::InvalidParameter {
                    name: "params",
                    reason: format!(
                        "ETAS expected count is not finite/non-negative at step {i} (got {expected}); \
                         check the parameters are admissible (mu,k >= 0, c > 0)"
                    ),
                });
            }
            rates_per_day.push(expected / dt_days);
        }
        Ok(Self {
            dims,
            dt_hours,
            rates_per_day,
        })
    }

    /// The expected rate at `step`, events/day.
    pub fn rate_per_day(&self, step: usize) -> f64 {
        self.rates_per_day[step]
    }
}

impl Forcing for SeismicityForcing {
    fn dims(&self) -> GridDims {
        self.dims
    }
    fn n_steps(&self) -> usize {
        self.rates_per_day.len()
    }
    fn dt_hours(&self) -> f64 {
        self.dt_hours
    }
    /// The forcing "depth" is the seismicity rate (events/day) — a rate, like
    /// the InSAR deformation forcing, meant for a [`ThresholdTrigger`].
    fn depth_mm(&self, _cell: usize, step: usize) -> f64 {
        self.rates_per_day[step]
    }
}

/// Build a seismicity [`ThresholdTrigger`] from a catalog and fitted ETAS
/// parameters: `factor = model(rate / rate_crit)`, with `rate_crit` in
/// events/day (M ≥ `m0`). See [`SeismicityForcing::from_catalog`] for the
/// causal rate series semantics.
#[allow(clippy::too_many_arguments)]
pub fn seismicity_trigger(
    params: &EtasParams,
    catalog: &Catalog,
    m0: f64,
    dims: GridDims,
    t0_days: f64,
    dt_hours: f64,
    n_steps: usize,
    rate_crit_per_day: f64,
    model: TriggerModel,
) -> Result<ThresholdTrigger<SeismicityForcing>> {
    let forcing =
        SeismicityForcing::from_catalog(params, catalog, m0, dims, t0_days, dt_hours, n_steps)?;
    ThresholdTrigger::new(forcing, rate_crit_per_day, model)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nowcast_core::{
        Combine, IdThreshold, IdTrigger, MultiNowcast, SusceptibilityMap, UniformRain,
    };

    fn params() -> EtasParams {
        EtasParams { mu: 0.05, k: 0.03, c: 0.05, alpha: 1.0, p: 1.2 }
    }

    #[test]
    fn rate_decays_after_mainshock() {
        let cat = Catalog::new(vec![Event::new(-1.0, 0.0, 0.0, 10.0, 7.0)]);
        let f = SeismicityForcing::from_catalog(
            &params(), &cat, 4.0, GridDims::new(1, 1), 0.0, 24.0, 5,
        )
        .unwrap();
        assert!(f.rate_per_day(0) > params().mu, "sequence beats background");
        for i in 1..5 {
            assert!(f.rate_per_day(i) < f.rate_per_day(i - 1), "Omori decay");
        }
        // Broadcast over cells.
        assert_eq!(f.depth_mm(0, 2), f.rate_per_day(2));
    }

    /// The series is causal: an event inside step 0's window must not change
    /// step 0's rate, but must raise step 1's.
    #[test]
    fn rate_series_is_causal() {
        let dims = GridDims::new(1, 1);
        let before = Catalog::new(vec![Event::new(-1.0, 0.0, 0.0, 10.0, 6.0)]);
        let with_inside = Catalog::new(vec![
            Event::new(-1.0, 0.0, 0.0, 10.0, 6.0),
            Event::new(0.5, 0.0, 0.0, 10.0, 5.5), // inside step 0's window
        ]);
        let a = SeismicityForcing::from_catalog(&params(), &before, 4.0, dims, 0.0, 24.0, 2)
            .unwrap();
        let b = SeismicityForcing::from_catalog(&params(), &with_inside, 4.0, dims, 0.0, 24.0, 2)
            .unwrap();
        assert_eq!(a.rate_per_day(0), b.rate_per_day(0), "unknown at forecast time");
        assert!(b.rate_per_day(1) > a.rate_per_day(1), "known by step 1");
    }

    #[test]
    fn hostile_inputs_are_rejected() {
        let cat = Catalog::new(vec![Event::new(-1.0, 0.0, 0.0, 10.0, 7.0)]);
        let dims = GridDims::new(1, 1);
        let p = params();
        assert!(SeismicityForcing::from_catalog(&p, &cat, 4.0, dims, 0.0, 0.0, 3).is_err());
        assert!(SeismicityForcing::from_catalog(&p, &cat, 4.0, dims, 0.0, -24.0, 3).is_err());
        assert!(SeismicityForcing::from_catalog(&p, &cat, 4.0, dims, 0.0, f64::NAN, 3).is_err());
        assert!(SeismicityForcing::from_catalog(&p, &cat, 4.0, dims, f64::NAN, 24.0, 3).is_err());
        assert!(SeismicityForcing::from_catalog(&p, &cat, 4.0, dims, 0.0, 24.0, 0).is_err());
        let bad = EtasParams { c: -1.0, ..p };
        assert!(SeismicityForcing::from_catalog(&bad, &cat, 4.0, dims, 0.0, 24.0, 3).is_err());
        assert!(
            seismicity_trigger(&p, &cat, 4.0, dims, 0.0, 24.0, 3, 0.0, TriggerModel::default())
                .is_err(),
            "rate_crit must be > 0"
        );
    }

    /// Mirror of the InSAR integration test: a hot aftershock sequence alerts
    /// with bone-dry rain under noisy-OR fusion.
    #[test]
    fn aftershocks_alert_without_rain() {
        let dims = GridDims::new(2, 1);
        let cat = Catalog::new(vec![Event::new(-0.1, 0.0, 0.0, 10.0, 7.5)]);
        let seismic = seismicity_trigger(
            &params(), &cat, 4.0, dims, 0.0, 24.0, 3, 1.0, TriggerModel::default(),
        )
        .unwrap();
        let rain = IdTrigger::new(
            UniformRain::new(dims, 24.0, vec![0.0, 0.0, 0.0]).unwrap(),
            IdThreshold::caine(),
            TriggerModel::default(),
            3,
        )
        .unwrap();
        // Susceptibility separates the cells; the forcing is uniform (v1).
        let susc = SusceptibilityMap::new(dims, vec![0.05, 0.9]).unwrap();
        let nc = MultiNowcast::new(
            susc,
            vec![Box::new(rain), Box::new(seismic)],
            Combine::NoisyOr,
        )
        .unwrap();
        let field = nc.hazard_at(0).unwrap();
        assert!(field.probability()[1] > 0.6, "primed slope alerts: {}", field.probability()[1]);
        assert!(field.probability()[0] < 0.1, "low-susceptibility cell stays quiet");
    }
}
