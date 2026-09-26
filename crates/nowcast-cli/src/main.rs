//! `nowcast` — command-line runner for the geohazard nowcasting engine.
//!
//! Turns the library (and its examples) into an operational tool with five
//! verbs that reuse the same core machinery:
//!
//!   nowcast run        susceptibility × rainfall → per-step hazard + alerts
//!   nowcast backtest   rainfall + dated inventory → POD/FAR/CSI (with sweep)
//!   nowcast explain    exact closed-form attribution of one cell/step
//!   nowcast watch      stream a rainfall CSV through the real-time engine
//!   nowcast calibrate  fit an isotonic index → probability map, persist as JSON
//!
//! Susceptibility enters as a GeoTIFF (georeferenced output) or as a uniform
//! value (quick tests). Rainfall enters as a single-gauge CSV series (broadcast
//! over the grid) or as a stack of per-step GeoTIFFs (distributed forcing).
//! When the susceptibility is a raster and the rain is a gauge CSV, the raster
//! fixes the grid — no manual `--ncols/--nrows` needed. The core stays
//! I/O-free; all CSV parsing is the core's (one shared, finite-only parser) and
//! all GeoTIFF handling goes through `nowcast-surtgis` (native, no GDAL).
//!
//! Scriptability: every verb takes `--format json` (stable, machine-parseable
//! output; `watch` emits JSON Lines). Exit codes: 0 = ran quiet, 1 = error,
//! 2 = `run`/`watch` raised at least one alert — so `nowcast watch … && …`
//! distinguishes "ran and alerted" from "ran quiet".

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result, bail};
use clap::{Args, Parser, Subcommand, ValueEnum};

use nowcast_core::{
    Calibrator, Combine, Forcing, GridDims, HazardField, IdThreshold, IdTrigger, LiveNowcast,
    MultiNowcast, Nowcast, SusceptibilityMap, Trigger, TriggerModel, UniformRain, csv_column,
    csv_events, csv_month_keys, csv_pairs, monthly_contingency, reliability,
};
use nowcast_h0::{
    AbsoluteWay, H0Signals, MonthlyStormClimatology, Persistence, TwoWayH0Trigger,
    ZeroIsothermSeries,
};
use nowcast_surtgis::{
    Georef, Raster, gridded_rain_from_rasters, read_susceptibility, same_grid,
    write_hazard_geotiff,
};
use surtgis_core::io::read_geotiff;

/// Output format shared by all verbs.
#[derive(Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Format {
    /// Human-readable tables (the default).
    Table,
    /// Machine-parseable JSON (`watch` emits one JSON object per line).
    Json,
}

#[derive(Parser)]
#[command(
    name = "nowcast",
    about = "Dynamic geohazard nowcasting: susceptibility × dynamic trigger.",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the nowcast over a forcing series and write hazard fields + alerts.
    Run(RunArgs),
    /// Backtest the I–D trigger against a dated event inventory (uniform forcing).
    Backtest(BacktestArgs),
    /// Explain one cell/step: exact terrain × trigger attribution.
    Explain(ExplainArgs),
    /// Stream a rainfall CSV through the real-time engine, alerting step by step.
    Watch(WatchArgs),
    /// Fit an isotonic calibrator (raw index → probability) on (score, outcome)
    /// pairs and persist it as JSON for `run`/`watch --calibrator`.
    Calibrate(CalibrateArgs),
}

/// Threshold and trigger parameters shared by `run` and `explain`.
#[derive(Args, Clone)]
struct EngineArgs {
    /// I–D intercept a in I = a·D^-b (mm/h). Default: Caine (1980) global.
    #[arg(long, default_value_t = 14.82)]
    id_a: f64,
    /// I–D duration exponent b.
    #[arg(long, default_value_t = 0.39)]
    id_b: f64,
    /// Logistic trigger steepness k.
    #[arg(long, default_value_t = 4.0)]
    k: f64,
    /// Maximum rolling I–D window (steps).
    #[arg(long, default_value_t = 7)]
    max_window: usize,
}

/// Susceptibility source: a GeoTIFF, or a uniform value over an explicit grid.
#[derive(Args, Clone)]
struct SuscArgs {
    /// Susceptibility GeoTIFF in [0,1] (gives georeferenced output).
    #[arg(long)]
    susc: Option<PathBuf>,
    /// Uniform susceptibility value (alternative to --susc).
    #[arg(long)]
    uniform_susc: Option<f64>,
}

/// Rainfall source: a single-gauge CSV column, or a stack of per-step GeoTIFFs.
#[derive(Args, Clone)]
struct RainArgs {
    /// Single-gauge rainfall CSV (one record per step), broadcast over the grid.
    #[arg(long)]
    rain_csv: Option<PathBuf>,
    /// 0-based column holding the rainfall depth (mm) in --rain-csv.
    #[arg(long, default_value_t = 1)]
    rain_col: usize,
    /// Per-step precipitation GeoTIFFs (mm), in chronological order.
    #[arg(long, num_args = 1.., value_delimiter = ',')]
    rain_rasters: Vec<PathBuf>,
    /// Step length in hours.
    #[arg(long, default_value_t = 24.0)]
    dt_hours: f64,
    /// Grid columns for --uniform-susc with a CSV gauge (a --susc raster fixes
    /// the grid by itself; square if rows omitted).
    #[arg(long)]
    ncols: Option<usize>,
    /// Grid rows for --uniform-susc with a CSV gauge.
    #[arg(long)]
    nrows: Option<usize>,
}

/// Trigger-factor combination rule for fused sources (`run --fuse-rasters`).
#[derive(Clone, Copy, PartialEq, Eq, ValueEnum)]
enum CombineArg {
    /// Independent sources, any may fire: 1 − ∏(1 − fᵢ).
    NoisyOr,
    /// The strongest source wins.
    Max,
    /// All sources must concur.
    Product,
}

