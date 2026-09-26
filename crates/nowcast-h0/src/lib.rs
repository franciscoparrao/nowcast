//! # nowcast-h0
//!
//! **The zero-degree isotherm as a two-way trigger modulator.**
//!
//! Rain intensity says how much water arrives; the freezing level `H0` says how
//! much of the catchment receives it as *rain* instead of snow. The two are
//! near-independent in the 30–36 °S record (`r = −0.069`, n = 356 storm days),
//! which is exactly why `H0` is worth carrying: it is not a restatement of the
//! rain gauge.
//!
//! ## Why two ways, and not one threshold
//!
//! "Rain high in the cordillera" arrives by two physically distinct routes, and
//! a single criterion misses half the cases:
//!
//! - **Summer** — the isotherm is *naturally* high and the rain is the unusual
//!   part. Witness: the 2021-01-30 San José de Maipo debris flow, `H0` = 4783 m.
//!   That is p75 in absolute terms over the whole series but only ~p15 *for a
//!   January*. A monthly-percentile rule does not see it.
//! - **Winter** — the isotherm is *anomalously* high: the warm storm proper.
//!   Witness: the 2023-06-21/26 atmospheric river, sustained between p66 and
//!   p84 of June storms. An absolute rule does not see it, because 3400 m is
//!   unremarkable on the annual scale.
//!
//! So the index fires when **either** route is satisfied, and reports the
//! signals **separately** ([`H0Signals`]) rather than collapsing them into one
//! scalar: they are different mechanisms and warrant different operator
//! messages.
//!
//! ## The anomaly route has two sub-routes, because height alone missed
//!
//! Fixing the anomaly route at a high percentile does **not** catch its own
//! witness case: the June 2023 river never reaches June's p90 (3802 m) on any
//! of its six days. What distinguishes it is that it *stays* high. So the route
//! fires on either:
//!
//! - **spike** — one day above the high bar (p90 by default);
//! - **sustained** — [`Persistence`]: several consecutive days above a lower
//!   bar (p75 pairs naturally).
//!
//! This *adds* a route instead of lowering the existing one — a single p80 day
//! is still not an alert. The cost is honest and worth stating: a run has to
//! accumulate, so on the witness case a 3-day rule fires on 25 June, two days
//! into a storm documented from the 21st. Longer runs buy specificity and pay
//! in lead time.
//!
//! ## Neither fusion rule works yet — the operational use is shadow mode
//!
//! A high isotherm with no rain is not a hazard, and the record is blunt about
//! it: with a 4000 m absolute threshold, **100 % of January and February days**
//! in the series clear it (95 % / 87 % at 4500 m). Restricted to storm days it
//! is 46/356 and 16/356. Fusing this trigger with the rainfall I–D by
//! [`Combine::NoisyOr`](nowcast_core::Combine) would therefore alarm every
//! summer day.
//!
//! An earlier version of this note said the fix was
//! [`Combine::Product`](nowcast_core::Combine) "so it can only amplify a
//! trigger the rain already supports". **That is false**: both factors lie in
//! `[0, 1]`, so their product can only *lower* the rain. Measured on the 356
//! storm days of the series (4500 m absolute, p90 spike, `k = 4`), the fused
//! factor has median 0.32 (p10 0.14, p90 0.53); in May–August it is below 0.5
//! on **88 %** of storm days — including the five documented winter storms,
//! which are warm-ish, not warm. Under `Product` this trigger acts as a
//! snow-line *damper* and would suppress most winter rainfall alerts. That is
//! physically arguable (snow does not run off) but uncalibrated, and the
//! persistence sweep already showed the index cannot be calibrated against an
//! inventory that does not separate mechanisms.
//!
//! So the CLI (`nowcast run --h0-csv`) uses it in **shadow mode**: the
//! [`H0Signals`] are reported next to every step and the hazard is left
//! untouched. [`Trigger::factor`] is still provided for experiments; if you
//! fuse it by `Product`, [`MissingPolicy::Neutral`] (the default) keeps a gap
//! from erasing a rainfall alert — a day without `H0` means "don't know",
//! never "isotherm low".
//!
//! ## Scope and honesty about the input
//!
//! The series covers **30–36 °S only**; Atacama and Aysén need their own fit,
//! and southern stations lack the vertical gradient to support one. It has
//! deliberate gaps — only days passing physical criteria are present — and a
//! residual bias of roughly +250…+450 m against radiosonde. Hence
//! [`AbsoluteWay`] thresholds must be set **on the scale of this series**, not
//! from radiosonde values.
//!
//! **Known misalignment, not yet corrected.** The series is stamped at 08:00 in
//! **UTC−4 fixed** (verified 2026-09-04: 24 records/day across all 18 Chilean
//! DST transitions 2018-2026, and a daily temperature maximum at 13:00 in every
//! month — no summer shift). A nowcast driven by IMERG runs on **UTC**, so
//! "day" here and "day" there are offset by 4 h. For a daily modulator that is
//! a fraction of a step and it is left uncorrected on purpose rather than
//! papered over; it would matter if the trigger ever ran on sub-daily H0.
//!
//! ```
//! use nowcast_core::{Combine, GridDims, IdThreshold, IdTrigger, MultiNowcast,
//!                    SusceptibilityMap, TriggerModel, UniformRain};
//! use nowcast_h0::{AbsoluteWay, MonthlyStormClimatology, TwoWayH0Trigger,
//!                  ZeroIsothermSeries};
//!
//! let dims = GridDims::new(2, 1);
//! // Two January days: a high-but-ordinary summer isotherm.
//! let series = ZeroIsothermSeries::from_csv(
//!     "fecha,h0_m,tormenta\n2021-01-30,4783,1\n2021-01-31,4700,1\n").unwrap();
//! let clim = MonthlyStormClimatology::from_series(&series);
//! let h0 = TwoWayH0Trigger::new(
//!     dims, series, clim, AbsoluteWay::new(4500.0).unwrap(),
//!     "2021-01-30", 24.0, 2, TriggerModel::default(),
//! ).unwrap();
//!
//! // The absolute way fires; the anomaly way cannot be evaluated (January has
//! // too few storm days to define a p90) — and that is reported, not faked.
//! let s = h0.signals(0);
//! assert!(s.absolute.unwrap() > 0.5);
//! assert!(s.anomaly.is_none());
//! ```

use nowcast_core::{Error, GridDims, Result, Trigger, TriggerModel};

/// Minimum storm days in a calendar month before a percentile is considered
/// defined. Matches the upstream climatology report, so the two agree by
/// construction.
pub const MIN_STORM_DAYS: usize = 8;

