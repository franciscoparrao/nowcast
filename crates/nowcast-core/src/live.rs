//! Real-time (streaming) nowcasting.
//!
//! [`Nowcast`](crate::Nowcast) is a *batch* engine: it pre-loads the whole
//! forcing series and replays it with per-cell prefix sums. An operational
//! nowcast instead ingests forcing **one step at a time** as it arrives and must
//! emit a hazard field immediately, without seeing the future. [`LiveNowcast`] is
//! that streaming engine.
//!
//! It keeps, per cell, a bounded ring buffer of the most recent prefix sums
//! (length `max_window + 1`) and a running total. On each [`push`](LiveNowcast::push)
//! it computes the worst rolling I–D exceedance ending at the current step using
//! the *identical* subtraction the batch engine uses
//! (`prefix[t+1] − prefix[t+1−m]`), so the streamed hazard is **bit-identical** to
//! [`Nowcast::run`](crate::Nowcast::run) on the same data (see the parity test and
//! the `live_loop` example) while using O(`max_window`) memory per cell instead of
//! O(`n_steps`).
//!
//! A [`StepSource`] abstracts where each step comes from (a replayed forcing, a
//! growing file, a polled service); [`run_live`] drives a source through the
//! engine and hands every step to a callback.

use std::collections::VecDeque;

use crate::error::{Error, Result};
use crate::forcing::Forcing;
use crate::grid::{GridDims, SusceptibilityMap};
use crate::nowcast::HazardField;
use crate::threshold::IdThreshold;
use crate::trigger::TriggerModel;

/// A streaming susceptibility × trigger engine fed one step at a time.
pub struct LiveNowcast {
    susceptibility: SusceptibilityMap,
    threshold: IdThreshold,
    trigger: TriggerModel,
    max_window_steps: usize,
    dt_hours: f64,
    /// Per cell: recent prefix sums, newest at the back, capped at `max_window+1`.
    prefix: Vec<VecDeque<f64>>,
    /// Per cell: running total depth (the latest prefix value).
    cum: Vec<f64>,
    /// Number of steps ingested so far (the index of the next step).
    step: usize,
}

impl LiveNowcast {
    /// Build a streaming engine. `dt_hours` is the step length (the forcing no
    /// longer carries it); `max_window_steps` bounds the longest I–D window.
    pub fn new(
        susceptibility: SusceptibilityMap,
        threshold: IdThreshold,
        trigger: TriggerModel,
        max_window_steps: usize,
        dt_hours: f64,
    ) -> Result<Self> {
        if max_window_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_window_steps",
                reason: "must be >= 1".to_string(),
            });
        }
        if !dt_hours.is_finite() || dt_hours <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "dt_hours",
                reason: format!("must be finite and > 0, got {dt_hours}"),
            });
        }
        let n_cells = susceptibility.dims().len();
        // Each cell starts with prefix[0] = 0 (total before any step).
        let prefix = vec![VecDeque::from([0.0]); n_cells];
        let cum = vec![0.0; n_cells];
        Ok(Self {
            susceptibility,
            threshold,
            trigger,
            max_window_steps,
            dt_hours,
            prefix,
            cum,
            step: 0,
        })
    }

    /// Grid of the engine.
    pub fn dims(&self) -> GridDims {
        self.susceptibility.dims()
    }

    /// Step length in hours.
    pub fn dt_hours(&self) -> f64 {
        self.dt_hours
    }

    /// Number of steps ingested so far (index of the next step to arrive).
    pub fn step_index(&self) -> usize {
        self.step
    }

    /// Ingest one step's per-cell water-input depth (mm) and return the hazard
    /// field for that step. The slice length must equal the grid size, and every
    /// depth must be finite and non-negative — a single `NaN` accepted here would
    /// poison this cell's running prefix sums (and thus every future hazard)
    /// silently, so the operational boundary rejects it up front.
    pub fn push(&mut self, depths: &[f64]) -> Result<HazardField> {
        let dims = self.susceptibility.dims();
        let n = dims.len();
        if depths.len() != n {
            return Err(Error::GridSizeMismatch {
                expected: n,
                got: depths.len(),
                ncols: dims.ncols,
                nrows: dims.nrows,
            });
        }
        if let Some((c, d)) = depths
            .iter()
            .enumerate()
            .find(|(_, d)| !d.is_finite() || **d < 0.0)
        {
            return Err(Error::InvalidParameter {
                name: "depths",
                reason: format!(
                    "depth at cell {c} is {d} at step {}; must be finite and non-negative",
                    self.step
                ),
            });
        }
        let cap = self.max_window_steps + 1;
        let t = self.step;
        let (threshold, trigger, dt) = (self.threshold, self.trigger, self.dt_hours);
        let mut probability = vec![0.0; n];
        for (c, p) in probability.iter_mut().enumerate() {
            // Advance this cell's prefix sums: prefix[t+1] = prefix[t] + depth.
            self.cum[c] += depths[c];
            let buf = &mut self.prefix[c];
            buf.push_back(self.cum[c]);
            if buf.len() > cap {
                buf.pop_front();
            }
            // Worst exceedance over windows m = 1..=max_m, through the shared
            // I-D kernel with the same subtraction the batch engine uses
            // (bit-identical; see the parity test).
            let buf = &self.prefix[c];
            let back = buf.len() - 1; // index of prefix[t+1]
            let prefix_now = buf[back]; // prefix[t+1]
            let max_m = back; // = min(max_window_steps, t+1)
            let best_e = threshold
                .worst_window(dt, max_m, |m| prefix_now - buf[back - m])
                .0;
            *p = self.susceptibility.get(c) * trigger.factor(best_e);
        }
        self.step += 1;
        HazardField::new(t, dims, probability)
    }
}

