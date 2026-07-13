//! On-demand debris-flow runout: the fine-scale end of the EWS cascade.
//!
//! Given a point (lon, lat) and a window size in km, this runner assembles the
//! full `debris_flow::Layers` stack *from scratch* and simulates the runout:
//!
//! - **DEM**: Copernicus GLO-30 streamed straight from the public AWS bucket
//!   (COG range reads via `surtgis-cloud`; up to 4 tiles mosaicked on the fly).
//! - **slope / streams**: derived from the DEM with `surtgis-algorithms`
//!   (Horn slope; fill sinks → D8 flow direction → flow accumulation →
//!   threshold in km² of contributing area).
//! - **susceptibility**: the user's ML GeoTIFF (e.g. XGBoost Elqui, 30 m),
//!   resampled/aligned to the window grid (nodata → 0).
//! - **rain / isotherm / sediment**: v0 proxies from the CLI — uniform daily
//!   rain (mm/day, broadcast over the window; max 3 days, the model's hourly
//!   disaggregation patterns), a constant 0 °C isotherm elevation (cells below
//!   it are "liquid rain": binary layer like the Copiapó stack), and a
//!   constant sediment availability.
//!
//! The window grid is metric UTM at 30 m (the zone is taken from the
//! susceptibility raster's CRS), so `pixel_size = 30 m` is exact for the ABM
//! and the output GeoTIFF is natively georeferenced.
//!
//! Outputs in `--out`: `runout_footprint.tif` (1 = reached by the flow) and
//! `stats.json` (inputs, sources, params, footprint stats, sanity metrics).
//!
//! Example (quebrada piloto, valle del Elqui cerca de Vicuña):
//!
//! ```text
//! cargo run --release -p nowcast-swarm --example runout_ondemand -- \
//!     --lon -70.70 --lat -30.03 --size-km 12 \
//!     --rain-mm-per-day 30,60,40 --isotherm-m 2000 \
//!     --susceptibility /path/to/susceptibility_XGBoost.tif \
//!     --out /tmp/runout
//! ```
//!
//! Needs network access to the public Copernicus DEM bucket only.

use std::fs;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use clap::Parser;
use surtgis_core::ndarray::Array2;
use nowcast_surtgis::{Georef, write_hazard_geotiff};
use nowcast_swarm::{DebrisParams, Layers, SwarmGrid, SwarmPos, run_runout};
use surtgis_algorithms::hydrology::{
    FillSinksParams, StreamNetworkParams, fill_sinks, flow_accumulation, flow_direction,
    stream_network,
};
use surtgis_algorithms::terrain::{SlopeParams, SlopeUnits, slope};
use surtgis_cloud::blocking::read_cog;
use surtgis_cloud::reproject::{parse_utm_epsg, utm_to_wgs84, wgs84_to_utm};
use surtgis_cloud::{BBox, CogReaderOptions};
use surtgis_core::io::{read_geotiff, write_geotiff};
use surtgis_core::{CRS, GeoTransform, Raster, ResampleMethod, resample_to_grid};

const PIXEL_M: f64 = 30.0;
const COPERNICUS_BUCKET: &str = "https://copernicus-dem-30m.s3.amazonaws.com";