/// Percentile of the month's storm days that defines "anomalously warm".
///
/// 90 as specified — but see [`MonthlyStormClimatology::from_series_at`]: at 90
/// the winter witness case (2023-06-21/26, p66–p84 of June) does **not** fire.
pub const DEFAULT_ANOMALY_PERCENTILE: f64 = 90.0;

// ---------------------------------------------------------------------------
// Dates without a date crate
// ---------------------------------------------------------------------------

/// Days since 1970-01-01 for a proleptic-Gregorian y/m/d (Hinnant's algorithm).
/// Twenty lines here buy the crate its place in the offline `std`-only gate.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// Inverse of [`days_from_civil`] (Hinnant's algorithm): epoch day → `(y, m, d)`.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// Length of month `m` in year `y`. Without this, `2021-02-31` parses and
/// `days_from_civil` quietly slides it into March — a silently wrong day.
fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        _ => 0,
    }
}

/// Parse `YYYY-MM-DD` → (days since epoch, month 1..=12). Strict on purpose:
/// a malformed date must not silently become day zero.
fn parse_date(s: &str) -> Result<(i64, usize)> {
    let bad = || Error::InvalidParameter {
        name: "fecha",
        reason: format!("expected YYYY-MM-DD, got {s:?}"),
    };
    let s = s.trim();
    let b = s.as_bytes();
    // Positional parsing must also check the separators, or "2021/01/30" — and
    // anything else with digits in the right slots — slips through as valid.
    if b.len() < 10 || b[4] != b'-' || b[7] != b'-' {
        return Err(bad());
    }
    if !b[..10]
        .iter()
        .enumerate()
        .all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
    {
        return Err(bad());
    }
    let y: i64 = s[0..4].parse().map_err(|_| bad())?;
    let m: i64 = s[5..7].parse().map_err(|_| bad())?;
    let d: i64 = s[8..10].parse().map_err(|_| bad())?;
    if !(1..=12).contains(&m) || d < 1 || d > days_in_month(y, m) {
        return Err(bad());
    }
    Ok((days_from_civil(y, m, d), m as usize))
}

// ---------------------------------------------------------------------------
// The series
// ---------------------------------------------------------------------------

/// One day of the H0 record.
#[derive(Debug, Clone, Copy)]
struct Day {
    epoch_day: i64,
    month: usize,
    h0_m: f64,
    storm: bool,
}

/// A daily zero-isotherm series with **explicit gaps**.
///
/// Absent days are absent — [`h0_on_day`](Self::h0_on_day) returns `None`, and
/// no part of this crate substitutes a number for them.
#[derive(Debug, Clone, Default)]
pub struct ZeroIsothermSeries {
    days: Vec<Day>, // sorted by epoch_day
}

impl ZeroIsothermSeries {
    /// Parse the `h0_centro_diaria_v3.csv` layout. Columns are located **by
    /// header name** (`fecha`, `h0_m`, `tormenta`), so upstream may add or
    /// reorder columns without breaking this — the lesson from the agromet id
    /// that changed under our feet.
    ///
    /// Rows whose `h0_m` is empty or unparseable are **skipped**, which keeps
    /// them gaps rather than zeros.
    pub fn from_csv(text: &str) -> Result<Self> {
        let mut lines = text.lines().filter(|l| !l.trim().is_empty());
        let header = lines.next().ok_or_else(|| Error::InvalidParameter {
            name: "csv",
            reason: "empty input: no header row".into(),
        })?;
        let cols: Vec<&str> = header.split(',').map(|c| c.trim()).collect();
        let idx = |name: &'static str| -> Result<usize> {
            cols.iter()
                .position(|c| *c == name)
                .ok_or(Error::InvalidParameter {
                    name: "csv",
                    reason: format!("missing required column {name:?}; found {cols:?}"),
                })
        };
        let (i_fecha, i_h0) = (idx("fecha")?, idx("h0_m")?);
        let i_storm = cols.iter().position(|c| *c == "tormenta");

        let mut days = Vec::new();
        for line in lines {
            let f: Vec<&str> = line.split(',').collect();
            if f.len() <= i_fecha.max(i_h0) {
                continue;
            }
            let (epoch_day, month) = parse_date(f[i_fecha])?;
            let Ok(h0_m) = f[i_h0].trim().parse::<f64>() else {
                continue; // unparseable → gap, never 0.0
            };
            if !h0_m.is_finite() {
                continue;
            }
            let storm = i_storm
                .and_then(|i| f.get(i))
                .map(|v| v.trim() == "1")
                .unwrap_or(false);
            days.push(Day {
                epoch_day,
                month,
                h0_m,
                storm,
            });
        }
        days.sort_by_key(|d| d.epoch_day);
        days.dedup_by_key(|d| d.epoch_day);
        Ok(Self { days })
    }

    fn find(&self, epoch_day: i64) -> Option<&Day> {
        self.days
            .binary_search_by_key(&epoch_day, |d| d.epoch_day)
            .ok()
            .map(|i| &self.days[i])
    }

    /// H0 in metres for a `YYYY-MM-DD` date, or `None` if that day is a gap.
    pub fn h0_on(&self, date: &str) -> Result<Option<f64>> {
        let (day, _) = parse_date(date)?;
        Ok(self.h0_on_day(day))
    }

    fn h0_on_day(&self, epoch_day: i64) -> Option<f64> {
        self.find(epoch_day).map(|d| d.h0_m)
    }

    /// Number of days actually present (gaps are not counted).
    pub fn len(&self) -> usize {
        self.days.len()
    }

    pub fn is_empty(&self) -> bool {
        self.days.is_empty()
    }

    /// Last day present, as `YYYY-MM-DD` — what an operator checks to know
    /// whether the series is still being produced. `None` on an empty series.
    pub fn last_date(&self) -> Option<String> {
        self.days.last().map(|d| {
            let (y, m, dd) = civil_from_days(d.epoch_day);
            format!("{y:04}-{m:02}-{dd:02}")
        })
    }
}

// ---------------------------------------------------------------------------
// Monthly storm climatology
// ---------------------------------------------------------------------------

/// `np.percentile`-compatible linear interpolation (Hyndman–Fan type 7), so a
/// p90 computed here equals the one in the upstream report to the metre.
fn percentile_linear(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    if n == 1 {
        return sorted[0];
    }
    let pos = q / 100.0 * (n - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    if lo == hi {
        return sorted[lo];
    }
    sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo as f64)
}

