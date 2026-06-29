//! `nowcast` — command-line runner for the geohazard nowcasting engine.
//!
//! Turns the library (and its examples) into an operational tool with three
//! verbs that reuse the same core machinery:
//!
//!   nowcast run        susceptibility × rainfall → per-step hazard + alerts
//!   nowcast backtest   rainfall + dated inventory → POD/FAR/CSI (with sweep)
//!   nowcast explain    exact closed-form attribution of one cell/step
//!
//! Susceptibility enters as a GeoTIFF (georeferenced output) or as a uniform
//! value (quick tests). Rainfall enters as a single-gauge CSV series (broadcast
//! over the grid) or as a stack of per-step GeoTIFFs (distributed forcing). The
//! core stays I/O-free; all GeoTIFF handling goes through `nowcast-surtgis`
//! (native, no GDAL).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::{Args, Parser, Subcommand};

use nowcast_core::{
    Forcing, GridDims, HazardField, IdThreshold, LiveNowcast, MonthKey, Nowcast, SusceptibilityMap,
    TriggerModel, UniformRain, monthly_contingency,
};
use nowcast_surtgis::{Georef, Raster, gridded_rain_from_rasters, read_susceptibility, write_hazard_geotiff};
use surtgis_core::io::read_geotiff;

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
    /// Grid columns/rows for uniform susceptibility with a CSV gauge (square if rows omitted).
    #[arg(long)]
    ncols: Option<usize>,
    /// Grid rows for uniform susceptibility with a CSV gauge.
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
}

/// Runtime forcing, plus the grid it defines.
enum ForcingKind {
    Uniform(UniformRain),
    Gridded(nowcast_core::GriddedRain),
}

impl ForcingKind {
    fn dims(&self) -> GridDims {
        match self {
            ForcingKind::Uniform(f) => f.dims(),
            ForcingKind::Gridded(f) => f.dims(),
        }
    }
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Run(a) => cmd_run(a),
        Command::Backtest(a) => cmd_backtest(a),
        Command::Explain(a) => cmd_explain(a),
        Command::Watch(a) => cmd_watch(a),
    }
}

