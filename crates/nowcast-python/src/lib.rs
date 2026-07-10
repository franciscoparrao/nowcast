//! Python bindings for nowcast-core (PyO3 + numpy).
//!
//! Exposes the forward engine ([`Nowcast`]), the streaming engine
//! ([`LiveNowcast`]), probability calibration ([`Calibrator`], [`reliability`],
//! [`brier_score`]) and the verification toolbox (`monthly_contingency`,
//! `spatial_daily_contingency`, `roc_auc`, `pr_auc`, `lead_times`) so both the
//! engine and its validation can be driven from the Python susceptibility
//! pipeline.
//!
//! Grids cross the boundary as **numpy arrays** in row-major cell order
//! (`cell = row * ncols + col`): susceptibility and per-step rain are 1-D
//! arrays, gridded rain is a 2-D `(steps, cells)` array, and `run` returns a
//! 2-D `(steps, cells)` float64 hazard array. Inputs are accepted in any real
//! dtype (float32, int, …) and any memory layout (strided views, transposes)
//! and converted to float64 on the way in — the susceptibility pipeline's
//! rasters are typically float32. The heavy computations release the GIL, so
//! a long `run()` does not freeze the host process.

use numpy::ndarray::Array2;
use numpy::{AllowTypeChange, IntoPyArray, PyArray1, PyArray2, PyArrayLike1, PyArrayLike2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::BTreeSet;

use nowcast_core::{
    Calibrator as CoreCalibrator, Contingency, DayKey, GridDims, GriddedRain,
    LiveNowcast as CoreLive, Nowcast as CoreNowcast, SusceptibilityMap, TriggerModel, UniformRain,
    brier_score as core_brier, reliability as core_reliability,
};
use nowcast_core::IdThreshold;

fn err(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// Alert levels are compared against hazard probabilities in `[0, 1]`; a NaN
/// makes every `p >= level` false, silently disabling alerting — reject it.
fn validate_alert_level(level: f64) -> PyResult<()> {
    if !level.is_finite() || !(0.0..=1.0).contains(&level) {
        return Err(err(format!(
            "alert level must be a probability in [0, 1], got {level}"
        )));
    }
    Ok(())
}

/// `(step, n_cells, fraction, max_probability)` — an alert crossing.
type AlertTuple = (usize, usize, f64, f64);
/// What `LiveNowcast.push` hands back: the hazard array and the optional alert.
type PushResult<'py> = PyResult<(Bound<'py, PyArray1<f64>>, Option<AlertTuple>)>;

fn build_susc(values: Vec<f64>, ncols: usize, nrows: usize) -> PyResult<(SusceptibilityMap, GridDims)> {
    let dims = GridDims::new(ncols, nrows);
    Ok((SusceptibilityMap::new(dims, values).map_err(err)?, dims))
}

/// Contingency table + derived scores as a Python dict.
fn contingency_dict<'py>(py: Python<'py>, c: &Contingency) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("hits", c.hits)?;
    d.set_item("misses", c.misses)?;
    d.set_item("false_alarms", c.false_alarms)?;
    d.set_item("correct_negatives", c.correct_negatives)?;
    d.set_item("pod", c.pod())?;
    d.set_item("far", c.far())?;
    d.set_item("csi", c.csi())?;
    d.set_item("frequency_bias", c.frequency_bias())?;
    Ok(d)
}

/// The batch nowcast engine: hazard = susceptibility × trigger over a forcing.
#[pyclass(name = "Nowcast")]
struct PyNowcast {
    inner: Engine,
}

enum Engine {
    Uniform(CoreNowcast<UniformRain>),
    Gridded(CoreNowcast<GriddedRain>),
}