impl From<CombineArg> for Combine {
    fn from(c: CombineArg) -> Self {
        match c {
            CombineArg::NoisyOr => Combine::NoisyOr,
            CombineArg::Max => Combine::Max,
            CombineArg::Product => Combine::Product,
        }
    }
}

#[derive(Args)]
struct RunArgs {
    #[command(flatten)]
    susc: SuscArgs,
    #[command(flatten)]
    rain: RainArgs,
    #[command(flatten)]
    engine: EngineArgs,
    /// Fuse an EXTRA per-step rain-raster stack (comma-separated GeoTIFFs in
    /// chronological order, same grid, step count and --dt-hours as the
    /// primary rainfall). Repeat the flag once per extra source; each stack
    /// becomes its own I–D trigger sharing the engine parameters, and factors
    /// combine per cell/step with --combine BEFORE susceptibility applies.
    /// `run` only — `watch` and `explain` stay single-source.
    #[arg(long, value_name = "TIFS", action = clap::ArgAction::Append)]
    fuse_rasters: Vec<String>,
    /// How fused trigger factors combine (requires --fuse-rasters).
    /// Default when fusing: noisy-or.
    #[arg(long, value_enum)]
    combine: Option<CombineArg>,
    #[command(flatten)]
    h0: H0Args,
    /// Alert when peak hazard ≥ this level.
    #[arg(long, default_value_t = 0.5)]
    alert_level: f64,
    /// Directory to write per-step hazard GeoTIFFs (needs --susc for georef).
    #[arg(long)]
    out_dir: Option<PathBuf>,
    /// Calibrator JSON (from `nowcast calibrate`): hazard becomes a calibrated
    /// probability before alerting and output.
    #[arg(long)]
    calibrator: Option<PathBuf>,
    /// Output format.
    #[arg(long, value_enum, default_value_t = Format::Table)]
    format: Format,
}

/// Zero-isotherm (H0) signals in SHADOW mode: reported next to every step,
/// never fused into the hazard. On the series' storm days the two-way factor
/// has median 0.32 (88 % below 0.5 in winter), so fusing it by `product`
/// would suppress most winter rain alerts; see the nowcast-h0 crate docs.
#[derive(Args)]
struct H0Args {
    /// Daily H0 series CSV (`fecha,h0_m,tormenta`, e.g. h0_centro_diaria_v3.csv).
    /// Valid only for 30–36 °S.
    #[arg(long)]
    h0_csv: Option<PathBuf>,
    /// UTC time of step 0, `YYYY-MM-DDTHH:MM` (required with --h0-csv, so each
    /// step maps to the right calendar day).
    #[arg(long)]
    start: Option<String>,
    /// Absolute-route threshold in metres, on the scale of the series
    /// (default 4500; an order-of-magnitude value, not calibrated).
    #[arg(long)]
    h0_abs_m: Option<f64>,
    /// Consecutive days for the sustained anomaly sub-route (default 3).
    #[arg(long)]
    h0_persist_days: Option<usize>,
    /// Monthly storm-day percentile for the sustained sub-route (default 75).
    #[arg(long)]
    h0_persist_pct: Option<f64>,
}

/// H0 shadow reporter: the trigger plus what the JSON header says about it.
struct H0Shadow {
    trigger: TwoWayH0Trigger,
    series: PathBuf,
    last_date: Option<String>,
    abs_m: f64,
    persist_days: usize,
    persist_pct: f64,
}

impl H0Shadow {
    fn signals_json(s: &H0Signals) -> serde_json::Value {
        serde_json::json!({
            "via": s.via.as_str(),
            "h0_m": s.h0_m,
            "absolute": s.absolute,
            "anomaly": s.anomaly,
            "anomaly_spike": s.anomaly_spike,
            "anomaly_sustained": s.anomaly_sustained,
            "run_days": s.run_days,
            "month": s.month,
            "month_p90_m": s.month_p90_m,
        })
    }

    fn header_json(&self) -> serde_json::Value {
        serde_json::json!({
            "mode": "shadow",
            "series": self.series.display().to_string(),
            "series_last_date": self.last_date,
            "abs_threshold_m": self.abs_m,
            "persistence": {"min_days": self.persist_days, "percentile": self.persist_pct},
        })
    }
}

/// `YYYY-MM-DDTHH:MM` (or with a space) → (`YYYY-MM-DD`, hour as f64). The date
/// itself is validated by nowcast-h0; this only splits off and checks the time.
fn parse_start(s: &str) -> Result<(String, f64)> {
    let s = s.trim();
    let bad = || anyhow::anyhow!("--start must be YYYY-MM-DDTHH:MM (UTC), got {s:?}");
    if s.len() != 16 || !matches!(s.as_bytes()[10], b'T' | b' ') || s.as_bytes()[13] != b':' {
        return Err(bad());
    }
    let hh: u32 = s[11..13].parse().map_err(|_| bad())?;
    let mm: u32 = s[14..16].parse().map_err(|_| bad())?;
    if hh > 23 || mm > 59 {
        return Err(bad());
    }
    Ok((s[..10].to_string(), hh as f64 + mm as f64 / 60.0))
}