/// Build the forcing from the rain flags (CSV gauge or GeoTIFF stack).
fn build_forcing(rain: &RainArgs) -> Result<ForcingKind> {
    match (rain.rain_csv.as_ref(), rain.rain_rasters.is_empty()) {
        (Some(_), false) => bail!("use either --rain-csv or --rain-rasters, not both"),
        (Some(csv), true) => {
            // Dims: a uniform gauge needs an explicit grid (for uniform susc) or
            // is paired with a susceptibility raster that fixes the grid later.
            let ncols = rain.ncols.unwrap_or(1);
            let nrows = rain.nrows.unwrap_or(ncols);
            let text = std::fs::read_to_string(csv)
                .with_context(|| format!("reading rain CSV {}", csv.display()))?;
            let f = UniformRain::from_csv(&text, rain.rain_col, GridDims::new(ncols, nrows), rain.dt_hours)?;
            Ok(ForcingKind::Uniform(f))
        }
        (None, false) => {
            let mut rasters = Vec::with_capacity(rain.rain_rasters.len());
            for p in &rain.rain_rasters {
                let r: Raster<f32> = read_geotiff(p, Some(0))
                    .with_context(|| format!("reading rain raster {}", p.display()))?;
                rasters.push(r);
            }
            let f = gridded_rain_from_rasters(&rasters, rain.dt_hours)?;
            Ok(ForcingKind::Gridded(f))
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

fn cmd_run(a: RunArgs) -> Result<()> {
    let forcing = build_forcing(&a.rain)?;
    let dims = forcing.dims();
    let (susc, georef) = build_susceptibility(&a.susc, dims)?;
    let fields = match forcing {
        ForcingKind::Uniform(f) => make_nowcast(susc, f, &a.engine)?.run(),
        ForcingKind::Gridded(f) => make_nowcast(susc, f, &a.engine)?.run(),
    };
    report_run(&fields, georef.as_ref(), a.alert_level, a.out_dir.as_deref())
}

fn report_run(
    fields: &[HazardField],
    georef: Option<&Georef>,
    alert_level: f64,
    out_dir: Option<&Path>,
) -> Result<()> {
    if let Some(dir) = out_dir {
        if georef.is_none() {
            bail!("--out-dir needs a georeferenced --susc to write GeoTIFFs");
        }
        std::fs::create_dir_all(dir)
            .with_context(|| format!("creating output directory {}", dir.display()))?;
    }

    println!("step  alert  n_cells  fraction  max_prob");
    let mut n_alert = 0usize;
    for f in fields {
        let alert = f.alert(alert_level);
        let fired = alert.is_some();
        if fired {
            n_alert += 1;
        }
        let (n_cells, fraction) = alert.map(|al| (al.n_cells, al.fraction)).unwrap_or((0, 0.0));
        println!(
            "{:>4}  {:>5}  {:>7}  {:>7.3}  {:>7.3}",
            f.step,
            if fired { "  *" } else { "  ." },
            n_cells,
            fraction,
            f.max_probability(),
        );
        if let (Some(dir), Some(gr)) = (out_dir, georef) {
            let path = dir.join(format!("hazard_{:04}.tif", f.step));
            write_hazard_geotiff(f, gr, &path)
                .with_context(|| format!("writing {}", path.display()))?;
        }
    }
    println!(
        "\n{} step(s), {} with an alert at level {:.2}{}",
        fields.len(),
        n_alert,
        alert_level,
        match out_dir {
            Some(d) => format!("; hazard GeoTIFFs written to {}", d.display()),
            None => String::new(),
        }
    );
    Ok(())
}

fn cmd_explain(a: ExplainArgs) -> Result<()> {
    let forcing = build_forcing(&a.rain)?;
    let dims = forcing.dims();
    let (susc, _) = build_susceptibility(&a.susc, dims)?;
    let e = a.engine.clone();
    let explanation = match forcing {
        ForcingKind::Uniform(f) => make_nowcast(susc, f, &e)?.explain(a.cell, a.step),
        ForcingKind::Gridded(f) => make_nowcast(susc, f, &e)?.explain(a.cell, a.step),
    };
    let x = explanation;
    println!("Attribution for cell {} at step {}", x.cell, x.step);
    println!("  hazard            {:.4}", x.hazard);
    println!("  susceptibility    {:.4}  (terrain)", x.susceptibility);
    println!("  trigger factor    {:.4}  (climate)", x.trigger_factor);
    println!(
        "  dominant I–D window  D={:.1} h  mean I={:.3} mm/h  vs I_crit={:.3} mm/h  (E={:.3})",
        x.critical_duration_h, x.mean_intensity_mm_h, x.critical_intensity_mm_h, x.exceedance
    );
    println!("  driver            {:?}", x.driver);
    Ok(())
}

fn cmd_watch(a: WatchArgs) -> Result<()> {
    let text = std::fs::read_to_string(&a.rain_csv)
        .with_context(|| format!("reading rain CSV {}", a.rain_csv.display()))?;
    let depths = column_values(&text, a.rain_col);
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
    let dims = susc.dims();
    let n = dims.len();
    let mut engine = LiveNowcast::new(
        susc,
        IdThreshold::new(a.engine.id_a, a.engine.id_b)?,
        TriggerModel::new(a.engine.k)?,
        a.engine.max_window,
        a.dt_hours,
    )?;

    println!(
        "Live nowcast over {} ({} cells), alert ≥ {:.2}:",
        a.rain_csv.display(),
        n,
        a.alert_level
    );
    println!("step  rain(mm)  peak_hazard  status");
    let mut buf = vec![0.0; n];
    let mut n_alert = 0;
    for (t, &rain) in depths.iter().enumerate() {
        buf.fill(rain); // one gauge broadcast over the grid this step
        let field = engine.push(&buf)?;
        let status = match field.alert(a.alert_level) {
            Some(al) => {
                n_alert += 1;
                format!("ALERT — {} cell(s), {:.0}% of grid", al.n_cells, 100.0 * al.fraction)
            }
            None => "quiet".to_string(),
        };
        println!("{t:>4}  {rain:>8.1}  {:>11.3}  {status}", field.max_probability());
    }
    println!("\n{} of {} steps raised an alert.", n_alert, depths.len());
    Ok(())
}

fn cmd_backtest(a: BacktestArgs) -> Result<()> {
    let text = std::fs::read_to_string(&a.rain_csv)
        .with_context(|| format!("reading rain CSV {}", a.rain_csv.display()))?;
    // A single representative gauge: 1×1 grid, susceptibility held at 1.0 so the
    // hazard reduces to the trigger factor (timing test against the inventory).
    let dims = GridDims::new(1, 1);
    let depths = column_values(&text, a.rain_col);
    if depths.is_empty() {
        bail!("no numeric values in column {} of {}", a.rain_col, a.rain_csv.display());
    }
    let day_month = month_keys(&text)?;
    if day_month.len() != depths.len() {
        bail!(
            "rain CSV has {} dated rows but {} numeric depths; need a date column 0 (YYYY-MM-...) and depth column {}",
            day_month.len(),
            depths.len(),
            a.rain_col
        );
    }
    let events = read_events(&a.events_csv)?;

    let alerts_for = |id_a: f64| -> Result<Vec<bool>> {
        let forcing = UniformRain::new(dims, a.dt_hours, depths.clone())?;
        let susc = SusceptibilityMap::uniform(dims, 1.0)?;
        let nc = make_nowcast(susc, forcing, &EngineArgs { id_a, ..a.engine.clone() })?;
        Ok(nc.run().into_iter().map(|f| f.max_probability() >= a.alert_level).collect())
    };

    println!(
        "Backtest: {} steps, {} events; I = a·D^-{:.2}, ±{} month match\n",
        depths.len(),
        events.len(),
        a.engine.id_b,
        a.tol_months
    );

    if let Some(spec) = &a.sweep {
        let (lo, hi, step) = parse_sweep(spec)?;
        let mut best = (f64::NAN, -1.0_f64);
        let mut v = lo;
        println!("   a     POD    FAR    CSI    bias");
        while v <= hi + 1e-9 {
            let alerts = alerts_for(v)?;
            let c = monthly_contingency(&day_month, &alerts, &events, a.tol_months);
            let csi = c.csi().unwrap_or(0.0);
            println!(
                "{:>5.1}  {}  {}  {}  {}",
                v,
                fmt(c.pod()),
                fmt(c.far()),
                fmt(c.csi()),
                fmt(c.frequency_bias())
            );
            if csi > best.1 {
                best = (v, csi);
            }
            v += step;
        }
        println!("\nmax-CSI intercept a* = {:.1} (CSI {:.3})", best.0, best.1);
    } else {
        let alerts = alerts_for(a.engine.id_a)?;
        let c = monthly_contingency(&day_month, &alerts, &events, a.tol_months);
        println!(
            "a = {:.2}:  POD {}  FAR {}  CSI {}  bias {}",
            a.engine.id_a,
            fmt(c.pod()),
            fmt(c.far()),
            fmt(c.csi()),
            fmt(c.frequency_bias())
        );
    }
    Ok(())
}

// ----------------------------------------------------------------------------
// CSV helpers (std-only; tolerant of a header row).

/// Numeric values in `column` (0-based), skipping non-numeric rows (e.g. header).
fn column_values(text: &str, column: usize) -> Vec<f64> {
    text.lines()
        .filter_map(|l| l.split(',').nth(column))
        .filter_map(|f| f.trim().parse::<f64>().ok())
        .collect()
}

/// `(year, month)` parsed from a leading `YYYY-MM-...` date column (column 0),
/// for every row whose date parses (skips the header).
fn month_keys(text: &str) -> Result<Vec<MonthKey>> {
    let mut out = Vec::new();
    for line in text.lines() {
        let Some(date) = line.split(',').next() else {
            continue;
        };
        let mut d = date.trim().split('-');
        let (Some(y), Some(m)) = (d.next(), d.next()) else {
            continue;
        };
        if let (Ok(y), Ok(m)) = (y.parse::<i32>(), m.parse::<u32>()) {
            out.push((y, m));
        }
    }
    Ok(out)
}

/// Event inventory `(year, month)` from columns 1 and 2 (id, year, month).
fn read_events(path: &Path) -> Result<Vec<MonthKey>> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("reading events CSV {}", path.display()))?;
    Ok(text
        .lines()
        .skip(1)
        .filter_map(|line| {
            let mut f = line.split(',');
            let _id = f.next()?;
            let y: i32 = f.next()?.trim().parse().ok()?;
            let m: u32 = f.next()?.trim().parse().ok()?;
            Some((y, m))
        })
        .collect())
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