#[pymethods]
impl PyNowcast {
    /// Build with a single-gauge rainfall series (1-D array, one value per
    /// step) broadcast over the whole grid.
    #[staticmethod]
    #[pyo3(signature = (susceptibility, ncols, nrows, rain, dt_hours, id_a=14.82, id_b=0.39, k=4.0, max_window=7))]
    #[allow(clippy::too_many_arguments)]
    fn uniform(
        susceptibility: PyArrayLike1<'_, f64, AllowTypeChange>,
        ncols: usize,
        nrows: usize,
        rain: PyArrayLike1<'_, f64, AllowTypeChange>,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, dims) = build_susc(susceptibility.as_array().to_vec(), ncols, nrows)?;
        let forcing = UniformRain::new(dims, dt_hours, rain.as_array().to_vec()).map_err(err)?;
        let nc = CoreNowcast::new(
            susc,
            forcing,
            IdThreshold::new(id_a, id_b).map_err(err)?,
            TriggerModel::new(k).map_err(err)?,
            max_window,
        )
        .map_err(err)?;
        Ok(Self { inner: Engine::Uniform(nc) })
    }

    /// Build with distributed rainfall: a 2-D array of shape `(steps, cells)`
    /// in row-major cell order (mm per step).
    #[staticmethod]
    #[pyo3(signature = (susceptibility, ncols, nrows, rain, dt_hours, id_a=14.82, id_b=0.39, k=4.0, max_window=7))]
    #[allow(clippy::too_many_arguments)]
    fn gridded(
        susceptibility: PyArrayLike1<'_, f64, AllowTypeChange>,
        ncols: usize,
        nrows: usize,
        rain: PyArrayLike2<'_, f64, AllowTypeChange>,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, dims) = build_susc(susceptibility.as_array().to_vec(), ncols, nrows)?;
        let n = dims.len();
        let rain = rain.as_array();
        let shape = rain.shape();
        if shape[1] != n {
            return Err(err(format!(
                "rain has {} cells per step, grid has {n}",
                shape[1]
            )));
        }
        // (steps, cells) in logical row-major order flattens to exactly
        // GriddedRain's step-major layout, whatever the input's memory layout.
        let flat: Vec<f64> = rain.iter().copied().collect();
        let forcing = GriddedRain::new(dims, dt_hours, flat).map_err(err)?;
        let nc = CoreNowcast::new(
            susc,
            forcing,
            IdThreshold::new(id_a, id_b).map_err(err)?,
            TriggerModel::new(k).map_err(err)?,
            max_window,
        )
        .map_err(err)?;
        Ok(Self { inner: Engine::Gridded(nc) })
    }

    /// Run the nowcast; returns hazard as a `(steps, cells)` float64 array.
    /// Releases the GIL while the engine computes.
    fn run<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyArray2<f64>>> {
        let fields = py.allow_threads(|| match &self.inner {
            Engine::Uniform(n) => n.run(),
            Engine::Gridded(n) => n.run(),
        });
        let n_steps = fields.len();
        let n_cells = fields.first().map(|f| f.probability().len()).unwrap_or(0);
        let mut flat = Vec::with_capacity(n_steps * n_cells);
        for f in &fields {
            flat.extend_from_slice(f.probability());
        }
        let arr = Array2::from_shape_vec((n_steps, n_cells), flat).map_err(err)?;
        Ok(arr.into_pyarray(py))
    }

    /// Steps whose peak hazard reaches `level`, as
    /// `(step, n_cells, fraction, max_probability)` tuples. Raises
    /// `ValueError` if `level` is not a probability in `[0, 1]` (a NaN would
    /// silently disable every alert) — validated by the core before running.
    fn alerts(&self, py: Python<'_>, level: f64) -> PyResult<Vec<(usize, usize, f64, f64)>> {
        let alerts = py
            .allow_threads(|| match &self.inner {
                Engine::Uniform(n) => n.alerts(level),
                Engine::Gridded(n) => n.alerts(level),
            })
            .map_err(err)?;
        Ok(alerts.iter().map(|a| (a.step, a.n_cells, a.fraction, a.max_probability)).collect())
    }

