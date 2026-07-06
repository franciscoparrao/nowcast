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
    Calibrator, Forcing, GridDims, HazardField, IdThreshold, LiveNowcast, Nowcast,
    SusceptibilityMap, TriggerModel, UniformRain, csv_column, csv_events, csv_month_keys,
    monthly_contingency, reliability,
};
use nowcast_surtgis::{Georef, Raster, gridded_rain_from_rasters, read_susceptibility, write_hazard_geotiff};
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

#[derive(Args)]
struct RunArgs {
    #[command(flatten)]
    susc: SuscArgs,
    #[command(flatten)]
    rain: RainArgs,
    #[command(flatten)]
    engine: EngineArgs,
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
    /// Grid columns/rows for uniform susceptibility (square if rows omitted).
    #[arg(long)]
    ncols: Option<usize>,
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
    // Errors keep the conventional 1.
    let alerted = match Cli::parse().command {
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
    let probability = cal.calibrate(field.probability());
    // Isotonic output is clamped to [0, 1] by construction.
    HazardField::new(field.step, field.dims(), probability)
        .expect("calibrated probabilities stay within [0,1]")
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
            let forcing = gridded_rain_from_rasters(&rasters, rain.dt_hours)?;
            let dims = forcing.dims();
            let (map, georef) = build_susceptibility(susc, dims)?;
            Ok((map, georef, ForcingKind::Gridded(forcing)))
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
    let (susc, georef, forcing) = resolve_inputs(&a.susc, &a.rain)?;
    let mut fields = match forcing {
        ForcingKind::Uniform(f) => make_nowcast(susc, f, &a.engine)?.run(),
        ForcingKind::Gridded(f) => make_nowcast(susc, f, &a.engine)?.run(),
    };
    if let Some(cal) = load_calibrator(a.calibrator.as_deref())? {
        fields = fields.into_iter().map(|f| calibrate_field(f, &cal)).collect();
    }
    report_run(&fields, georef.as_ref(), a.alert_level, a.out_dir.as_deref(), a.format)
}

fn report_run(
    fields: &[HazardField],
    georef: Option<&Georef>,
    alert_level: f64,
    out_dir: Option<&Path>,
    format: Format,
) -> Result<bool> {
    if let Some(dir) = out_dir {
        if georef.is_none() {
            bail!("--out-dir needs a georeferenced --susc to write GeoTIFFs");
        }
        std::fs::create_dir_all(dir)
            .with_context(|| format!("creating output directory {}", dir.display()))?;
    }

    if format == Format::Table {
        println!("step  alert  n_cells  fraction  max_prob");
    }
    let mut n_alert = 0usize;
    let mut steps_json = Vec::new();
    for f in fields {
        let alert = f.alert(alert_level);
        let fired = alert.is_some();
        if fired {
            n_alert += 1;
        }
        match format {
            Format::Table => {
                let (n_cells, fraction) =
                    alert.map(|al| (al.n_cells, al.fraction)).unwrap_or((0, 0.0));
                println!(
                    "{:>4}  {:>5}  {:>7}  {:>7.3}  {:>7.3}",
                    f.step,
                    if fired { "  *" } else { "  ." },
                    n_cells,
                    fraction,
                    f.max_probability(),
                );
            }
            Format::Json => steps_json.push(serde_json::json!({
                "step": f.step,
                "max_probability": f.max_probability(),
                "alert": alert,
            })),
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
        Format::Json => println!(
            "{}",
            serde_json::json!({
                "alert_level": alert_level,
                "n_steps": fields.len(),
                "n_alerts": n_alert,
                "out_dir": out_dir.map(|d| d.display().to_string()),
                "steps": steps_json,
            })
        ),
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
    let text = std::fs::read_to_string(&a.rain_csv)
        .with_context(|| format!("reading rain CSV {}", a.rain_csv.display()))?;
    let depths = csv_column(&text, a.rain_col);
    if depths.is_empty() {
        bail!("no numeric values in column {} of {}", a.rain_col, a.rain_csv.display());
    }
    // Single-gauge susceptibility, broadcast over the grid.
    let susc = match (a.susc.susc.as_ref(), a.susc.uniform_susc) {
        (Some(_), Some(_)) => bail!("use either --susc or --uniform-susc, not both"),
        (Some(path), None) => {
            read_susceptibility(path)
                .with_context(|| format!("reading susceptibility {}", path.display()))?
                .0
        }
        (None, Some(v)) => {
            let ncols = a.ncols.unwrap_or(1);
            let nrows = a.nrows.unwrap_or(ncols);
            SusceptibilityMap::uniform(GridDims::new(ncols, nrows), v)?
        }
        (None, None) => bail!("provide susceptibility with --susc or --uniform-susc"),
    };
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
        let alert = field.alert(a.alert_level);
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
    let scores = csv_column(&text, a.score_col);
    let outcome_vals = csv_column(&text, a.outcome_col);
    if scores.is_empty() {
        bail!("no numeric values in column {} of {}", a.score_col, a.pairs_csv.display());
    }
    if scores.len() != outcome_vals.len() {
        bail!(
            "score column {} has {} values but outcome column {} has {} — the pairs must align row by row",
            a.score_col,
            scores.len(),
            a.outcome_col,
            outcome_vals.len()
        );
    }
    let outcomes: Vec<bool> = outcome_vals.iter().map(|&v| v != 0.0).collect();

    let cal = Calibrator::fit_isotonic(&scores, &outcomes)?;
    let before = reliability(&scores, &outcomes, a.bins)?;
    let after = reliability(&cal.calibrate(&scores), &outcomes, a.bins)?;

    match a.format {
        Format::Table => {
            println!(
                "Isotonic calibration on {} pairs (base rate {:.3}):\n",
                scores.len(),
                before.base_rate
            );
            println!("               Brier     skill      ECE");
            println!("  raw index  {:.4}   {:>+.3}   {:.4}", before.brier, before.brier_skill, before.ece);
            println!("  calibrated {:.4}   {:>+.3}   {:.4}", after.brier, after.brier_skill, after.ece);
        }
        Format::Json => println!(
            "{}",
            serde_json::json!({
                "n_pairs": scores.len(),
                "base_rate": before.base_rate,
                "before": {"brier": before.brier, "brier_skill": before.brier_skill, "ece": before.ece},
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
    if step <= 0.0 || hi < lo {
        bail!("--sweep needs STEP > 0 and MAX ≥ MIN");
    }
    Ok((lo, hi, step))
}

fn fmt(x: Option<f64>) -> String {
    x.map(|v| format!("{v:.2}")).unwrap_or_else(|| " -  ".into())
}
