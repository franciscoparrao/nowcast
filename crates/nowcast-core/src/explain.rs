//! Exact attribution of a nowcast hazard value.
//!
//! Because the hazard is closed-form — `susceptibility × trigger_factor` — every
//! alert is **exactly** decomposable into its drivers, with no sampling or
//! surrogate model. This is stronger than post-hoc SHAP on a black box: there is
//! nothing to approximate. (SHAP belongs one layer up, explaining the upstream
//! ML *susceptibility* that enters here as a single, already-interpretable input.)
//!
//! An [`Explanation`] reports, for one cell and step: the hazard and its two
//! factors, the rolling intensity–duration window that drove the trigger (which
//! duration, the mean rainfall intensity over it, the critical intensity, and the
//! exceedance), and which side — terrain or weather — is the binding constraint.

/// Which factor limits the hazard (the smaller multiplicand is the bottleneck).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Driver {
    /// The dynamic trigger is the bottleneck — susceptible ground, not enough rain.
    TriggerLimited,
    /// Susceptibility is the bottleneck — heavy rain, but stable ground.
    TerrainLimited,
    /// Both contribute comparably.
    Balanced,
}

impl Driver {
    fn classify(susceptibility: f64, trigger_factor: f64, tol: f64) -> Self {
        if trigger_factor + tol < susceptibility {
            Driver::TriggerLimited
        } else if susceptibility + tol < trigger_factor {
            Driver::TerrainLimited
        } else {
            Driver::Balanced
        }
    }
}

/// Exact decomposition of `hazard(cell, step)` into its drivers.
#[derive(Debug, Clone, Copy)]
pub struct Explanation {
    pub cell: usize,
    pub step: usize,
    pub hazard: f64,
    /// Static susceptibility at the cell (the "terrain" factor).
    pub susceptibility: f64,
    /// Dynamic trigger factor in `[0, 1]` (the "weather" factor).
    pub trigger_factor: f64,
    /// Duration (h) of the rolling window that maximised the I–D exceedance.
    pub critical_duration_h: f64,
    /// Mean rainfall intensity (mm/h) over that window.
    pub mean_intensity_mm_h: f64,
    /// Critical intensity (mm/h) of the I–D curve at that duration.
    pub critical_intensity_mm_h: f64,
    /// Exceedance ratio `E = I / I_crit` of the driving window.
    pub exceedance: f64,
    /// Which side is the binding constraint.
    pub driver: Driver,
}

impl Explanation {
    // A plain data-bundle constructor; each argument is a distinct field.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        cell: usize,
        step: usize,
        susceptibility: f64,
        trigger_factor: f64,
        critical_duration_h: f64,
        mean_intensity_mm_h: f64,
        critical_intensity_mm_h: f64,
        exceedance: f64,
    ) -> Self {
        Self {
            cell,
            step,
            hazard: susceptibility * trigger_factor,
            susceptibility,
            trigger_factor,
            critical_duration_h,
            mean_intensity_mm_h,
            critical_intensity_mm_h,
            exceedance,
            driver: Driver::classify(susceptibility, trigger_factor, 0.15),
        }
    }

    /// A one-line human-readable account of why the hazard is what it is.
    pub fn summary(&self) -> String {
        let driver = match self.driver {
            Driver::TriggerLimited => "limitado por el gatillo (lluvia insuficiente)",
            Driver::TerrainLimited => "limitado por el terreno (poco susceptible)",
            Driver::Balanced => "terreno y gatillo comparables",
        };
        format!(
            "celda {} paso {}: peligro {:.2} = susceptibilidad {:.2} × gatillo {:.2}; \
             ventana I–D dominante {:.0} h con {:.1} mm/h (crítica {:.1} mm/h, E={:.2}); {driver}",
            self.cell,
            self.step,
            self.hazard,
            self.susceptibility,
            self.trigger_factor,
            self.critical_duration_h,
            self.mean_intensity_mm_h,
            self.critical_intensity_mm_h,
            self.exceedance,
        )
    }
}