    /// Exact attribution of the hazard at `(cell, step)` as a dict.
    /// Raises `ValueError` if `cell` or `step` is out of range.
    fn explain<'py>(&self, py: Python<'py>, cell: usize, step: usize) -> PyResult<Bound<'py, PyDict>> {
        let x = match &self.inner {
            Engine::Uniform(n) => n.explain(cell, step).map_err(err)?,
            Engine::Gridded(n) => n.explain(cell, step).map_err(err)?,
        };
        let d = PyDict::new(py);
        d.set_item("cell", x.cell)?;
        d.set_item("step", x.step)?;
        d.set_item("hazard", x.hazard)?;
        d.set_item("susceptibility", x.susceptibility)?;
        d.set_item("trigger_factor", x.trigger_factor)?;
        d.set_item("critical_duration_h", x.critical_duration_h)?;
        d.set_item("mean_intensity_mm_h", x.mean_intensity_mm_h)?;
        d.set_item("critical_intensity_mm_h", x.critical_intensity_mm_h)?;
        d.set_item("exceedance", x.exceedance)?;
        d.set_item("driver", format!("{:?}", x.driver))?;
        Ok(d)
    }

    /// Counterfactual: the mean rainfall intensity (mm/h) sustained over
    /// `duration_h` that would lift `cell`'s hazard to `alert_level`; `None`
    /// if the cell's susceptibility alone cannot reach it (terrain-capped).
    /// The other half of the XAI story next to `explain`. Raises `ValueError`
    /// if `cell` is out of range or `duration_h` is not finite and > 0.
    fn intensity_to_alert(
        &self,
        cell: usize,
        alert_level: f64,
        duration_h: f64,
    ) -> PyResult<Option<f64>> {
        match &self.inner {
            Engine::Uniform(n) => n.intensity_to_alert(cell, alert_level, duration_h).map_err(err),
            Engine::Gridded(n) => n.intensity_to_alert(cell, alert_level, duration_h).map_err(err),
        }
    }
}

/// The streaming engine: ingest forcing step by step, alert as it arrives.
#[pyclass(name = "LiveNowcast")]
struct PyLive {
    inner: CoreLive,
}

#[pymethods]
impl PyLive {
    #[new]
    #[pyo3(signature = (susceptibility, ncols, nrows, dt_hours, id_a=14.82, id_b=0.39, k=4.0, max_window=7))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        susceptibility: PyArrayLike1<'_, f64, AllowTypeChange>,
        ncols: usize,
        nrows: usize,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, _) = build_susc(susceptibility.as_array().to_vec(), ncols, nrows)?;
        let inner = CoreLive::new(
            susc,
            IdThreshold::new(id_a, id_b).map_err(err)?,
            TriggerModel::new(k).map_err(err)?,
            max_window,
            dt_hours,
        )
        .map_err(err)?;
        Ok(Self { inner })
    }

    /// Ingest one step's per-cell depth (mm, 1-D array). Returns
    /// `(hazard, alert)`: the per-cell hazard as a float64 array, and — when
    /// `alert_level` is given — an `(step, n_cells, fraction, max_probability)`
    /// tuple if the step crossed the level, else `None`. Raises `ValueError`
    /// if `alert_level` is not a probability in `[0, 1]`. Releases the GIL
    /// while the engine computes.
    #[pyo3(signature = (depths, alert_level=None))]
    fn push<'py>(
        &mut self,
        py: Python<'py>,
        depths: PyArrayLike1<'_, f64, AllowTypeChange>,
        alert_level: Option<f64>,
    ) -> PushResult<'py> {
        if let Some(level) = alert_level {
            validate_alert_level(level)?;
        }
        // Copy the step out first: the compute must not touch Python memory
        // once the GIL is released.
        let depths = depths.as_array().to_vec();
        let inner = &mut self.inner;
        let field = py.allow_threads(move || inner.push(&depths)).map_err(err)?;
        let alert = match alert_level {
            // Level pre-validated above; the core check is a cheap backstop.
            Some(level) => field
                .alert(level)
                .map_err(err)?
                .map(|a| (a.step, a.n_cells, a.fraction, a.max_probability)),
            None => None,
        };
        Ok((field.probability().to_vec().into_pyarray(py), alert))
    }

    /// Number of steps ingested so far.
    #[getter]
    fn step_index(&self) -> usize {
        self.inner.step_index()
    }
}