/// On-demand debris-flow runout over a lon/lat window (Copernicus DEM +
/// derived slope/streams + ML susceptibility + CLI forcing proxies).
#[derive(Parser, Debug)]
#[command(name = "runout_ondemand")]
struct Args {
    /// Longitude of the window centre (WGS84 degrees).
    #[arg(long, allow_negative_numbers = true)]
    lon: f64,
    /// Latitude of the window centre (WGS84 degrees).
    #[arg(long, allow_negative_numbers = true)]
    lat: f64,
    /// Window side, km (grid is size_km × size_km at 30 m).
    #[arg(long, default_value_t = 12.0)]
    size_km: f64,
    /// Daily rain, mm/day, comma-separated (1–3 days; broadcast uniformly).
    #[arg(long, value_delimiter = ',', default_values_t = [30.0, 60.0, 40.0])]
    rain_mm_per_day: Vec<f64>,
    /// 0 °C isotherm elevation, m (cells below it receive liquid rain). v0:
    /// constant over the window and the event.
    #[arg(long, default_value_t = 2000.0)]
    isotherm_m: f64,
    /// Constant sediment-availability proxy in [0,1] (v0; the Copiapó stack
    /// used a normalized map, mean ≈ 0.39).
    #[arg(long, default_value_t = 0.5)]
    sediment: f64,
    /// Susceptibility GeoTIFF ([0,1], any UTM zone; defines the UTM zone of
    /// the run). Nodata → 0.
    #[arg(
        long,
        default_value = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors/07_rio_elqui/susceptibility_XGBoost.tif"
    )]
    susceptibility: PathBuf,
    /// Flow-accumulation threshold for the stream network, km² of
    /// contributing area.
    #[arg(long, default_value_t = 1.0)]
    stream_km2: f64,
    /// Parameter preset: de (DE-calibrated, data/best_params_de.json),
    /// optuna (model default), 18iters, chanaral.
    #[arg(long, default_value = "de")]
    params: String,
    /// Override the number of rain agents (default: the preset's).
    #[arg(long)]
    agents: Option<usize>,
    /// ABM steps (1 step = 1 h; the rain event spans 72 h).
    #[arg(long, default_value_t = 300)]
    steps: u64,
    /// RNG seed.
    #[arg(long, default_value_t = 42)]
    seed: u64,
    /// Output directory (created if missing).
    #[arg(long, default_value = "runout_out")]
    out: PathBuf,
    /// Also write the assembled layers (dem/slope/streams/susceptibility) as
    /// GeoTIFFs into --out, for QGIS inspection.
    #[arg(long, default_value_t = false)]
    write_layers: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    if args.rain_mm_per_day.is_empty() || args.rain_mm_per_day.len() > 3 {
        bail!(
            "--rain-mm-per-day takes 1 to 3 days (the model's hourly \
             disaggregation patterns cover 3 days / 72 h)"
        );
    }
    if args.steps < 72 {
        eprintln!(
            "warning: --steps {} < 72 h truncates the rain event",
            args.steps
        );
    }
    fs::create_dir_all(&args.out)
        .with_context(|| format!("creating output dir {}", args.out.display()))?;

    // ── 1. Susceptibility raster defines the UTM zone of the run ────────
    let susc_raw: Raster<f32> = read_geotiff(&args.susceptibility, Some(0))
        .with_context(|| format!("reading susceptibility {}", args.susceptibility.display()))?;
    let (zone, north, epsg) = match susc_raw.crs().and_then(|c| c.epsg()) {
        Some(code) => match parse_utm_epsg(code) {
            Some((z, n)) => (z, n, code),
            None => bail!("susceptibility CRS EPSG:{code} is not UTM (326xx/327xx)"),
        },
        None => {
            // Fallback: zone of the centre point.
            let z = ((args.lon + 180.0) / 6.0).floor() as u32 + 1;
            let n = args.lat >= 0.0;
            let code = if n { 32600 + z } else { 32700 + z };
            eprintln!("warning: susceptibility has no EPSG; assuming UTM zone {z} → EPSG:{code}");
            (z, n, code)
        }
    };

    // ── 2. Window grid: n×n UTM cells at 30 m, centred on the point ─────
    let (cx, cy) = wgs84_to_utm(args.lon, args.lat, zone, north);
    let n = ((args.size_km * 1000.0 / PIXEL_M).round() as usize).max(32);
    let half = n as f64 * PIXEL_M / 2.0;
    let snap = |v: f64| (v / PIXEL_M).round() * PIXEL_M;
    let (ox, oy) = (snap(cx - half), snap(cy + half));
    let transform = GeoTransform::new(ox, oy, PIXEL_M, -PIXEL_M);
    println!(
        "Ventana: {n}×{n} celdas de 30 m (~{:.1} km) · UTM {zone}{} (EPSG:{epsg}) · \
         origen ({ox:.0}, {oy:.0})",
        n as f64 * PIXEL_M / 1000.0,
        if north { "N" } else { "S" },
    );

    // ── 3. Copernicus GLO-30: read the intersecting 1°×1° tiles ─────────
    let (tiles, tile_ids) = read_copernicus_tiles(&transform, n, zone, north)?;
    if tiles.is_empty() {
        bail!("no Copernicus GLO-30 tile could be read for this window");
    }

    // ── 4. DEM on the window grid (bilinear from the tile mosaic) ───────
    let mut dem_data = Array2::<f64>::from_elem((n, n), f64::NAN);
    for row in 0..n {
        for col in 0..n {
            let e = ox + (col as f64 + 0.5) * PIXEL_M;
            let nn = oy - (row as f64 + 0.5) * PIXEL_M;
            let (lon, lat) = utm_to_wgs84(e, nn, zone, north);
            for tile in &tiles {
                let v = sample_bilinear(tile, lon, lat);
                if v.is_finite() {
                    dem_data[[row, col]] = v;
                    break;
                }
            }
        }
    }
    let mut dem = Raster::from_array(dem_data);
    dem.set_transform(transform);
    dem.set_crs(Some(CRS::from_epsg(epsg)));
    dem.set_nodata(Some(f64::NAN));

    let (dem_min, dem_max, dem_mean, valid_frac) = raster_stats(&dem);
    println!(
        "DEM: {n}×{n} · {:.0}–{:.0} m (media {:.0} m) · {:.1}% celdas válidas · tiles {:?}",
        dem_min,
        dem_max,
        dem_mean,
        valid_frac * 100.0,
        tile_ids,
    );
    if valid_frac < 0.5 {
        bail!("more than half the DEM window is nodata — wrong tiles or ocean?");
    }

    // ── 5. Terrain derivatives: filled DEM → slope, streams ─────────────
    let filled = fill_sinks(&dem, FillSinksParams::default()).context("fill_sinks")?;
    let slope_pct = slope(
        &filled,
        SlopeParams {
            units: SlopeUnits::Percent,
            z_factor: 1.0,
        },
    )
    .context("slope")?;
    // Percent / 100 = tangent (m/m), the unit of the Copiapó slope layer.
    let slope_frac: Vec<f64> = slope_pct.data().iter().map(|&v| v / 100.0).collect();
    let (s50, s95) = percentiles(&slope_frac);
    println!(
        "Pendiente (fracción m/m): mediana {:.3} · p95 {:.3} (≈ {:.1}°/{:.1}°)",
        s50,
        s95,
        s50.atan().to_degrees(),
        s95.atan().to_degrees(),
    );

    let fdir = flow_direction(&filled).context("flow_direction")?;
    let facc = flow_accumulation(&fdir).context("flow_accumulation")?;
    let threshold_cells = (args.stream_km2 * 1.0e6 / (PIXEL_M * PIXEL_M)).max(1.0);
    let streams = stream_network(
        &facc,
        StreamNetworkParams {
            threshold: threshold_cells,
        },
    )
    .context("stream_network")?;
    let stream_frac = streams.data().iter().filter(|&&v| v > 0).count() as f64 / (n * n) as f64;
    println!(
        "Streams: umbral {:.1} km² ({:.0} celdas) → {:.2}% de la ventana",
        args.stream_km2,
        threshold_cells,
        stream_frac * 100.0,
    );

    // ── 6. Susceptibility resampled onto the window grid ────────────────
    let susc_nodata = susc_raw.nodata();
    let susc_f64_data = susc_raw.data().mapv(|v| {
        if !v.is_finite() || susc_nodata.is_some_and(|nd| v == nd) {
            f64::NAN
        } else {
            f64::from(v)
        }
    });
    let mut susc_f64 = Raster::from_array(susc_f64_data);
    susc_f64.set_transform(*susc_raw.transform());
    susc_f64.set_crs(susc_raw.crs().cloned());
    susc_f64.set_nodata(Some(f64::NAN));
    let susc =
        resample_to_grid(&susc_f64, &dem, ResampleMethod::NearestNeighbor).context("resample")?;
    let susc_clean = susc.data().mapv(|v| if v.is_finite() { v.clamp(0.0, 1.0) } else { 0.0 });
    let susc_mean = susc_clean.mean().unwrap_or(0.0);
    let susc_max = susc_clean.iter().fold(0.0f64, |m, &v| m.max(v));
    println!("Susceptibilidad en ventana: media {susc_mean:.3} · máx {susc_max:.3}");

    // ── 7. Assemble the Layers stack ─────────────────────────────────────
    let params = build_params(&args)?;
    if susc_max <= params.susceptibility_threshold {
        eprintln!(
            "warning: max susceptibility {:.3} ≤ threshold {:.3} — no flow can \
             be generated (window outside the susceptibility map?)",
            susc_max, params.susceptibility_threshold,
        );
    }
    let dem_grid = grid_from(&filled, |v| v as f32);
    let layers = Layers {
        slope: grid_from(&slope_pct, |v| (v / 100.0) as f32),
        rain: args
            .rain_mm_per_day
            .iter()
            .map(|&mm| SwarmGrid::fill(n, n, mm as f32))
            .collect(),
        // Binary "liquid rain" mask, like the Copiapó isotherm layer: 1 below
        // the 0 °C isotherm, 0 above (the model tests `isotherm > 0.5`).
        isotherm: SwarmGrid::from_fn(n, n, |p: SwarmPos| {
            let z = f64::from(dem_grid[p]);
            if z.is_finite() && z < args.isotherm_m { 1.0 } else { 0.0 }
        }),
        sediment: SwarmGrid::fill(n, n, args.sediment as f32),
        susceptibility: SwarmGrid::from_fn(n, n, |p: SwarmPos| susc_clean[[p.y, p.x]] as f32),
        streams: SwarmGrid::from_fn(n, n, |p: SwarmPos| {
            f32::from(streams.data()[[p.y, p.x]])
        }),
        dem: dem_grid,
    };

    // ── 8. Run the ABM ───────────────────────────────────────────────────
    println!(
        "Corriendo debris-flow ABM: preset '{}' · {} agentes de lluvia · {} pasos · semilla {}",
        args.params, params.n_rain_agents, args.steps, args.seed,
    );
    let runout = run_runout(Arc::new(layers), params.clone(), PIXEL_M, args.seed, args.steps)
        .context("run_runout")?;
    let cells = runout.affected_cells();
    let km2 = runout.affected_km2();
    let window_frac = cells as f64 / (n * n) as f64;
    println!(
        "Runout: {cells} celdas · {km2:.2} km² · {:.1}% de la ventana",
        window_frac * 100.0,
    );
    if cells == 0 {
        eprintln!("warning: empty footprint — check rain/isotherm/susceptibility inputs");
    }
    if window_frac > 0.9 {
        eprintln!("warning: footprint covers >90% of the window — parameters likely too hot");
    }

    // ── 9. Sanity: does the footprint follow drainage / go downslope? ────
    let fp = runout.footprint();
    let mut on_stream = 0usize;
    let (mut elev_in, mut n_in) = (0.0f64, 0usize);
    let (mut elev_out, mut n_out) = (0.0f64, 0usize);
    let (mut slope_in, mut slope_out) = (0.0f64, 0.0f64);
    for (i, &hit) in fp.iter().enumerate() {
        let (row, col) = (i / n, i % n);
        let z = filled.data()[[row, col]];
        let s = slope_frac[i];
        if !z.is_finite() {
            continue;
        }
        if hit {
            if streams.data()[[row, col]] > 0 {
                on_stream += 1;
            }
            elev_in += z;
            if s.is_finite() {
                slope_in += s;
            }
            n_in += 1;
        } else {
            elev_out += z;
            if s.is_finite() {
                slope_out += s;
            }
            n_out += 1;
        }
    }
    let frac_stream_fp = if n_in > 0 { on_stream as f64 / n_in as f64 } else { 0.0 };
    let enrichment = if stream_frac > 0.0 { frac_stream_fp / stream_frac } else { 0.0 };
    let mean_in = if n_in > 0 { elev_in / n_in as f64 } else { f64::NAN };
    let mean_out = if n_out > 0 { elev_out / n_out as f64 } else { f64::NAN };
    let mslope_in = if n_in > 0 { slope_in / n_in as f64 } else { f64::NAN };
    let mslope_out = if n_out > 0 { slope_out / n_out as f64 } else { f64::NAN };
    println!(
        "Sanidad física: {:.1}% del footprint cae sobre streams (ventana {:.1}% → \
         enriquecimiento ×{:.1}) · elev media dentro {:.0} m vs fuera {:.0} m",
        frac_stream_fp * 100.0,
        stream_frac * 100.0,
        enrichment,
        mean_in,
        mean_out,
    );

    // ── 10. Outputs: GeoTIFF footprint + stats JSON ──────────────────────
    let georef = Georef {
        transform,
        crs: Some(CRS::from_epsg(epsg)),
    };
    let hazard = runout.refined_hazard(0, 1.0)?;
    let tif_path = args.out.join("runout_footprint.tif");
    write_hazard_geotiff(&hazard, &georef, &tif_path)?;

    // Verification: re-read the GeoTIFF and re-count the footprint.
    let back: Raster<f32> = read_geotiff(&tif_path, Some(0)).context("re-reading output")?;
    let back_cells = back.data().iter().filter(|&&v| v > 0.5).count();
    if back.shape() != (n, n) || back_cells != cells {
        bail!(
            "output GeoTIFF verification failed: shape {:?} cells {back_cells} (expected ({n},{n}) / {cells})",
            back.shape(),
        );
    }
    println!(
        "GeoTIFF verificado (releído): {}×{} px · {back_cells} celdas en 1 · {}",
        back.rows(),
        back.cols(),
        tif_path.display(),
    );

    if args.write_layers {
        write_geotiff(&filled, args.out.join("dem_filled.tif"), None)?;
        write_geotiff(&slope_pct, args.out.join("slope_percent.tif"), None)?;
        write_geotiff(&streams, args.out.join("streams.tif"), None)?;
        write_geotiff(&susc, args.out.join("susceptibility_window.tif"), None)?;
        println!("Capas intermedias escritas en {}", args.out.display());
    }

    let stats = serde_json::json!({
        "point": { "lon": args.lon, "lat": args.lat },
        "grid": {
            "epsg": epsg, "pixel_m": PIXEL_M, "cells": n,
            "origin_x": ox, "origin_y": oy, "size_km": n as f64 * PIXEL_M / 1000.0,
        },
        "dem": {
            "source": format!("Copernicus GLO-30 COG ({COPERNICUS_BUCKET})"),
            "tiles": tile_ids,
            "min_m": dem_min, "max_m": dem_max, "mean_m": dem_mean,
            "valid_frac": valid_frac,
        },
        "slope": { "median_frac": s50, "p95_frac": s95 },
        "streams": {
            "threshold_km2": args.stream_km2,
            "threshold_cells": threshold_cells,
            "window_frac": stream_frac,
        },
        "susceptibility": {
            "source": args.susceptibility.display().to_string(),
            "window_mean": susc_mean, "window_max": susc_max,
        },
        "forcing": {
            "rain_mm_per_day": args.rain_mm_per_day,
            "isotherm_m": args.isotherm_m,
            "sediment_const": args.sediment,
        },
        "params": {
            "preset": args.params,
            "n_rain_agents": params.n_rain_agents,
            "steps": args.steps, "seed": args.seed,
        },
        "runout": {
            "affected_cells": cells,
            "affected_km2": km2,
            "window_frac": window_frac,
            "stream_enrichment": enrichment,
            "footprint_on_stream_frac": frac_stream_fp,
            "mean_elev_footprint_m": mean_in,
            "mean_elev_window_rest_m": mean_out,
            "mean_slope_footprint_frac": mslope_in,
            "mean_slope_window_rest_frac": mslope_out,
        },
        "outputs": { "footprint_geotiff": tif_path.display().to_string() },
    });
    let json_path = args.out.join("stats.json");
    fs::write(&json_path, serde_json::to_string_pretty(&stats)?)?;
    println!("Stats: {}", json_path.display());
    Ok(())
}