/// Per-calendar-month p90 of H0 **on storm days** — what "warm storm" means for
/// each time of year.
///
/// A month with fewer than [`MIN_STORM_DAYS`] storm days has **no** p90. That
/// is not a technicality: January, February and December fall below the bar,
/// which is precisely when the absolute way has to carry the load. The two ways
/// cover each other, and a month without climatology must report
/// *inevaluable*, never *did not fire*.
#[derive(Debug, Clone)]
pub struct MonthlyStormClimatology {
    p90_m: [Option<f64>; 12],
    n_storm: [usize; 12],
    percentile: f64,
}

impl Default for MonthlyStormClimatology {
    fn default() -> Self {
        Self {
            p90_m: [None; 12],
            n_storm: [0; 12],
            percentile: DEFAULT_ANOMALY_PERCENTILE,
        }
    }
}

impl MonthlyStormClimatology {
    /// Compute from the series' own storm days at the default percentile.
    /// Preferred over reading the upstream report: same numbers, but a drift
    /// between the two shows up as a failing test instead of a silent
    /// disagreement.
    pub fn from_series(series: &ZeroIsothermSeries) -> Self {
        Self::from_series_at(series, DEFAULT_ANOMALY_PERCENTILE)
            .expect("default percentile is in range")
    }

    /// Same, at an explicit percentile.
    ///
    /// **The default of 90 does not fire on the winter witness case.** The
    /// 2023-06-21/26 atmospheric river sits at p66–p84 of June storms (measured:
    /// 65.9, 72.7, 78.4, 80.7, 83.0, 84.1), entirely below June's p90 of 3802 m.
    /// A p90 rule therefore misses the very event the anomaly route exists for.
    /// The percentile is a parameter here precisely so that choice is explicit
    /// and revisable rather than baked in; note also that the witness case is
    /// described as *sustained over six days*, which suggests persistence — not
    /// just height — is part of the mechanism.
    pub fn from_series_at(series: &ZeroIsothermSeries, percentile: f64) -> Result<Self> {
        if !percentile.is_finite() || !(0.0..=100.0).contains(&percentile) {
            return Err(Error::InvalidParameter {
                name: "percentile",
                reason: format!("must be in [0, 100], got {percentile}"),
            });
        }
        let mut buckets: [Vec<f64>; 12] = Default::default();
        for d in &series.days {
            if d.storm {
                buckets[d.month - 1].push(d.h0_m);
            }
        }
        let mut p90_m = [None; 12];
        let mut n_storm = [0usize; 12];
        for (m, vals) in buckets.iter_mut().enumerate() {
            n_storm[m] = vals.len();
            if vals.len() >= MIN_STORM_DAYS {
                vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
                p90_m[m] = Some(percentile_linear(vals, percentile));
            }
        }
        Ok(Self {
            p90_m,
            n_storm,
            percentile,
        })
    }

    /// Build from explicit threshold values (index 0 = January). `None` = undefined.
    pub fn from_p90(p90_m: [Option<f64>; 12]) -> Self {
        Self {
            p90_m,
            n_storm: [0; 12],
            percentile: DEFAULT_ANOMALY_PERCENTILE,
        }
    }

    /// The percentile these thresholds were computed at.
    pub fn percentile(&self) -> f64 {
        self.percentile
    }

    /// p90 for month `1..=12`, or `None` where undefined.
    pub fn p90(&self, month: usize) -> Option<f64> {
        self.p90_m.get(month.checked_sub(1)?).copied().flatten()
    }

    /// Storm-day count backing month `1..=12` (0 when built via [`Self::from_p90`]).
    pub fn n_storm_days(&self, month: usize) -> usize {
        month
            .checked_sub(1)
            .and_then(|i| self.n_storm.get(i))
            .copied()
            .unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// Persistence: the sustained sub-route of the anomaly way
// ---------------------------------------------------------------------------

/// A **sustained** warm anomaly: H0 above a lower monthly bar for several
/// consecutive days.
///
/// Why this exists. The winter witness case — the 2023-06-21/26 atmospheric
/// river — never reaches June's p90 (3802 m) on any of its six days; it sits at
/// p66–p84 and *stays there*. Height alone at a high percentile does not
/// describe that event; **duration at a moderate one does**. So the anomaly way
/// gets two sub-routes and fires on either:
///
/// - **spike** — one day above the high bar (p90 by default), unchanged;
/// - **sustained** — [`min_days`](Self::min_days) consecutive days above a
///   lower bar (p75 is the natural companion).
///
/// This adds a route rather than lowering the existing one: a single p85 day is
/// still not an alert, and a week of p78 now is.
///
/// **A gap breaks the run.** A missing day is "don't know", and counting it as
/// part of a streak would be asserting persistence over days we never observed.
/// Breaking is the conservative direction: it can only withhold the sustained
/// route, never invent it. Same for a month with no climatology.
#[derive(Debug, Clone)]
pub struct Persistence {
    clim: MonthlyStormClimatology,
    min_days: usize,
}

/// Hard stop for the backward walk. The series is finite so the walk always
/// terminates on its own; this only bounds the work per step.
const MAX_RUN_DAYS: usize = 366;

impl Persistence {
    /// `clim` should be built at a **lower** percentile than the spike route
    /// (e.g. 75 against 90), and `min_days >= 2` — a "sustained" run of one day
    /// is just the spike route with a lower bar, which is the very thing this
    /// is meant to avoid.
    pub fn new(clim: MonthlyStormClimatology, min_days: usize) -> Result<Self> {
        if min_days < 2 {
            return Err(Error::InvalidParameter {
                name: "min_days",
                reason: format!(
                    "must be >= 2 (a 1-day 'run' is just a lower spike threshold), got {min_days}"
                ),
            });
        }
        if min_days > MAX_RUN_DAYS {
            return Err(Error::InvalidParameter {
                name: "min_days",
                reason: format!("must be <= {MAX_RUN_DAYS}, got {min_days}"),
            });
        }
        Ok(Self { clim, min_days })
    }

    pub fn min_days(&self) -> usize {
        self.min_days
    }

    pub fn climatology(&self) -> &MonthlyStormClimatology {
        &self.clim
    }
}

// ---------------------------------------------------------------------------
// The absolute way
// ---------------------------------------------------------------------------

/// The absolute route: H0 above a fixed height relevant to the catchment's
/// hypsometry (order 4000–4500 m for the Maipo; calibrate against the real
/// hypsometry via `surtgis`).
///
/// **Set this on the scale of the H0 series**, not from radiosonde values: the
/// series carries a residual bias of roughly +250…+450 m depending on stratum,
/// so a threshold borrowed from sounding data is silently offset.
#[derive(Debug, Clone, Copy)]
pub struct AbsoluteWay {
    threshold_m: f64,
}

impl AbsoluteWay {
    pub fn new(threshold_m: f64) -> Result<Self> {
        if !threshold_m.is_finite() || threshold_m <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "threshold_m",
                reason: format!("must be finite and > 0, got {threshold_m}"),
            });
        }
        Ok(Self { threshold_m })
    }

    pub fn threshold_m(&self) -> f64 {
        self.threshold_m
    }
}

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------