/// A pull-based source of forcing steps for [`run_live`]: each call yields the
/// next step's per-cell depth (mm), or `None` when the stream ends. Implementors
/// decide where steps come from (a replayed series, a growing file, a feed).
pub trait StepSource {
    /// Grid the steps are defined on.
    fn dims(&self) -> GridDims;
    /// Step length in hours.
    fn dt_hours(&self) -> f64;
    /// The next step's per-cell depth (mm), or `None` at end of stream.
    fn next_step(&mut self) -> Option<Vec<f64>>;
}

/// A [`StepSource`] that replays an in-memory [`Forcing`] step by step — the
/// bridge that lets the streaming engine consume any batch forcing (and the basis
/// of the parity test against [`Nowcast::run`](crate::Nowcast::run)).
pub struct ReplaySource<F: Forcing> {
    forcing: F,
    step: usize,
}

impl<F: Forcing> ReplaySource<F> {
    pub fn new(forcing: F) -> Self {
        Self { forcing, step: 0 }
    }
}

impl<F: Forcing> StepSource for ReplaySource<F> {
    fn dims(&self) -> GridDims {
        self.forcing.dims()
    }

    fn dt_hours(&self) -> f64 {
        self.forcing.dt_hours()
    }

    fn next_step(&mut self) -> Option<Vec<f64>> {
        if self.step >= self.forcing.n_steps() {
            return None;
        }
        let n = self.forcing.dims().len();
        let depths = (0..n).map(|c| self.forcing.depth_mm(c, self.step)).collect();
        self.step += 1;
        Some(depths)
    }
}