/// Preset → `DebrisParams`, with the optional agent-count override.
fn build_params(args: &Args) -> Result<DebrisParams> {
    let mut p = match args.params.as_str() {
        // `preset_de` == data/best_params_de.json (DE robusto, IoU 0.171).
        "de" => DebrisParams::preset_de(),
        // Model default == Optuna-TPE with temperature (IoU 0.134).
        "optuna" | "default" => DebrisParams::default(),
        "18iters" => DebrisParams::preset_18iters(),
        "chanaral" => DebrisParams::preset_chanaral(),
        other => bail!("unknown --params '{other}' (de|optuna|18iters|chanaral)"),
    };
    if let Some(a) = args.agents {
        p.n_rain_agents = a;
    }
    Ok(p)
}

/// Copernicus GLO-30 tile id for the 1°×1° cell whose SW corner is
/// (`tlon`, `tlat`), e.g. (-71, -31) → `S31_00_W071_00`.
fn copernicus_tile_id(tlat: i32, tlon: i32) -> String {
    let lat = if tlat < 0 {
        format!("S{:02}", -tlat)
    } else {
        format!("N{tlat:02}")
    };
    let lon = if tlon < 0 {
        format!("W{:03}", -tlon)
    } else {
        format!("E{tlon:03}")
    };
    format!("{lat}_00_{lon}_00")
}