/// Which route (if any) carried the day. Reported so the operator sees the
/// *mechanism*, not just a number: a summer cloudburst on a permanently high
/// isotherm and a winter warm storm deserve different messages.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum H0Via {
    /// No H0 for this day — "don't know", not "low".
    Missing,
    /// H0 present, neither route above its threshold.
    Quiet,
    /// Absolute route only (summer pattern).
    Absolute,
    /// Monthly-anomaly route only (winter warm storm).
    Anomalous,
    /// Both routes.
    Both,
}

impl H0Via {
    /// Stable lowercase label for machine output (JSON, logs).
    pub fn as_str(self) -> &'static str {
        match self {
            H0Via::Missing => "missing",
            H0Via::Quiet => "quiet",
            H0Via::Absolute => "absolute",
            H0Via::Anomalous => "anomalous",
            H0Via::Both => "both",
        }
    }
}

/// The two routes reported **separately**, plus the inputs behind them.
#[derive(Debug, Clone, Copy)]
pub struct H0Signals {
    /// Isotherm height, or `None` on a gap day.
    pub h0_m: Option<f64>,
    /// Absolute-route factor in `[0, 1]`; `None` on a gap day.
    pub absolute: Option<f64>,
    /// Anomaly-route factor in `[0, 1]` — the stronger of the two sub-routes
    /// below. `None` on a gap day **or** in a month with no defined p90
    /// (inevaluable ≠ did not fire).
    pub anomaly: Option<f64>,
    /// Spike sub-route: today alone above the high monthly bar.
    pub anomaly_spike: Option<f64>,
    /// Sustained sub-route: `Some` only once the run reaches
    /// [`Persistence::min_days`]; `None` while it is still building (watch
    /// [`run_days`](Self::run_days)) or when no [`Persistence`] is configured.
    pub anomaly_sustained: Option<f64>,
    /// Consecutive days up to and including today above the *lower* sustained
    /// bar. Always reported, so a run that is building is visible before it
    /// fires. `0` without a [`Persistence`].
    pub run_days: usize,
    /// The month's storm p90, when defined.
    pub month_p90_m: Option<f64>,
    /// Calendar month `1..=12` the step fell in.
    pub month: usize,
    pub via: H0Via,
}

/// What a gap contributes to the fused factor.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum MissingPolicy {
    /// `1.0` — changes nothing under
    /// [`Combine::Product`](nowcast_core::Combine). The default, because this
    /// trigger is a *modulator*: on a day with no isotherm the rainfall verdict
    /// must stand unaltered.
    #[default]
    Neutral,
    /// `0.0` — "no evidence", for
    /// [`Combine::NoisyOr`](nowcast_core::Combine)/`Max` use. Read the crate
    /// note first: OR-fusing this trigger with rain alarms every summer day.
    NoEvidence,
}

// ---------------------------------------------------------------------------
// The trigger
// ---------------------------------------------------------------------------

/// The two-way H0 index as a composable [`Trigger`].
///
/// Uniform in space (v1), like `nowcast-omori`'s seismicity rate: the series is
/// a single transect-wide estimate, and pretending otherwise would invent
/// spatial structure the data does not have.
#[derive(Debug, Clone)]
pub struct TwoWayH0Trigger {
    dims: GridDims,
    series: ZeroIsothermSeries,
    clim: MonthlyStormClimatology,
    absolute: AbsoluteWay,
    start_day: i64,
    start_hour: f64,
    dt_hours: f64,
    n_steps: usize,
    model: TriggerModel,
    missing: MissingPolicy,
    persistence: Option<Persistence>,
}