/// Drive a [`StepSource`] through a [`LiveNowcast`], handing every step's
/// [`HazardField`] to `on_step` as it is produced. Errors if the source grid or
/// step length disagrees with the engine.
pub fn run_live<S: StepSource>(
    engine: &mut LiveNowcast,
    source: &mut S,
    mut on_step: impl FnMut(&HazardField),
) -> Result<()> {
    if source.dims() != engine.dims() {
        let (s, e) = (source.dims(), engine.dims());
        return Err(Error::GridMismatch {
            susc_cols: e.ncols,
            susc_rows: e.nrows,
            forc_cols: s.ncols,
            forc_rows: s.nrows,
        });
    }
    if source.dt_hours() != engine.dt_hours() {
        return Err(Error::InvalidParameter {
            name: "dt_hours",
            reason: format!(
                "source step {} h differs from engine step {} h",
                source.dt_hours(),
                engine.dt_hours()
            ),
        });
    }
    while let Some(depths) = source.next_step() {
        let field = engine.push(&depths)?;
        on_step(&field);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::forcing::{GriddedRain, UniformRain};
    use crate::nowcast::Nowcast;

    /// The streaming engine must reproduce the batch engine bit-for-bit.
    #[test]
    fn streaming_matches_batch_bit_for_bit() {
        let dims = GridDims::new(3, 2);
        let n = dims.len();
        // A varied per-cell series so windows of different lengths dominate.
        let n_steps = 40;
        let mut depths = Vec::with_capacity(n_steps * n);
        let mut x = 0u64;
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
        let trigger = TriggerModel::new(4.0).unwrap();
        let max_window = 7;

        let batch = Nowcast::new(susc.clone(), forcing.clone(), threshold, trigger, max_window)
            .unwrap()
            .run();

        let mut live = LiveNowcast::new(susc, threshold, trigger, max_window, 6.0).unwrap();
        let mut source = ReplaySource::new(forcing);
        let mut streamed = Vec::new();
        run_live(&mut live, &mut source, |f| streamed.push(f.clone())).unwrap();

        assert_eq!(batch.len(), streamed.len());
        for (b, s) in batch.iter().zip(&streamed) {
            assert_eq!(b.step, s.step);
            // Bit-identical: same f64 bit patterns, not just approximately equal.
            for (pb, ps) in b.probability().iter().zip(s.probability()) {
                assert_eq!(pb.to_bits(), ps.to_bits(), "step {} diverged", b.step);
            }
        }
    }

    #[test]
    fn push_rejects_wrong_length() {
        let dims = GridDims::new(2, 2);
        let susc = SusceptibilityMap::uniform(dims, 0.5).unwrap();
        let mut live =
            LiveNowcast::new(susc, IdThreshold::caine(), TriggerModel::default(), 4, 1.0).unwrap();
        assert!(live.push(&[1.0, 2.0]).is_err()); // grid is 4 cells
        assert!(live.push(&[1.0, 2.0, 3.0, 4.0]).is_ok());
        assert_eq!(live.step_index(), 1);
    }

    #[test]
    fn push_rejects_non_finite_and_negative_depths() {
        let dims = GridDims::new(2, 1);
        let susc = SusceptibilityMap::uniform(dims, 0.5).unwrap();
        let mut live =
            LiveNowcast::new(susc, IdThreshold::caine(), TriggerModel::default(), 4, 1.0).unwrap();
        assert!(live.push(&[1.0, f64::NAN]).is_err());
        assert!(live.push(&[f64::INFINITY, 1.0]).is_err());
        assert!(live.push(&[-0.5, 1.0]).is_err());
        // A rejected push must not have advanced the stream nor the sums.
        assert_eq!(live.step_index(), 0);
        let field = live.push(&[10.0, 10.0]).unwrap();
        assert_eq!(field.step, 0);
    }

    #[test]
    fn rejects_bad_parameters() {
        let dims = GridDims::new(1, 1);
        let susc = SusceptibilityMap::uniform(dims, 0.5).unwrap();
        assert!(LiveNowcast::new(susc.clone(), IdThreshold::caine(), TriggerModel::default(), 0, 1.0).is_err());
        assert!(LiveNowcast::new(susc, IdThreshold::caine(), TriggerModel::default(), 4, 0.0).is_err());
    }

    #[test]
    fn run_live_detects_dt_mismatch() {
        let dims = GridDims::new(1, 1);
        let susc = SusceptibilityMap::uniform(dims, 0.5).unwrap();
        let mut live =
            LiveNowcast::new(susc, IdThreshold::caine(), TriggerModel::default(), 4, 24.0).unwrap();
        let forcing = UniformRain::new(dims, 1.0, vec![5.0, 5.0]).unwrap(); // dt 1h ≠ 24h
        let mut source = ReplaySource::new(forcing);
        assert!(run_live(&mut live, &mut source, |_| {}).is_err());
    }
}