/// Isotonic calibrator mapping a raw hazard index to a probability.
#[pyclass(name = "Calibrator")]
struct PyCalibrator {
    inner: CoreCalibrator,
}

#[pymethods]
impl PyCalibrator {
    /// Fit `score → probability` by isotonic regression on `(scores, outcomes)`.
    #[staticmethod]
    fn fit_isotonic(scores: Vec<f64>, outcomes: Vec<bool>) -> PyResult<Self> {
        Ok(Self {
            inner: CoreCalibrator::fit_isotonic(&scores, &outcomes).map_err(err)?,
        })
    }

    /// Calibrated probability for a single raw score. Raises `ValueError` on
    /// a non-finite score (a NaN used to panic instead of raising).
    fn probability(&self, score: f64) -> PyResult<f64> {
        self.inner.probability(score).map_err(err)
    }

    /// Calibrate a list of raw scores into probabilities. Raises `ValueError`
    /// if any score is non-finite.
    fn calibrate(&self, scores: Vec<f64>) -> PyResult<Vec<f64>> {
        self.inner.calibrate(&scores).map_err(err)
    }

    /// Serialize the fitted map as JSON — the same format `nowcast calibrate
    /// --out` writes, so a calibrator fitted here feeds `run/watch
    /// --calibrator` directly.
    fn to_json(&self) -> PyResult<String> {
        serde_json::to_string_pretty(&self.inner).map_err(err)
    }

    /// Load a calibrator from `nowcast calibrate` JSON. The map is validated
    /// (non-empty, finite, monotone, probabilities in [0, 1]); raises
    /// `ValueError` on hand-edited or corrupted input.
    #[staticmethod]
    fn from_json(text: &str) -> PyResult<Self> {
        let inner: CoreCalibrator = serde_json::from_str(text).map_err(err)?;
        inner.validate().map_err(err)?;
        Ok(Self { inner })
    }
}

/// Brier score of probabilistic predictions against binary outcomes.
/// Raises `ValueError` on mismatched lengths or empty input.
#[pyfunction]
fn brier_score(preds: Vec<f64>, outcomes: Vec<bool>) -> PyResult<f64> {
    core_brier(&preds, &outcomes).map_err(err)
}

/// Reliability diagram + Brier/skill/ECE as a dict; `bins` is a list of
/// `(p_pred_mean, p_obs, n, ci_low, ci_high)` tuples with Wilson 95 % intervals.
#[pyfunction]
#[pyo3(signature = (preds, outcomes, n_bins=10))]
fn reliability<'py>(
    py: Python<'py>,
    preds: Vec<f64>,
    outcomes: Vec<bool>,
    n_bins: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let r = core_reliability(&preds, &outcomes, n_bins).map_err(err)?;
    let d = PyDict::new(py);
    d.set_item("brier", r.brier)?;
    d.set_item("brier_skill", r.brier_skill)?;
    d.set_item("ece", r.ece)?;
    d.set_item("base_rate", r.base_rate)?;
    let bins: Vec<(f64, f64, usize, f64, f64)> = r
        .bins
        .iter()
        .map(|b| (b.p_pred_mean, b.p_obs, b.n, b.ci_low, b.ci_high))
        .collect();
    d.set_item("bins", bins)?;
    Ok(d)
}

/// Event-centric monthly contingency (see the Rust docs): `day_month` and
/// `alert_days` align 1:1; events are `(year, month)` pairs; `tol_months`
/// absorbs inventory date noise. Returns a dict with the table and POD / FAR /
/// CSI / frequency bias.
#[pyfunction]
fn monthly_contingency<'py>(
    py: Python<'py>,
    day_month: Vec<(i32, u32)>,
    alert_days: Vec<bool>,
    event_months: Vec<(i32, u32)>,
    tol_months: u32,
) -> PyResult<Bound<'py, PyDict>> {
    let c = nowcast_core::monthly_contingency(&day_month, &alert_days, &event_months, tol_months)
        .map_err(err)?;
    contingency_dict(py, &c)
}