impl TwoWayH0Trigger {
    /// `start_date` is `YYYY-MM-DD` for step 0; each step maps to the calendar
    /// day it falls in (the series is daily, so sub-daily steps share a day).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        dims: GridDims,
        series: ZeroIsothermSeries,
        clim: MonthlyStormClimatology,
        absolute: AbsoluteWay,
        start_date: &str,
        dt_hours: f64,
        n_steps: usize,
        model: TriggerModel,
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
        let (start_day, _) = parse_date(start_date)?;
        Ok(Self {
            dims,
            series,
            clim,
            absolute,
            start_day,
            start_hour: 0.0,
            dt_hours,
            n_steps,
            model,
            missing: MissingPolicy::default(),
            persistence: None,
        })
    }

    /// Hour of `start_date` at which step 0 begins, in `[0, 24)`. Defaults to
    /// `0.0` (midnight). Without it a sub-daily window that starts in the
    /// afternoon maps its evening steps onto the wrong day: at 30-min steps
    /// from 14:00, step 20 is 00:00 of the next day, not "still day 0".
    pub fn with_start_hour(mut self, hour: f64) -> Result<Self> {
        if !hour.is_finite() || !(0.0..24.0).contains(&hour) {
            return Err(Error::InvalidParameter {
                name: "start_hour",
                reason: format!("must be in [0, 24), got {hour}"),
            });
        }
        self.start_hour = hour;
        Ok(self)
    }

    /// Override how gap days contribute. See [`MissingPolicy`].
    pub fn with_missing_policy(mut self, policy: MissingPolicy) -> Self {
        self.missing = policy;
        self
    }

    /// Add the sustained sub-route to the anomaly way. See [`Persistence`].
    pub fn with_persistence(mut self, persistence: Persistence) -> Self {
        self.persistence = Some(persistence);
        self
    }

    /// Consecutive days ending at `day` (inclusive) above the sustained bar.
    ///
    /// Walks **backwards only** — a run may never be extended by days the
    /// nowcast has not reached yet, or the trigger would be reading the future.
    fn run_days_ending(&self, day: i64, p: &Persistence) -> usize {
        let mut n = 0;
        let mut d = day;
        while n < MAX_RUN_DAYS {
            let Some(rec) = self.series.find(d) else { break }; // gap breaks the run
            let Some(bar) = p.clim.p90(rec.month) else { break }; // no climatology, no claim
            if rec.h0_m < bar {
                break;
            }
            n += 1;
            d -= 1;
        }
        n
    }

    /// Calendar day (epoch days) and month for a step.
    fn day_of(&self, step: usize) -> (i64, usize) {
        let offset = ((self.start_hour + step as f64 * self.dt_hours) / 24.0).floor() as i64;
        let day = self.start_day + offset;
        let month = self
            .series
            .find(day)
            .map(|d| d.month)
            .unwrap_or_else(|| month_of_epoch_day(day));
        (day, month)
    }

    /// Both routes for `step`, reported separately. This is the output the
    /// operator should see; [`Trigger::factor`] is only its fused shadow.
    pub fn signals(&self, step: usize) -> H0Signals {
        let (day, month) = self.day_of(step);
        let Some(h0_m) = self.series.h0_on_day(day) else {
            return H0Signals {
                h0_m: None,
                absolute: None,
                anomaly: None,
                anomaly_spike: None,
                anomaly_sustained: None,
                run_days: 0,
                month_p90_m: self.clim.p90(month),
                month,
                via: H0Via::Missing,
            };
        };
        let abs_f = self.model.factor(h0_m / self.absolute.threshold_m());
        let p90 = self.clim.p90(month);
        let spike_f = p90.map(|p| self.model.factor(h0_m / p));

        // Sustained sub-route: only speaks once the run is long enough.
        let (run_days, sustained_f) = match &self.persistence {
            None => (0, None),
            Some(p) => {
                let run = self.run_days_ending(day, p);
                let f = (run >= p.min_days)
                    .then(|| p.clim.p90(month).map(|bar| self.model.factor(h0_m / bar)))
                    .flatten();
                (run, f)
            }
        };

        // The anomaly way is whichever sub-route speaks loudest. `None` only if
        // neither can be evaluated at all.
        let ano_f = match (spike_f, sustained_f) {
            (None, None) => None,
            (a, b) => Some(a.unwrap_or(0.0).max(b.unwrap_or(0.0))),
        };

        // "Fired" = above the threshold itself, i.e. ratio >= 1, which the
        // logistic maps to exactly 0.5. Reading the route off the factor keeps
        // this consistent with whatever steepness the model was built with.
        let abs_hit = abs_f >= 0.5;
        let ano_hit = ano_f.is_some_and(|f| f >= 0.5);
        let via = match (abs_hit, ano_hit) {
            (true, true) => H0Via::Both,
            (true, false) => H0Via::Absolute,
            (false, true) => H0Via::Anomalous,
            (false, false) => H0Via::Quiet,
        };
        H0Signals {
            h0_m: Some(h0_m),
            absolute: Some(abs_f),
            anomaly: ano_f,
            anomaly_spike: spike_f,
            anomaly_sustained: sustained_f,
            run_days,
            month_p90_m: p90,
            month,
            via,
        }
    }
}

/// Month for a day the series does not contain (needed to look up climatology
/// even on a gap day).
fn month_of_epoch_day(z: i64) -> usize {
    civil_from_days(z).1 as usize
}

impl Trigger for TwoWayH0Trigger {
    fn dims(&self) -> GridDims {
        self.dims
    }

    fn n_steps(&self) -> usize {
        self.n_steps
    }

