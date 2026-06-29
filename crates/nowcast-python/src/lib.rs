//! Python bindings for nowcast-core (PyO3).
//!
//! Exposes the forward engine ([`Nowcast`]), the streaming engine
//! ([`LiveNowcast`]) and probability calibration ([`Calibrator`], [`reliability`],
//! [`brier_score`]) so the engine can be driven from the Python susceptibility
//! pipeline. Grids cross the boundary as flat Python lists in row-major order
//! (`cell = row * ncols + col`); gridded rainfall is a list of per-step lists.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use nowcast_core::{
    Calibrator as CoreCalibrator, GridDims, GriddedRain, IdThreshold, LiveNowcast as CoreLive,
    Nowcast as CoreNowcast, SusceptibilityMap, TriggerModel, UniformRain,
    brier_score as core_brier, reliability as core_reliability,
};

fn err(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn build_susc(values: Vec<f64>, ncols: usize, nrows: usize) -> PyResult<(SusceptibilityMap, GridDims)> {
    let dims = GridDims::new(ncols, nrows);
    Ok((SusceptibilityMap::new(dims, values).map_err(err)?, dims))
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
    /// Build with a single-gauge rainfall series broadcast over the whole grid.
    #[staticmethod]
    #[pyo3(signature = (susceptibility, ncols, nrows, rain, dt_hours, id_a=14.82, id_b=0.39, k=4.0, max_window=7))]
    #[allow(clippy::too_many_arguments)]
    fn uniform(
        susceptibility: Vec<f64>,
        ncols: usize,
        nrows: usize,
        rain: Vec<f64>,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, dims) = build_susc(susceptibility, ncols, nrows)?;
        let forcing = UniformRain::new(dims, dt_hours, rain).map_err(err)?;
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

    /// Build with distributed rainfall `rain[step][cell]` (mm), one inner list
    /// per time step, each of length `ncols * nrows`.
    #[staticmethod]
    #[pyo3(signature = (susceptibility, ncols, nrows, rain, dt_hours, id_a=14.82, id_b=0.39, k=4.0, max_window=7))]
    #[allow(clippy::too_many_arguments)]
    fn gridded(
        susceptibility: Vec<f64>,
        ncols: usize,
        nrows: usize,
        rain: Vec<Vec<f64>>,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, dims) = build_susc(susceptibility, ncols, nrows)?;
        let n = dims.len();
        let mut flat = Vec::with_capacity(rain.len() * n);
        for (s, step) in rain.iter().enumerate() {
            if step.len() != n {
                return Err(err(format!("rain step {s} has {} cells, grid has {n}", step.len())));
            }
            flat.extend_from_slice(step);
        }
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

    /// Run the nowcast; returns hazard as `result[step][cell]`.
    fn run(&self) -> Vec<Vec<f64>> {
        let fields = match &self.inner {
            Engine::Uniform(n) => n.run(),
            Engine::Gridded(n) => n.run(),
        };
        fields.iter().map(|f| f.probability().to_vec()).collect()
    }

    /// Steps whose peak hazard reaches `level`, as
    /// `(step, n_cells, fraction, max_probability)` tuples.
    fn alerts(&self, level: f64) -> Vec<(usize, usize, f64, f64)> {
        let alerts = match &self.inner {
            Engine::Uniform(n) => n.alerts(level),
            Engine::Gridded(n) => n.alerts(level),
        };
        alerts.iter().map(|a| (a.step, a.n_cells, a.fraction, a.max_probability)).collect()
    }

    /// Exact attribution of the hazard at `(cell, step)` as a dict.
    fn explain<'py>(&self, py: Python<'py>, cell: usize, step: usize) -> PyResult<Bound<'py, PyDict>> {
        let x = match &self.inner {
            Engine::Uniform(n) => n.explain(cell, step),
            Engine::Gridded(n) => n.explain(cell, step),
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
        susceptibility: Vec<f64>,
        ncols: usize,
        nrows: usize,
        dt_hours: f64,
        id_a: f64,
        id_b: f64,
        k: f64,
        max_window: usize,
    ) -> PyResult<Self> {
        let (susc, _) = build_susc(susceptibility, ncols, nrows)?;
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

    /// Ingest one step's per-cell depth (mm); returns the per-cell hazard now.
    fn push(&mut self, depths: Vec<f64>) -> PyResult<Vec<f64>> {
        let field = self.inner.push(&depths).map_err(err)?;
        Ok(field.probability().to_vec())
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

    /// Calibrated probability for a single raw score.
    fn probability(&self, score: f64) -> f64 {
        self.inner.probability(score)
    }

    /// Calibrate a list of raw scores into probabilities.
    fn calibrate(&self, scores: Vec<f64>) -> Vec<f64> {
        self.inner.calibrate(&scores)
    }
}

/// Brier score of probabilistic predictions against binary outcomes.
#[pyfunction]
fn brier_score(preds: Vec<f64>, outcomes: Vec<bool>) -> f64 {
    core_brier(&preds, &outcomes)
}

/// Reliability diagram + Brier/skill/ECE as a dict; `bins` is a list of
/// `(p_pred_mean, p_obs, n, ci_low, ci_high)` tuples with Wilson 95 % intervals.
#[pyfunction]
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

#[pymodule]
fn nowcast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNowcast>()?;
    m.add_class::<PyLive>()?;
    m.add_class::<PyCalibrator>()?;
    m.add_function(wrap_pyfunction!(brier_score, m)?)?;
    m.add_function(wrap_pyfunction!(reliability, m)?)?;
    Ok(())
}