fn build_h0_shadow(h: &H0Args, dims: GridDims, dt_hours: f64, n_steps: usize) -> Result<Option<H0Shadow>> {
    let Some(path) = &h.h0_csv else {
        // Contradictory flags must not pass as a silent no-op.
        if h.start.is_some() || h.h0_abs_m.is_some() || h.h0_persist_days.is_some()
            || h.h0_persist_pct.is_some()
        {
            bail!("--start / --h0-abs-m / --h0-persist-* only apply with --h0-csv");
        }
        return Ok(None);
    };
    let start = h.start.as_deref().context("--h0-csv needs --start (UTC time of step 0)")?;
    let (date, hour) = parse_start(start)?;
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("reading H0 series {}", path.display()))?;
    let series = ZeroIsothermSeries::from_csv(&text)
        .with_context(|| format!("parsing H0 series {}", path.display()))?;
    if series.is_empty() {
        bail!("H0 series {} has no usable days", path.display());
    }
    let abs_m = h.h0_abs_m.unwrap_or(4500.0);
    let persist_days = h.h0_persist_days.unwrap_or(3);
    let persist_pct = h.h0_persist_pct.unwrap_or(75.0);
    let spike = MonthlyStormClimatology::from_series(&series);
    let sustained = MonthlyStormClimatology::from_series_at(&series, persist_pct)?;
    let last_date = series.last_date();
    let trigger = TwoWayH0Trigger::new(
        dims, series, spike, AbsoluteWay::new(abs_m)?, &date, dt_hours, n_steps,
        TriggerModel::default(),
    )?
    .with_start_hour(hour)?
    .with_persistence(Persistence::new(sustained, persist_days)?);
    Ok(Some(H0Shadow {
        trigger,
        series: path.clone(),
        last_date,
        abs_m,
        persist_days,
        persist_pct,
    }))
}

#[derive(Args)]
struct BacktestArgs {
    /// Single-gauge rainfall CSV.
    #[arg(long)]
    rain_csv: PathBuf,
    /// 0-based rainfall column.
    #[arg(long, default_value_t = 1)]
    rain_col: usize,
    /// Step length in hours.
    #[arg(long, default_value_t = 24.0)]
    dt_hours: f64,
    /// Dated event inventory CSV (columns: id, year, month).
    #[arg(long)]
    events_csv: PathBuf,
    #[command(flatten)]
    engine: EngineArgs,
    /// Alert level (hazard ≥ level ⟺ on/above the curve at 0.5).
    #[arg(long, default_value_t = 0.5)]
    alert_level: f64,
    /// Month-match tolerance (absorbs inventory date noise).
    #[arg(long, default_value_t = 1)]
    tol_months: u32,
    /// Sweep the I–D intercept a as MIN:MAX:STEP and report the max-CSI value.
    #[arg(long)]
    sweep: Option<String>,
    /// Output format.
    #[arg(long, value_enum, default_value_t = Format::Table)]
    format: Format,
}

#[derive(Args)]
struct WatchArgs {
    #[command(flatten)]
    susc: SuscArgs,
    /// Single-gauge rainfall CSV streamed one row = one step.
    #[arg(long)]
    rain_csv: PathBuf,
    /// 0-based rainfall column.
    #[arg(long, default_value_t = 1)]
    rain_col: usize,
    /// Step length in hours.
    #[arg(long, default_value_t = 24.0)]
    dt_hours: f64,
    /// Grid columns for --uniform-susc (a --susc raster fixes the grid by
    /// itself; explicit values must agree with it; square if rows omitted).
    #[arg(long)]
    ncols: Option<usize>,
    /// Grid rows for --uniform-susc.
    #[arg(long)]
    nrows: Option<usize>,
    #[command(flatten)]
    engine: EngineArgs,
    /// Alert when peak hazard ≥ this level.
    #[arg(long, default_value_t = 0.5)]
    alert_level: f64,
    /// Calibrator JSON (from `nowcast calibrate`): hazard becomes a calibrated
    /// probability before alerting and output.
    #[arg(long)]
    calibrator: Option<PathBuf>,
    /// Output format (`json` emits one JSON object per step — JSON Lines).
    #[arg(long, value_enum, default_value_t = Format::Table)]
    format: Format,
}

#[derive(Args)]
struct ExplainArgs {
    #[command(flatten)]
    susc: SuscArgs,
    #[command(flatten)]
    rain: RainArgs,
    #[command(flatten)]
    engine: EngineArgs,
    /// Cell index (row-major) to explain.
    #[arg(long)]
    cell: usize,
    /// Time step to explain.
    #[arg(long)]
    step: usize,
    /// Output format.
    #[arg(long, value_enum, default_value_t = Format::Table)]
    format: Format,
}

#[derive(Args)]
struct CalibrateArgs {
    /// CSV of held-out backtest pairs: a raw-index column and a binary-outcome
    /// column (0 = no event, anything else = event).
    #[arg(long)]
    pairs_csv: PathBuf,
    /// 0-based column holding the raw hazard index / score.
    #[arg(long, default_value_t = 0)]
    score_col: usize,
    /// 0-based column holding the binary outcome.
    #[arg(long, default_value_t = 1)]
    outcome_col: usize,
    /// Reliability-diagram bins for the before/after report.
    #[arg(long, default_value_t = 10)]
    bins: usize,
    /// Write the fitted calibrator here as JSON (feeds `run/watch --calibrator`).
    #[arg(long)]
    out: Option<PathBuf>,
    /// Output format.
    #[arg(long, value_enum, default_value_t = Format::Table)]
    format: Format,
}

/// Runtime forcing (the grid it lives on is resolved in [`resolve_inputs`]).
enum ForcingKind {
    Uniform(UniformRain),
    Gridded(nowcast_core::GriddedRain),
}