/// Read every Copernicus GLO-30 tile intersecting the window (≤ 4 for
/// windows under ~100 km). Tiles that fail to read (e.g. all-ocean cells
/// absent from the bucket) are skipped with a warning.
fn read_copernicus_tiles(
    transform: &GeoTransform,
    n: usize,
    zone: u32,
    north: bool,
) -> Result<(Vec<Raster<f32>>, Vec<String>)> {
    // Geographic envelope of the UTM window (4 corners + padding for the
    // bilinear stencil).
    let side = n as f64 * PIXEL_M;
    let corners = [
        (transform.origin_x, transform.origin_y),
        (transform.origin_x + side, transform.origin_y),
        (transform.origin_x, transform.origin_y - side),
        (transform.origin_x + side, transform.origin_y - side),
    ];
    let (mut min_lon, mut min_lat) = (f64::MAX, f64::MAX);
    let (mut max_lon, mut max_lat) = (f64::MIN, f64::MIN);
    for (e, nn) in corners {
        let (lon, lat) = utm_to_wgs84(e, nn, zone, north);
        min_lon = min_lon.min(lon);
        max_lon = max_lon.max(lon);
        min_lat = min_lat.min(lat);
        max_lat = max_lat.max(lat);
    }
    let pad = 0.002; // ~200 m: bilinear margin at window and tile edges
    let (min_lon, max_lon) = (min_lon - pad, max_lon + pad);
    let (min_lat, max_lat) = (min_lat - pad, max_lat + pad);

    let mut rasters = Vec::new();
    let mut ids = Vec::new();
    for tlat in (min_lat.floor() as i32)..=(max_lat.floor() as i32) {
        for tlon in (min_lon.floor() as i32)..=(max_lon.floor() as i32) {
            let id = copernicus_tile_id(tlat, tlon);
            let name = format!("Copernicus_DSM_COG_10_{id}_DEM");
            let url = format!("{COPERNICUS_BUCKET}/{name}/{name}.tif");
            // Intersection of the padded window with this 1°×1° tile.
            let bbox = BBox::new(
                min_lon.max(f64::from(tlon)),
                min_lat.max(f64::from(tlat)),
                max_lon.min(f64::from(tlon) + 1.0),
                max_lat.min(f64::from(tlat) + 1.0),
            );
            match read_cog::<f32>(&url, &bbox, CogReaderOptions::default()) {
                Ok(r) => {
                    println!(
                        "  tile {id}: {}×{} px leídos del bucket público",
                        r.rows(),
                        r.cols()
                    );
                    rasters.push(r);
                    ids.push(id);
                }
                Err(e) => eprintln!("  tile {id}: no leído ({e}) — se omite"),
            }
        }
    }
    Ok((rasters, ids))
}