/// Day-resolution spatial contingency (the COOLR-style matcher): `alerted` and
/// `events` are `(cell, day)` pairs on an `ncols × nrows` row-major grid; an
/// event is a hit iff some cell within `cell_radius` (Chebyshev) alerted
/// within `tol_days`. Returns a dict with the table and derived scores.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn spatial_daily_contingency<'py>(
    py: Python<'py>,
    ncols: usize,
    nrows: usize,
    days: Vec<i64>,
    alerted: Vec<(usize, i64)>,
    events: Vec<(usize, i64)>,
    cell_radius: usize,
    tol_days: u32,
) -> PyResult<Bound<'py, PyDict>> {
    let dims = GridDims::new(ncols, nrows);
    let alerted: BTreeSet<(usize, DayKey)> = alerted.into_iter().collect();
    let c = py.allow_threads(|| {
        nowcast_core::spatial_daily_contingency(dims, &days, &alerted, &events, cell_radius, tol_days)
    });
    contingency_dict(py, &c)
}

/// ROC-AUC of a ranked hazard score against binary labels (Mann–Whitney,
/// tie-aware). `None` if either class is empty.
#[pyfunction]
fn roc_auc(scores: PyArrayLike1<'_, f64, AllowTypeChange>, labels: Vec<bool>) -> PyResult<Option<f64>> {
    let scores = scores.as_array().to_vec();
    nowcast_core::roc_auc(&scores, &labels).map_err(err)
}

/// PR-AUC (average precision) of a ranked hazard score against binary labels —
/// the honest discrimination metric at a rare event base rate. `None` if there
/// are no positives.
#[pyfunction]
fn pr_auc(scores: PyArrayLike1<'_, f64, AllowTypeChange>, labels: Vec<bool>) -> PyResult<Option<f64>> {
    let scores = scores.as_array().to_vec();
    nowcast_core::pr_auc(&scores, &labels).map_err(err)
}

/// POD when only the top `area_fraction` of the ranked field can be warned.
#[pyfunction]
fn pod_at_area(
    scores: PyArrayLike1<'_, f64, AllowTypeChange>,
    labels: Vec<bool>,
    area_fraction: f64,
) -> PyResult<Option<f64>> {
    let scores = scores.as_array().to_vec();
    nowcast_core::pod_at_area(&scores, &labels, area_fraction).map_err(err)
}

/// Best warning lead time per deduplicated in-period event, in days: positive =
/// warned in advance, 0 = same-day, negative = late-only alert, `None` = miss.
/// Returns `((cell, day), lead)` pairs in ascending event order.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn lead_times(
    py: Python<'_>,
    ncols: usize,
    nrows: usize,
    days: Vec<i64>,
    alerted: Vec<(usize, i64)>,
    events: Vec<(usize, i64)>,
    cell_radius: usize,
    tol_days: u32,
) -> Vec<((usize, i64), Option<i64>)> {
    let dims = GridDims::new(ncols, nrows);
    let alerted: BTreeSet<(usize, DayKey)> = alerted.into_iter().collect();
    py.allow_threads(|| {
        nowcast_core::lead_times(dims, &days, &alerted, &events, cell_radius, tol_days)
    })
}

/// Dynamic geohazard nowcasting: susceptibility × trigger, with calibration
/// and forecast-verification tools. See the class and function docstrings.
#[pymodule]
fn nowcast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNowcast>()?;
    m.add_class::<PyLive>()?;
    m.add_class::<PyCalibrator>()?;
    m.add_function(wrap_pyfunction!(brier_score, m)?)?;
    m.add_function(wrap_pyfunction!(reliability, m)?)?;
    m.add_function(wrap_pyfunction!(monthly_contingency, m)?)?;
    m.add_function(wrap_pyfunction!(spatial_daily_contingency, m)?)?;
    m.add_function(wrap_pyfunction!(roc_auc, m)?)?;
    m.add_function(wrap_pyfunction!(pr_auc, m)?)?;
    m.add_function(wrap_pyfunction!(pod_at_area, m)?)?;
    m.add_function(wrap_pyfunction!(lead_times, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