    /// The fused index: **either** route may carry it (max), which is the "any
    /// of the two conditions, not their intersection" rule.
    ///
    /// On a gap day the value comes from [`MissingPolicy`] — under the default
    /// `Neutral` that is `1.0`, so a missing isotherm leaves a rainfall alert
    /// exactly as it was.
    fn factor(&self, _cell: usize, step: usize) -> f64 {
        let s = self.signals(step);
        match (s.absolute, s.anomaly) {
            (None, _) => match self.missing {
                MissingPolicy::Neutral => 1.0,
                MissingPolicy::NoEvidence => 0.0,
            },
            (Some(a), Some(b)) => a.max(b),
            (Some(a), None) => a,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CSV: &str = "fecha,h0_m,ci_lo,ci_hi,tormenta\n\
                       2023-06-21,3600,3400,3800,1\n\
                       2023-06-23,3700,3500,3900,1\n\
                       2021-01-30,4783,4500,5000,1\n";

    #[test]
    fn dates_round_trip() {
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(days_from_civil(2021, 1, 30), 18657);
        // leap-year boundary, the classic off-by-one
        assert_eq!(days_from_civil(2020, 3, 1) - days_from_civil(2020, 2, 28), 2);
        for (y, m, d) in [(2021, 1, 30), (2023, 6, 21), (2026, 12, 31), (2000, 2, 29)] {
            assert_eq!(month_of_epoch_day(days_from_civil(y, m, d)), m as usize);
        }
    }

    #[test]
    fn rejects_malformed_dates() {
        assert!(parse_date("2021-13-01").is_err(), "month 13");
        assert!(parse_date("2021-01-00").is_err(), "day 0");
        assert!(parse_date("nope").is_err());
        assert!(parse_date("2021/01/30").is_err(), "wrong separator");
        assert!(parse_date("2021-02-31").is_err(), "31 February");
        assert!(parse_date("2021-02-29").is_err(), "2021 is not a leap year");
        assert!(parse_date("2020-02-29").is_ok(), "2020 is");
        assert!(parse_date("2000-02-29").is_ok(), "century leap year");
        assert!(parse_date("1900-02-29").is_err(), "1900 is not");
        assert!(parse_date("2021-04-31").is_err(), "April has 30");
        assert!(parse_date("2021-0a-01").is_err(), "non-digit");
    }

    #[test]
    fn percentile_matches_numpy_linear() {
        // np.percentile([1,2,3,4], 90) == 3.7
        let v = [1.0, 2.0, 3.0, 4.0];
        assert!((percentile_linear(&v, 90.0) - 3.7).abs() < 1e-12);
        // np.percentile([1,2,3,4], 50) == 2.5
        assert!((percentile_linear(&v, 50.0) - 2.5).abs() < 1e-12);
        assert_eq!(percentile_linear(&[7.0], 90.0), 7.0);
    }

    #[test]
    fn missing_day_is_not_zero() {
        let s = ZeroIsothermSeries::from_csv(CSV).unwrap();
        assert_eq!(s.h0_on("2023-06-22").unwrap(), None, "gap must stay a gap");
        assert_eq!(s.h0_on("2023-06-21").unwrap(), Some(3600.0));
    }

    #[test]
    fn unparseable_h0_becomes_a_gap_not_a_zero() {
        let s = ZeroIsothermSeries::from_csv(
            "fecha,h0_m,tormenta\n2023-06-21,,1\n2023-06-22,nan,1\n2023-06-23,3700,1\n",
        )
        .unwrap();
        assert_eq!(s.h0_on("2023-06-21").unwrap(), None);
        assert_eq!(s.h0_on("2023-06-22").unwrap(), None);
        assert_eq!(s.len(), 1);
    }

    #[test]
    fn columns_located_by_name_not_position() {
        let s = ZeroIsothermSeries::from_csv(
            "tormenta,extra,h0_m,fecha\n1,zzz,3600,2023-06-21\n",
        )
        .unwrap();
        assert_eq!(s.h0_on("2023-06-21").unwrap(), Some(3600.0));
        assert!(ZeroIsothermSeries::from_csv("a,b\n1,2\n").is_err(), "no fecha/h0_m");
    }

    fn trigger(csv: &str, thr: f64, start: &str, n: usize) -> TwoWayH0Trigger {
        let series = ZeroIsothermSeries::from_csv(csv).unwrap();
        let clim = MonthlyStormClimatology::from_series(&series);
        TwoWayH0Trigger::new(
            GridDims::new(1, 1),
            series,
            clim,
            AbsoluteWay::new(thr).unwrap(),
            start,
            24.0,
            n,
            TriggerModel::default(),
        )
        .unwrap()
    }

    #[test]
    fn summer_case_fires_absolute_while_anomaly_is_inevaluable() {
        // San José de Maipo 2021-01-30: high in absolute terms, ordinary for a
        // January — and January has too few storm days for a p90 at all.
        let t = trigger(CSV, 4500.0, "2021-01-30", 1);
        let s = t.signals(0);
        assert_eq!(s.via, H0Via::Absolute);
        assert!(s.absolute.unwrap() > 0.5);
        assert!(s.anomaly.is_none(), "January p90 must be undefined, not 0");
        assert!(s.month_p90_m.is_none());
    }

    #[test]
    fn winter_case_needs_the_anomaly_way() {
        // 3600 m is unremarkable annually (below a 4500 m absolute bar) but
        // above a June p90 fixed at 3400: only the anomaly route sees it.
        let mut p90 = [None; 12];
        p90[5] = Some(3400.0);
        let series = ZeroIsothermSeries::from_csv(CSV).unwrap();
        let t = TwoWayH0Trigger::new(
            GridDims::new(1, 1),
            series,
            MonthlyStormClimatology::from_p90(p90),
            AbsoluteWay::new(4500.0).unwrap(),
            "2023-06-21",
            24.0,
            1,
            TriggerModel::default(),
        )
        .unwrap();
        let s = t.signals(0);
        assert_eq!(s.via, H0Via::Anomalous);
        assert!(s.absolute.unwrap() < 0.5, "absolute route must NOT see it");
        assert!(s.anomaly.unwrap() > 0.5);
    }

    #[test]
    fn gap_is_neutral_by_default_and_no_evidence_on_request() {
        let t = trigger(CSV, 4500.0, "2023-06-22", 1); // 22-jun is a gap
        assert_eq!(t.signals(0).via, H0Via::Missing);
        assert_eq!(t.factor(0, 0), 1.0, "Neutral must not erase a rain alert");
        let t = t.with_missing_policy(MissingPolicy::NoEvidence);
        assert_eq!(t.factor(0, 0), 0.0);
    }

    #[test]
    fn gap_under_product_leaves_rainfall_untouched() {
        // The invariant that matters: multiplying by a gap changes nothing.
        let t = trigger(CSV, 4500.0, "2023-06-22", 1);
        let rain = 0.83_f64;
        assert!((rain * t.factor(0, 0) - rain).abs() < 1e-12);
    }

    #[test]
    fn either_way_suffices_never_their_intersection() {
        let t = trigger(CSV, 4500.0, "2021-01-30", 1);
        let s = t.signals(0);
        // anomaly inevaluable, yet the fused factor still fires on absolute
        assert!(s.anomaly.is_none());
        assert!(t.factor(0, 0) > 0.5, "one route must be enough");
    }

    #[test]
    fn steps_map_to_calendar_days() {
        let t = trigger(CSV, 4500.0, "2023-06-21", 3);
        assert_eq!(t.signals(0).h0_m, Some(3600.0));
        assert_eq!(t.signals(1).h0_m, None, "22-jun missing");
        assert_eq!(t.signals(2).h0_m, Some(3700.0));
    }

    #[test]
    fn sub_daily_steps_share_their_day() {
        let series = ZeroIsothermSeries::from_csv(CSV).unwrap();
        let clim = MonthlyStormClimatology::from_series(&series);
        let t = TwoWayH0Trigger::new(
            GridDims::new(1, 1),
            series,
            clim,
            AbsoluteWay::new(4500.0).unwrap(),
            "2023-06-21",
            6.0, // 4 steps per day
            8,
            TriggerModel::default(),
        )
        .unwrap();
        for step in 0..4 {
            assert_eq!(t.signals(step).h0_m, Some(3600.0), "step {step}");
        }
        assert_eq!(t.signals(4).h0_m, None, "next day is a gap");
    }

    #[test]
    fn hostile_inputs_are_rejected() {
        let s = ZeroIsothermSeries::from_csv(CSV).unwrap();
        let c = MonthlyStormClimatology::from_series(&s);
        let a = AbsoluteWay::new(4500.0).unwrap();
        let d = GridDims::new(1, 1);
        assert!(AbsoluteWay::new(0.0).is_err());
        assert!(AbsoluteWay::new(-1.0).is_err());
        assert!(AbsoluteWay::new(f64::NAN).is_err());
        assert!(AbsoluteWay::new(f64::INFINITY).is_err());
        let m = TriggerModel::default();
        assert!(
            TwoWayH0Trigger::new(d, s.clone(), c.clone(), a, "2023-06-21", 0.0, 1, m).is_err(),
            "dt_hours = 0"
        );
        assert!(
            TwoWayH0Trigger::new(d, s.clone(), c.clone(), a, "2023-06-21", f64::NAN, 1, m).is_err()
        );
        assert!(
            TwoWayH0Trigger::new(d, s.clone(), c.clone(), a, "2023-06-21", 24.0, 0, m).is_err(),
            "n_steps = 0"
        );
        assert!(TwoWayH0Trigger::new(d, s, c, a, "not-a-date", 24.0, 1, m).is_err());
    }

    #[test]
    fn climatology_needs_enough_storm_days() {
        let mut csv = String::from("fecha,h0_m,tormenta\n");
        for d in 1..=7 {
            csv.push_str(&format!("2023-06-{d:02},3000,1\n"));
        }
        let c = MonthlyStormClimatology::from_series(&ZeroIsothermSeries::from_csv(&csv).unwrap());
        assert!(c.p90(6).is_none(), "7 storm days is below MIN_STORM_DAYS");
        csv.push_str("2023-06-08,3000,1\n");
        let c = MonthlyStormClimatology::from_series(&ZeroIsothermSeries::from_csv(&csv).unwrap());
        assert!(c.p90(6).is_some(), "8 storm days defines it");
        assert_eq!(c.n_storm_days(6), 8);
        assert!(c.p90(0).is_none(), "month 0 is not a month");
        assert!(c.p90(13).is_none());
    }

    #[test]
    fn anomaly_percentile_is_a_parameter_and_p90_misses_the_winter_case() {
        // Nine June storm days whose top values straddle the witness range.
        let mut csv = String::from("fecha,h0_m,tormenta\n");
        for (d, h) in [
            (1, 2300.0), (2, 2500.0), (3, 2700.0), (4, 2900.0), (5, 3100.0),
            (6, 3300.0), (7, 3500.0), (8, 3700.0), (9, 3900.0),
        ] {
            csv.push_str(&format!("2023-06-{d:02},{h},1\n"));
        }
        let s = ZeroIsothermSeries::from_csv(&csv).unwrap();
        let p90 = MonthlyStormClimatology::from_series_at(&s, 90.0).unwrap();
        let p75 = MonthlyStormClimatology::from_series_at(&s, 75.0).unwrap();
        assert!(p75.p90(6).unwrap() < p90.p90(6).unwrap(), "lower percentile, lower bar");
        assert_eq!(p90.percentile(), 90.0);
        assert_eq!(p75.percentile(), 75.0);

        // A day at ~p80 of the month: below the p90 bar (3740), above p75 (3500)
        // — the witness case's position, reproduced synthetically.
        let day = "fecha,h0_m,tormenta\n2023-06-23,3600,1\n";
        let mk = |clim: MonthlyStormClimatology| {
            TwoWayH0Trigger::new(
                GridDims::new(1, 1),
                ZeroIsothermSeries::from_csv(day).unwrap(),
                clim,
                AbsoluteWay::new(4500.0).unwrap(),
                "2023-06-23",
                24.0,
                1,
                TriggerModel::default(),
            )
            .unwrap()
        };
        assert_eq!(mk(p90).signals(0).via, H0Via::Quiet, "p90 misses it");
        assert_eq!(mk(p75).signals(0).via, H0Via::Anomalous, "p75 catches it");

        assert!(MonthlyStormClimatology::from_series_at(&s, -1.0).is_err());
        assert!(MonthlyStormClimatology::from_series_at(&s, 101.0).is_err());
        assert!(MonthlyStormClimatology::from_series_at(&s, f64::NAN).is_err());
    }

    // --- persistence: the sustained sub-route -----------------------------

    /// Nine June storm days so the month has a climatology, then a run of days
    /// sitting between p75 and p90 — the winter witness case's position.
    fn june_series(tail: &[(u32, f64)]) -> ZeroIsothermSeries {
        let mut csv = String::from("fecha,h0_m,tormenta\n");
        for (d, h) in [
            (1, 2300.0), (2, 2500.0), (3, 2700.0), (4, 2900.0), (5, 3100.0),
            (6, 3300.0), (7, 3500.0), (8, 3700.0), (9, 3900.0),
        ] {
            csv.push_str(&format!("2023-06-{d:02},{h},1\n"));
        }
        for (d, h) in tail {
            csv.push_str(&format!("2023-06-{d:02},{h},1\n"));
        }
        ZeroIsothermSeries::from_csv(&csv).unwrap()
    }

    /// Thresholds are pinned EXPLICITLY, not derived from the same tiny series
    /// under test: with n=13 the event's own days shift the percentile and the
    /// test would be measuring the climatology instead of the run logic (which
    /// is exactly how the first version of this test fooled itself).
    fn sustained_trigger(series: ZeroIsothermSeries, min_days: usize, start: &str, n: usize)
        -> TwoWayH0Trigger
    {
        let bars = |v: f64| MonthlyStormClimatology::from_p90({
            let mut a = [None; 12];
            a[5] = Some(v); // June
            a
        });
        let spike = bars(3740.0); // ~p90
        let low = bars(3500.0);   // ~p75
        TwoWayH0Trigger::new(
            GridDims::new(1, 1), series, spike, AbsoluteWay::new(4500.0).unwrap(),
            start, 24.0, n, TriggerModel::default(),
        )
        .unwrap()
        .with_persistence(Persistence::new(low, min_days).unwrap())
    }

    #[test]
    fn sustained_route_fires_only_once_the_run_is_long_enough() {
        // 20-23 jun all sit above p75 (3500) but below p90 (3740).
        let s = june_series(&[(20, 3600.0), (21, 3620.0), (22, 3610.0), (23, 3630.0)]);
        let t = sustained_trigger(s, 3, "2023-06-20", 4);
        let via: Vec<_> = (0..4).map(|i| t.signals(i).via).collect();
        let runs: Vec<_> = (0..4).map(|i| t.signals(i).run_days).collect();
        assert_eq!(runs, vec![1, 2, 3, 4], "run must build day by day");
        assert_eq!(
            via,
            vec![H0Via::Quiet, H0Via::Quiet, H0Via::Anomalous, H0Via::Anomalous],
            "fires on the 3rd day, not before"
        );
        // and the spike route never fired on its own
        assert!((0..4).all(|i| t.signals(i).anomaly_spike.unwrap() < 0.5));
        assert!(t.signals(1).anomaly_sustained.is_none(), "still building");
        assert!(t.signals(2).anomaly_sustained.is_some(), "now it speaks");
    }

    #[test]
    fn a_gap_breaks_the_run() {
        // 21-jun missing: the streak restarts, so day 23 is only its 2nd day.
        let s = june_series(&[(20, 3600.0), (22, 3610.0), (23, 3630.0)]);
        let t = sustained_trigger(s, 3, "2023-06-20", 4);
        assert_eq!(t.signals(0).run_days, 1);
        assert_eq!(t.signals(1).run_days, 0, "the gap itself");
        assert_eq!(t.signals(1).via, H0Via::Missing);
        assert_eq!(t.signals(2).run_days, 1, "run restarted after the gap");
        assert_eq!(t.signals(3).run_days, 2);
        assert_eq!(t.signals(3).via, H0Via::Quiet, "never reached 3 observed days");
    }

    #[test]
    fn a_day_below_the_bar_breaks_the_run() {
        let s = june_series(&[(20, 3600.0), (21, 2000.0), (22, 3610.0), (23, 3630.0)]);
        let t = sustained_trigger(s, 3, "2023-06-20", 4);
        assert_eq!(t.signals(1).run_days, 0, "2000 m is below p75");
        assert_eq!(t.signals(3).run_days, 2);
    }

    #[test]
    fn the_run_never_reads_the_future() {
        // Identical history, but one series continues past the step. The verdict
        // at the shared day must be the same: runs look backwards only.
        let short = june_series(&[(20, 3600.0), (21, 3620.0)]);
        let long = june_series(&[(20, 3600.0), (21, 3620.0), (22, 3610.0), (23, 3630.0)]);
        let a = sustained_trigger(short, 3, "2023-06-21", 1);
        let b = sustained_trigger(long, 3, "2023-06-21", 1);
        assert_eq!(a.signals(0).run_days, b.signals(0).run_days);
        assert_eq!(a.signals(0).via, b.signals(0).via);
        assert_eq!(a.signals(0).via, H0Via::Quiet, "2 days is not yet 3");
    }

    #[test]
    fn persistence_adds_a_route_without_lowering_the_spike_bar() {
        // A lone day at p80: above p75, below p90. It must NOT fire — that is
        // the whole point of adding a route instead of dropping the threshold.
        let s = june_series(&[(20, 3600.0)]);
        let t = sustained_trigger(s, 3, "2023-06-20", 1);
        assert_eq!(t.signals(0).via, H0Via::Quiet);
        assert_eq!(t.signals(0).run_days, 1);
    }

    #[test]
    fn spike_still_fires_alone_with_persistence_configured() {
        let s = june_series(&[(20, 4000.0)]); // above p90 (3740)
        let t = sustained_trigger(s, 3, "2023-06-20", 1);
        let sig = t.signals(0);
        assert_eq!(sig.via, H0Via::Anomalous);
        assert!(sig.anomaly_spike.unwrap() > 0.5);
        assert!(sig.anomaly_sustained.is_none(), "run of 1 < min_days");
    }

    #[test]
    fn persistence_rejects_degenerate_configuration() {
        let s = june_series(&[]);
        let low = MonthlyStormClimatology::from_series_at(&s, 75.0).unwrap();
        assert!(Persistence::new(low.clone(), 1).is_err(), "1-day run is a spike");
        assert!(Persistence::new(low.clone(), 0).is_err());
        assert!(Persistence::new(low, 400).is_err(), "beyond MAX_RUN_DAYS");
    }

    #[test]
    fn without_persistence_nothing_changes() {
        let s = june_series(&[(20, 3600.0), (21, 3620.0), (22, 3610.0), (23, 3630.0)]);
        let spike = MonthlyStormClimatology::from_series_at(&s, 90.0).unwrap();
        let t = TwoWayH0Trigger::new(
            GridDims::new(1, 1), s, spike, AbsoluteWay::new(4500.0).unwrap(),
            "2023-06-20", 24.0, 4, TriggerModel::default(),
        )
        .unwrap();
        assert!((0..4).all(|i| t.signals(i).via == H0Via::Quiet));
        assert!((0..4).all(|i| t.signals(i).run_days == 0));
        assert!((0..4).all(|i| t.signals(i).anomaly_sustained.is_none()));
    }

    #[test]
    fn non_storm_days_do_not_enter_the_climatology() {
        let mut csv = String::from("fecha,h0_m,tormenta\n");
        for d in 1..=8 {
            csv.push_str(&format!("2023-06-{d:02},3000,1\n"));
        }
        for d in 9..=20 {
            csv.push_str(&format!("2023-06-{d:02},9000,0\n")); // dry, absurdly high
        }
        let c = MonthlyStormClimatology::from_series(&ZeroIsothermSeries::from_csv(&csv).unwrap());
        assert_eq!(c.p90(6), Some(3000.0), "dry days must not inflate the p90");
        assert_eq!(c.n_storm_days(6), 8);
    }

    #[test]
    fn civil_round_trip_every_day_across_leap_years() {
        for z in days_from_civil(1999, 12, 25)..days_from_civil(2101, 3, 5) {
            let (y, m, d) = civil_from_days(z);
            assert_eq!(days_from_civil(y, m, d), z, "{y}-{m}-{d}");
        }
    }

    #[test]
    fn last_date_is_the_newest_present_day() {
        let s = ZeroIsothermSeries::from_csv(CSV).unwrap(); // rows out of order
        assert_eq!(s.last_date().as_deref(), Some("2023-06-23"));
        assert_eq!(ZeroIsothermSeries::default().last_date(), None);
    }

    #[test]
    fn start_hour_moves_evening_steps_to_the_next_day() {
        // 2023-06-21 present, 2023-06-22 a gap: which day a step lands on is
        // visible as present-vs-missing.
        let s = ZeroIsothermSeries::from_csv(CSV).unwrap();
        let clim = MonthlyStormClimatology::from_series(&s);
        let t = TwoWayH0Trigger::new(
            GridDims::new(1, 1), s, clim, AbsoluteWay::new(4500.0).unwrap(),
            "2023-06-21", 0.5, 48, TriggerModel::default(),
        )
        .unwrap();
        // From midnight, step 20 (10:00) is still the 21st.
        assert!(t.signals(20).h0_m.is_some());
        // From 14:00, step 19 is 23:30 of the 21st and step 20 is 00:00 of the 22nd.
        let t = t.with_start_hour(14.0).unwrap();
        assert!(t.signals(19).h0_m.is_some());
        assert_eq!(t.signals(20).via, H0Via::Missing);
    }

    #[test]
    fn start_hour_rejects_hostile_values() {
        let s = ZeroIsothermSeries::from_csv(CSV).unwrap();
        let clim = MonthlyStormClimatology::from_series(&s);
        let t = TwoWayH0Trigger::new(
            GridDims::new(1, 1), s, clim, AbsoluteWay::new(4500.0).unwrap(),
            "2023-06-21", 0.5, 4, TriggerModel::default(),
        )
        .unwrap();
        for h in [-0.5, 24.0, f64::NAN, f64::INFINITY] {
            assert!(t.clone().with_start_hour(h).is_err(), "{h}");
        }
        assert!(t.with_start_hour(23.5).is_ok());
    }

    #[test]
    fn via_labels_are_stable() {
        let all = [H0Via::Missing, H0Via::Quiet, H0Via::Absolute, H0Via::Anomalous, H0Via::Both];
        let labels: Vec<_> = all.iter().map(|v| v.as_str()).collect();
        assert_eq!(labels, ["missing", "quiet", "absolute", "anomalous", "both"]);
    }
}