/// NaN-tolerant bilinear sample of `r` at CRS coordinates (`x`, `y`)
/// (pixel-centre convention; edges clamped). Returns NaN outside the raster
/// or where every neighbour is nodata.
fn sample_bilinear(r: &Raster<f32>, x: f64, y: f64) -> f64 {
    let gt = r.transform();
    let (rows, cols) = r.shape();
    let cf = (x - gt.origin_x) / gt.pixel_width - 0.5;
    let rf = (y - gt.origin_y) / gt.pixel_height - 0.5;
    if !cf.is_finite() || !rf.is_finite() {
        return f64::NAN;
    }
    if cf < -0.5 || rf < -0.5 || cf > cols as f64 - 0.5 || rf > rows as f64 - 0.5 {
        return f64::NAN;
    }
    let c0 = (cf.floor().max(0.0) as usize).min(cols - 1);
    let r0 = (rf.floor().max(0.0) as usize).min(rows - 1);
    let c1 = (c0 + 1).min(cols - 1);
    let r1 = (r0 + 1).min(rows - 1);
    let dc = (cf - c0 as f64).clamp(0.0, 1.0);
    let dr = (rf - r0 as f64).clamp(0.0, 1.0);
    let nodata = r.nodata();
    let val = |rr: usize, cc: usize| -> f64 {
        let v = r.data()[[rr, cc]];
        if !v.is_finite() || nodata.is_some_and(|nd| v == nd) {
            f64::NAN
        } else {
            f64::from(v)
        }
    };
    let neighbours = [
        (val(r0, c0), (1.0 - dc) * (1.0 - dr)),
        (val(r0, c1), dc * (1.0 - dr)),
        (val(r1, c0), (1.0 - dc) * dr),
        (val(r1, c1), dc * dr),
    ];
    let (mut sum, mut total) = (0.0, 0.0);
    for (v, w) in neighbours {
        if v.is_finite() {
            sum += v * w;
            total += w;
        }
    }
    if total > 0.0 { sum / total } else { f64::NAN }
}