fn main() -> ExitCode {
    // Each verb reports whether it raised ≥1 alert; that maps to exit code 2 so
    // shell pipelines can distinguish "ran and alerted" from "ran quiet" (0).
    // Errors keep the conventional 1 — INCLUDING argument/usage errors, which
    // clap would otherwise exit(2), indistinguishable from a real alert: an
    // operational wrapper (e.g. the monitor) invoking a stale binary with an
    // unknown flag must see a failure, not a phantom alert.
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(e) => {
            // Keep clap's behavior for help/version (successful exits).
            if e.exit_code() == 0 {
                let _ = e.print();
                return ExitCode::SUCCESS;
            }
            let _ = e.print();
            return ExitCode::FAILURE;
        }
    };
    let alerted = match cli.command {
        Command::Run(a) => cmd_run(a),
        Command::Backtest(a) => cmd_backtest(a),
        Command::Explain(a) => cmd_explain(a),
        Command::Watch(a) => cmd_watch(a),
        Command::Calibrate(a) => cmd_calibrate(a),
    };
    match alerted {
        Ok(true) => ExitCode::from(2),
        Ok(false) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("Error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

/// Load a `nowcast calibrate` JSON if given.
fn load_calibrator(path: Option<&Path>) -> Result<Option<Calibrator>> {
    match path {
        None => Ok(None),
        Some(p) => {
            let text = std::fs::read_to_string(p)
                .with_context(|| format!("reading calibrator {}", p.display()))?;
            let cal: Calibrator = serde_json::from_str(&text)
                .with_context(|| format!("parsing calibrator {}", p.display()))?;
            cal.validate()
                .with_context(|| format!("invalid calibrator {}", p.display()))?;
            Ok(Some(cal))
        }
    }
}

/// Map a hazard field through a fitted calibrator (index → probability).
fn calibrate_field(field: HazardField, cal: &Calibrator) -> HazardField {
    // Hazard probabilities are finite in [0, 1] by construction, so neither
    // the calibration (rejects non-finite scores) nor the rebuild (isotonic
    // output stays in [0, 1]) can fail here.
    let probability = cal
        .calibrate(field.probability())
        .expect("hazard probabilities are finite");
    HazardField::new(field.step, field.dims(), probability)
        .expect("calibrated probabilities stay within [0,1]")
}

/// `--alert-level` is compared against hazard probabilities in `[0, 1]`. A NaN
/// makes every `p >= level` comparison false — the alerting tool would report
/// "ran quiet" (exit 0) forever, indistinguishable from real calm — and a
/// level above 1 can never fire either. Reject both up front.
fn validate_alert_level(level: f64) -> Result<()> {
    if !level.is_finite() || !(0.0..=1.0).contains(&level) {
        bail!("--alert-level must be a probability in [0, 1], got {level}");
    }
    Ok(())
}

/// Resolve susceptibility and forcing **together**.
///
/// A gauge CSV has no grid of its own, so when the susceptibility comes from a
/// raster, the raster fixes the grid (no `--ncols/--nrows` needed; explicit
/// dimensions, when given, must agree with it). With `--uniform-susc` the grid
/// comes from `--ncols/--nrows` (1×1 by default). A GeoTIFF rain stack always
/// defines the grid itself.
fn resolve_inputs(
    susc: &SuscArgs,
    rain: &RainArgs,
) -> Result<(SusceptibilityMap, Option<Georef>, ForcingKind)> {
    match (rain.rain_csv.as_ref(), rain.rain_rasters.is_empty()) {
        (Some(_), false) => bail!("use either --rain-csv or --rain-rasters, not both"),
        (Some(csv), true) => {
            // Susceptibility first: a raster fixes the gauge's grid.
            let pre = match (susc.susc.as_ref(), susc.uniform_susc) {
                (Some(_), Some(_)) => bail!("use either --susc or --uniform-susc, not both"),
                (Some(path), None) => {
                    let (map, georef) = read_susceptibility(path)
                        .with_context(|| format!("reading susceptibility {}", path.display()))?;
                    Some((map, georef))
                }
                _ => None,
            };
            let dims = match &pre {
                Some((map, _)) => {
                    let d = map.dims();
                    if let Some(nc) = rain.ncols
                        && nc != d.ncols
                    {
                        bail!("--ncols {nc} disagrees with the susceptibility raster ({} cols)", d.ncols);
                    }
                    if let Some(nr) = rain.nrows
                        && nr != d.nrows
                    {
                        bail!("--nrows {nr} disagrees with the susceptibility raster ({} rows)", d.nrows);
                    }
                    d
                }
                None => {
                    let ncols = rain.ncols.unwrap_or(1);
                    GridDims::new(ncols, rain.nrows.unwrap_or(ncols))
                }
            };
            let text = std::fs::read_to_string(csv)
                .with_context(|| format!("reading rain CSV {}", csv.display()))?;
            let forcing = UniformRain::from_csv(&text, rain.rain_col, dims, rain.dt_hours)?;
            let (map, georef) = match pre {
                Some((map, georef)) => (map, Some(georef)),
                None => build_susceptibility(susc, dims)?,
            };
            Ok((map, georef, ForcingKind::Uniform(forcing)))
        }
        (None, false) => {
            let mut rasters = Vec::with_capacity(rain.rain_rasters.len());
            for p in &rain.rain_rasters {
                let r: Raster<f32> = read_geotiff(p, Some(0))
                    .with_context(|| format!("reading rain raster {}", p.display()))?;
                rasters.push(r);
            }
            let (forcing, rain_georef) = gridded_rain_from_rasters(&rasters, rain.dt_hours)?;
            let dims = forcing.dims();
            let (map, georef) = build_susceptibility(susc, dims)?;
            // Same dims is not enough: a same-shaped susceptibility cutout
            // from a different grid would multiply against the wrong cells
            // with no visible symptom (sister of the --fuse-rasters check).
            if let Some(gr) = &georef
                && !same_grid(gr, &rain_georef)
            {
                bail!(
                    "the susceptibility raster is on a different grid than the rain \
                     rasters (same shape, different georeference)"
                );
            }
            // The susceptibility raster's georef wins when present; otherwise
            // the rain stack's georeferences the outputs (so --uniform-susc
            // over real rain rasters can still write GeoTIFFs).
            Ok((map, georef.or(Some(rain_georef)), ForcingKind::Gridded(forcing)))
        }
        (None, true) => bail!("provide rainfall with --rain-csv or --rain-rasters"),
    }
}

/// Resolve susceptibility to match a forcing grid; return its georef if any.
fn build_susceptibility(susc: &SuscArgs, dims: GridDims) -> Result<(SusceptibilityMap, Option<Georef>)> {
    match (susc.susc.as_ref(), susc.uniform_susc) {
        (Some(_), Some(_)) => bail!("use either --susc or --uniform-susc, not both"),
        (Some(path), None) => {
            let (map, georef) = read_susceptibility(path)
                .with_context(|| format!("reading susceptibility {}", path.display()))?;
            if map.dims() != dims {
                bail!(
                    "susceptibility grid {:?} does not match forcing grid {:?}",
                    map.dims(),
                    dims
                );
            }
            Ok((map, Some(georef)))
        }
        (None, Some(v)) => Ok((SusceptibilityMap::uniform(dims, v)?, None)),
        (None, None) => bail!("provide susceptibility with --susc or --uniform-susc"),
    }
}

fn make_nowcast<F: Forcing>(
    susc: SusceptibilityMap,
    forcing: F,
    e: &EngineArgs,
) -> Result<Nowcast<F>> {
    Ok(Nowcast::new(
        susc,
        forcing,
        IdThreshold::new(e.id_a, e.id_b)?,
        TriggerModel::new(e.k)?,
        e.max_window,
    )?)
}

fn cmd_run(a: RunArgs) -> Result<bool> {
    validate_alert_level(a.alert_level)?;
    if a.combine.is_some() && a.fuse_rasters.is_empty() {
        // Accepting the flag as a silent no-op would be the `watch --ncols`
        // class of bug: contradictory arguments must not pass unremarked.
        bail!("--combine only applies when fusing extra sources with --fuse-rasters");
    }
    let (susc, mut georef, forcing) = resolve_inputs(&a.susc, &a.rain)?;
    // Built BEFORE the engine runs: a bad --h0-* flag must fail fast, not after
    // a full run whose output is then thrown away.
    let n_steps = match &forcing {
        ForcingKind::Uniform(f) => f.n_steps(),
        ForcingKind::Gridded(f) => f.n_steps(),
    };
    let h0 = build_h0_shadow(&a.h0, susc.dims(), a.rain.dt_hours, n_steps)?;
    let mut fields = if a.fuse_rasters.is_empty() {
        match forcing {
            ForcingKind::Uniform(f) => make_nowcast(susc, f, &a.engine)?.run(),
            ForcingKind::Gridded(f) => make_nowcast(susc, f, &a.engine)?.run(),
        }
    } else {
        let combine = Combine::from(a.combine.unwrap_or(CombineArg::NoisyOr));
        let threshold = IdThreshold::new(a.engine.id_a, a.engine.id_b)?;
        let model = TriggerModel::new(a.engine.k)?;
        let mut triggers: Vec<Box<dyn Trigger>> = Vec::with_capacity(1 + a.fuse_rasters.len());
        triggers.push(match forcing {
            ForcingKind::Uniform(f) => {
                Box::new(IdTrigger::new(f, threshold, model, a.engine.max_window)?)
            }
            ForcingKind::Gridded(f) => {
                Box::new(IdTrigger::new(f, threshold, model, a.engine.max_window)?)
            }
        });
        for (i, stack) in a.fuse_rasters.iter().enumerate() {
            let mut rasters = Vec::new();
            for p in stack.split(',').filter(|s| !s.is_empty()) {
                let r: Raster<f32> = read_geotiff(Path::new(p), Some(0))
                    .with_context(|| format!("reading fused rain raster {p}"))?;
                rasters.push(r);
            }
            let (fused, fused_georef) = gridded_rain_from_rasters(&rasters, a.rain.dt_hours)
                .with_context(|| format!("stacking fused source {}", i + 1))?;
            // Same-shaped cutouts from a different grid must not fuse: factors
            // would combine across misaligned cells with no visible symptom.
            match &georef {
                Some(gr) if !same_grid(gr, &fused_georef) => bail!(
                    "fused source {} is on a different grid than the primary inputs \
                     (same shape, different georeference)",
                    i + 1
                ),
                Some(_) => {}
                // Ungeoreferenced primary (uniform susc + gauge CSV): the first
                // fused stack's georef then georeferences outputs and anchors
                // the alignment check for later stacks.
                None => georef = Some(fused_georef),
            }
            triggers.push(Box::new(IdTrigger::new(fused, threshold, model, a.engine.max_window)?));
        }
        MultiNowcast::new(susc, triggers, combine)?.run()
    };
    if let Some(cal) = load_calibrator(a.calibrator.as_deref())? {
        fields = fields.into_iter().map(|f| calibrate_field(f, &cal)).collect();
    }
    report_run(&fields, georef.as_ref(), a.alert_level, a.out_dir.as_deref(), a.format, h0.as_ref())
}

fn report_run(
    fields: &[HazardField],
    georef: Option<&Georef>,
    alert_level: f64,
    out_dir: Option<&Path>,
    format: Format,
    h0: Option<&H0Shadow>,
) -> Result<bool> {
    if let Some(dir) = out_dir {
        if georef.is_none() {
            bail!("--out-dir needs a georeferenced --susc to write GeoTIFFs");
        }
        std::fs::create_dir_all(dir)
            .with_context(|| format!("creating output directory {}", dir.display()))?;
    }

    if format == Format::Table {
        println!("step  alert  n_cells  fraction  max_prob{}", if h0.is_some() { "  h0 (shadow)" } else { "" });
    }
    let mut n_alert = 0usize;
    let mut steps_json = Vec::new();
    for f in fields {
        let alert = f.alert(alert_level)?; // level pre-validated; cheap backstop
        let fired = alert.is_some();
        if fired {
            n_alert += 1;
        }
        match format {
            Format::Table => {
                let (n_cells, fraction) =
                    alert.map(|al| (al.n_cells, al.fraction)).unwrap_or((0, 0.0));
                let h0_col = h0
                    .map(|h| {
                        let s = h.trigger.signals(f.step);
                        match s.h0_m {
                            Some(m) => format!("  {} {m:.0} m", s.via.as_str()),
                            None => "  missing".to_string(),
                        }
                    })
                    .unwrap_or_default();
                println!(
                    "{:>4}  {:>5}  {:>7}  {:>7.3}  {:>7.3}{h0_col}",
                    f.step,
                    if fired { "  *" } else { "  ." },
                    n_cells,
                    fraction,
                    f.max_probability(),
                );
            }
            Format::Json => {
                let mut step = serde_json::json!({
                    "step": f.step,
                    "max_probability": f.max_probability(),
                    "alert": alert,
                });
                if let Some(h) = h0 {
                    step["h0"] = H0Shadow::signals_json(&h.trigger.signals(f.step));
                }
                steps_json.push(step)
            }
        }
        if let (Some(dir), Some(gr)) = (out_dir, georef) {
            let path = dir.join(format!("hazard_{:04}.tif", f.step));
            write_hazard_geotiff(f, gr, &path)
                .with_context(|| format!("writing {}", path.display()))?;
        }
    }
    match format {
        Format::Table => println!(
            "\n{} step(s), {} with an alert at level {:.2}{}",
            fields.len(),
            n_alert,
            alert_level,
            match out_dir {
                Some(d) => format!("; hazard GeoTIFFs written to {}", d.display()),
                None => String::new(),
            }
        ),
        Format::Json => {
            let mut out = serde_json::json!({
                "alert_level": alert_level,
                "n_steps": fields.len(),
                "n_alerts": n_alert,
                "out_dir": out_dir.map(|d| d.display().to_string()),
                "steps": steps_json,
            });
            if let Some(h) = h0 {
                out["h0"] = h.header_json();
            }
            println!("{out}")
        }
    }
    Ok(n_alert > 0)
}

fn cmd_explain(a: ExplainArgs) -> Result<bool> {
    let (susc, _, forcing) = resolve_inputs(&a.susc, &a.rain)?;
    let e = a.engine.clone();
    let explanation = match forcing {
        ForcingKind::Uniform(f) => make_nowcast(susc, f, &e)?.explain(a.cell, a.step)?,
        ForcingKind::Gridded(f) => make_nowcast(susc, f, &e)?.explain(a.cell, a.step)?,
    };
    let x = explanation;
    match a.format {
        Format::Json => println!("{}", serde_json::to_string_pretty(&x)?),
        Format::Table => {
            println!("Attribution for cell {} at step {}", x.cell, x.step);
            println!("  hazard            {:.4}", x.hazard);
            println!("  susceptibility    {:.4}  (terrain)", x.susceptibility);
            println!("  trigger factor    {:.4}  (climate)", x.trigger_factor);
            println!(
                "  dominant I–D window  D={:.1} h  mean I={:.3} mm/h  vs I_crit={:.3} mm/h  (E={:.3})",
                x.critical_duration_h, x.mean_intensity_mm_h, x.critical_intensity_mm_h, x.exceedance
            );
            println!("  driver            {:?}", x.driver);
        }
    }
    Ok(false)
}

fn cmd_watch(a: WatchArgs) -> Result<bool> {
    validate_alert_level(a.alert_level)?;
    // Resolve susceptibility and gauge through the same path as `run`, so both
    // verbs validate identically (`watch` used to ignore --ncols/--nrows that
    // contradicted a --susc raster, where `run` rejects them).
    let rain = RainArgs {
        rain_csv: Some(a.rain_csv.clone()),
        rain_col: a.rain_col,
        rain_rasters: Vec::new(),
        dt_hours: a.dt_hours,
        ncols: a.ncols,
        nrows: a.nrows,
    };
    let (susc, _georef, forcing) = resolve_inputs(&a.susc, &rain)?;
    let ForcingKind::Uniform(gauge) = forcing else {
        unreachable!("watch has no --rain-rasters flag");
    };
    let depths: Vec<f64> = (0..gauge.n_steps()).map(|t| gauge.depth_mm(0, t)).collect();
    let calibrator = load_calibrator(a.calibrator.as_deref())?;
    let dims = susc.dims();
    let n = dims.len();
    let mut engine = LiveNowcast::new(
        susc,
        IdThreshold::new(a.engine.id_a, a.engine.id_b)?,
        TriggerModel::new(a.engine.k)?,
        a.engine.max_window,
        a.dt_hours,
    )?;

    if a.format == Format::Table {
        println!(
            "Live nowcast over {} ({} cells), alert ≥ {:.2}:",
            a.rain_csv.display(),
            n,
            a.alert_level
        );
        println!("step  rain(mm)  peak_hazard  status");
    }
    let mut buf = vec![0.0; n];
    let mut n_alert = 0;
    for (t, &rain) in depths.iter().enumerate() {
        buf.fill(rain); // one gauge broadcast over the grid this step
        let mut field = engine.push(&buf)?;
        if let Some(cal) = &calibrator {
            field = calibrate_field(field, cal);
        }
        let alert = field.alert(a.alert_level)?; // level pre-validated
        if alert.is_some() {
            n_alert += 1;
        }
        match a.format {
            // JSON Lines: one object per step, parseable as the stream arrives.
            Format::Json => println!(
                "{}",
                serde_json::json!({
                    "step": t,
                    "rain_mm": rain,
                    "max_probability": field.max_probability(),
                    "alert": alert,
                })
            ),
            Format::Table => {
                let status = match alert {
                    Some(al) => format!(
                        "ALERT — {} cell(s), {:.0}% of grid",
                        al.n_cells,
                        100.0 * al.fraction
                    ),
                    None => "quiet".to_string(),
                };
                println!("{t:>4}  {rain:>8.1}  {:>11.3}  {status}", field.max_probability());
            }
        }
    }
    if a.format == Format::Table {
        println!("\n{} of {} steps raised an alert.", n_alert, depths.len());
    }
    Ok(n_alert > 0)
}

fn cmd_backtest(a: BacktestArgs) -> Result<bool> {
    validate_alert_level(a.alert_level)?;
    let text = std::fs::read_to_string(&a.rain_csv)
        .with_context(|| format!("reading rain CSV {}", a.rain_csv.display()))?;
    // A single representative gauge: 1×1 grid, susceptibility held at 1.0 so the
    // hazard reduces to the trigger factor (timing test against the inventory).
    let dims = GridDims::new(1, 1);
    let depths = csv_column(&text, a.rain_col);
    if depths.is_empty() {
        bail!("no numeric values in column {} of {}", a.rain_col, a.rain_csv.display());
    }
    let day_month = csv_month_keys(&text);
    if day_month.len() != depths.len() {
        bail!(
            "rain CSV has {} dated rows but {} numeric depths; need a date column 0 (YYYY-MM-...) and depth column {}",
            day_month.len(),
            depths.len(),
            a.rain_col
        );
    }
    let events_text = std::fs::read_to_string(&a.events_csv)
        .with_context(|| format!("reading events CSV {}", a.events_csv.display()))?;
    let events = csv_events(&events_text);

    let alerts_for = |id_a: f64| -> Result<Vec<bool>> {
        let forcing = UniformRain::new(dims, a.dt_hours, depths.clone())?;
        let susc = SusceptibilityMap::uniform(dims, 1.0)?;
        let nc = make_nowcast(susc, forcing, &EngineArgs { id_a, ..a.engine.clone() })?;
        Ok(nc.run().into_iter().map(|f| f.max_probability() >= a.alert_level).collect())
    };
    let scores = |c: &nowcast_core::Contingency| {
        serde_json::json!({
            "contingency": c,
            "pod": c.pod(), "far": c.far(), "csi": c.csi(), "bias": c.frequency_bias(),
        })
    };

    if a.format == Format::Table {
        println!(
            "Backtest: {} steps, {} events; I = a·D^-{:.2}, ±{} month match\n",
            depths.len(),
            events.len(),
            a.engine.id_b,
            a.tol_months
        );
    }

    if let Some(spec) = &a.sweep {
        let (lo, hi, step) = parse_sweep(spec)?;
        let mut best = (f64::NAN, -1.0_f64);
        let mut rows_json = Vec::new();
        let mut v = lo;
        if a.format == Format::Table {
            println!("   a     POD    FAR    CSI    bias");
        }
        while v <= hi + 1e-9 {
            let alerts = alerts_for(v)?;
            let c = monthly_contingency(&day_month, &alerts, &events, a.tol_months)?;
            let csi = c.csi().unwrap_or(0.0);
            match a.format {
                Format::Table => println!(
                    "{:>5.1}  {}  {}  {}  {}",
                    v,
                    fmt(c.pod()),
                    fmt(c.far()),
                    fmt(c.csi()),
                    fmt(c.frequency_bias())
                ),
                Format::Json => {
                    let mut row = scores(&c);
                    row["a"] = serde_json::json!(v);
                    rows_json.push(row);
                }
            }
            if csi > best.1 {
                best = (v, csi);
            }
            v += step;
        }
        match a.format {
            Format::Table => println!("\nmax-CSI intercept a* = {:.1} (CSI {:.3})", best.0, best.1),
            Format::Json => println!(
                "{}",
                serde_json::json!({
                    "n_steps": depths.len(), "n_events": events.len(),
                    "b": a.engine.id_b, "tol_months": a.tol_months,
                    "sweep": rows_json,
                    "best": {"a": best.0, "csi": best.1},
                })
            ),
        }
    } else {
        let alerts = alerts_for(a.engine.id_a)?;
        let c = monthly_contingency(&day_month, &alerts, &events, a.tol_months)?;
        match a.format {
            Format::Table => println!(
                "a = {:.2}:  POD {}  FAR {}  CSI {}  bias {}",
                a.engine.id_a,
                fmt(c.pod()),
                fmt(c.far()),
                fmt(c.csi()),
                fmt(c.frequency_bias())
            ),
            Format::Json => {
                let mut out = scores(&c);
                out["a"] = serde_json::json!(a.engine.id_a);
                out["b"] = serde_json::json!(a.engine.id_b);
                out["tol_months"] = serde_json::json!(a.tol_months);
                out["n_steps"] = serde_json::json!(depths.len());
                out["n_events"] = serde_json::json!(events.len());
                println!("{out}");
            }
        }
    }
    Ok(false)
}

/// Fit an isotonic calibrator on (score, outcome) pairs, report the before /
/// after reliability, and optionally persist the calibrator as JSON.
fn cmd_calibrate(a: CalibrateArgs) -> Result<bool> {
    let text = std::fs::read_to_string(&a.pairs_csv)
        .with_context(|| format!("reading pairs CSV {}", a.pairs_csv.display()))?;
    // Row-wise pair parser: two independent column passes used to pair values
    // from *different rows* silently when parse failures crossed with equal
    // counts — poisoning the fitted calibrator without any warning.
    let pairs = csv_pairs(&text, a.score_col, a.outcome_col)
        .with_context(|| format!("parsing pairs CSV {}", a.pairs_csv.display()))?;
    if pairs.is_empty() {
        bail!(
            "no (score, outcome) pairs in columns {} and {} of {}",
            a.score_col,
            a.outcome_col,
            a.pairs_csv.display()
        );
    }
    let (scores, outcomes): (Vec<f64>, Vec<bool>) =
        pairs.into_iter().map(|(s, o)| (s, o != 0.0)).unzip();

    let cal = Calibrator::fit_isotonic(&scores, &outcomes)?;
    // The "before" reliability treats the raw score as a probability, which
    // only makes sense when it already lives in [0, 1] (the engine's hazard
    // index does). Arbitrary external scores still calibrate fine — only the
    // before-diagram is skipped.
    let before = if scores.iter().all(|s| (0.0..=1.0).contains(s)) {
        Some(reliability(&scores, &outcomes, a.bins)?)
    } else {
        None
    };
    let after = reliability(&cal.calibrate(&scores)?, &outcomes, a.bins)?;

    match a.format {
        Format::Table => {
            println!(
                "Isotonic calibration on {} pairs (base rate {:.3}):\n",
                scores.len(),
                after.base_rate
            );
            println!("               Brier     skill      ECE");
            match &before {
                Some(b) => println!("  raw index  {:.4}   {:>+.3}   {:.4}", b.brier, b.brier_skill, b.ece),
                None => println!("  raw index    (scores outside [0, 1]: not a probability, before-diagram skipped)"),
            }
            println!("  calibrated {:.4}   {:>+.3}   {:.4}", after.brier, after.brier_skill, after.ece);
        }
        Format::Json => println!(
            "{}",
            serde_json::json!({
                "n_pairs": scores.len(),
                "base_rate": after.base_rate,
                "before": before.as_ref().map(|b| serde_json::json!({
                    "brier": b.brier, "brier_skill": b.brier_skill, "ece": b.ece
                })),
                "after": {"brier": after.brier, "brier_skill": after.brier_skill, "ece": after.ece},
                "out": a.out.as_ref().map(|p| p.display().to_string()),
            })
        ),
    }

    if let Some(path) = &a.out {
        let json = serde_json::to_string_pretty(&cal)?;
        std::fs::write(path, json)
            .with_context(|| format!("writing calibrator {}", path.display()))?;
        if a.format == Format::Table {
            println!("\nCalibrator written to {} (use with run/watch --calibrator).", path.display());
        }
    }
    Ok(false)
}

fn parse_sweep(spec: &str) -> Result<(f64, f64, f64)> {
    let p: Vec<&str> = spec.split(':').collect();
    if p.len() != 3 {
        bail!("--sweep must be MIN:MAX:STEP (e.g. 2:16:0.5)");
    }
    let lo: f64 = p[0].parse().context("sweep MIN")?;
    let hi: f64 = p[1].parse().context("sweep MAX")?;
    let step: f64 = p[2].parse().context("sweep STEP")?;
    if !lo.is_finite() || !hi.is_finite() || !step.is_finite() {
        bail!("--sweep values must be finite numbers");
    }
    // The sweep varies the I–D intercept a, which must be positive — starting
    // at 0 used to abort the whole sweep on its first value.
    if lo <= 0.0 {
        bail!("--sweep MIN must be > 0 (it sweeps the I–D intercept a)");
    }
    if step <= 0.0 || hi < lo {
        bail!("--sweep needs STEP > 0 and MAX ≥ MIN");
    }
    let runs = ((hi - lo) / step).floor() + 1.0;
    if runs > 100_000.0 {
        bail!("--sweep spans {runs:.0} backtest runs; use a coarser STEP");
    }
    Ok((lo, hi, step))
}

fn fmt(x: Option<f64>) -> String {
    x.map(|v| format!("{v:.2}")).unwrap_or_else(|| " -  ".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_start_accepts_t_or_space_and_rejects_the_rest() {
        assert_eq!(parse_start("2023-06-21T14:30").unwrap(), ("2023-06-21".into(), 14.5));
        assert_eq!(parse_start("2023-06-21 00:00").unwrap(), ("2023-06-21".into(), 0.0));
        for bad in ["2023-06-21", "2023-06-21T24:00", "2023-06-21T12:60", "2023-06-21X00:00",
                    "2023-06-21T1:00", "2023-06-21T12-00", "2023-06-21T12:00Z", ""] {
            assert!(parse_start(bad).is_err(), "{bad:?}");
        }
    }

    fn no_h0() -> H0Args {
        H0Args { h0_csv: None, start: None, h0_abs_m: None, h0_persist_days: None, h0_persist_pct: None }
    }

    #[test]
    fn h0_is_off_without_csv_and_stray_flags_are_errors() {
        let dims = GridDims::new(1, 1);
        assert!(build_h0_shadow(&no_h0(), dims, 24.0, 3).unwrap().is_none());
        let strays = [
            H0Args { start: Some("2023-06-21T00:00".into()), ..no_h0() },
            H0Args { h0_abs_m: Some(4000.0), ..no_h0() },
            H0Args { h0_persist_days: Some(3), ..no_h0() },
            H0Args { h0_persist_pct: Some(75.0), ..no_h0() },
        ];
        for a in &strays {
            assert!(build_h0_shadow(a, dims, 24.0, 3).is_err());
        }
    }

    #[test]
    fn h0_csv_without_start_is_an_error() {
        let a = H0Args { h0_csv: Some("/does/not/matter.csv".into()), ..no_h0() };
        let e = build_h0_shadow(&a, GridDims::new(1, 1), 24.0, 3).err().unwrap();
        assert!(e.to_string().contains("--start"), "{e}");
    }
}