/// Copy a `Raster<f64>` into a `SwarmGrid<f32>` (row = y, col = x).
fn grid_from(r: &Raster<f64>, f: impl Fn(f64) -> f32) -> SwarmGrid<f32> {
    SwarmGrid::from_fn(r.cols(), r.rows(), |p: SwarmPos| f(r.data()[[p.y, p.x]]))
}

/// (min, max, mean, valid fraction) over the finite cells of a raster.
fn raster_stats(r: &Raster<f64>) -> (f64, f64, f64, f64) {
    let (mut min, mut max, mut sum, mut count) = (f64::MAX, f64::MIN, 0.0, 0usize);
    for &v in r.data() {
        if v.is_finite() {
            min = min.min(v);
            max = max.max(v);
            sum += v;
            count += 1;
        }
    }
    let total = r.rows() * r.cols();
    if count == 0 {
        (f64::NAN, f64::NAN, f64::NAN, 0.0)
    } else {
        (min, max, sum / count as f64, count as f64 / total as f64)
    }
}

/// (median, p95) of the finite values.
fn percentiles(values: &[f64]) -> (f64, f64) {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return (f64::NAN, f64::NAN);
    }
    v.sort_by(|a, b| a.partial_cmp(b).expect("finite values"));
    let at = |q: f64| v[((v.len() - 1) as f64 * q).round() as usize];
    (at(0.5), at(0.95))
}
