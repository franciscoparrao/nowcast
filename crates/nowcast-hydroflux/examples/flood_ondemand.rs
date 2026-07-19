//! On-demand flood inundation: the fine-scale end of the EWS cascade for
//! **crecidas**, twin of `nowcast-swarm/examples/runout_ondemand.rs` for
//! debris flows.
//!
//! Given a point (lon, lat), this runner assembles a 2D shallow-water run
//! *from scratch* over the real terrain:
//!
//! - **DEM**: Copernicus GLO-30 streamed from the public AWS bucket (same
//!   COG range-read + tile mosaic as `runout_ondemand`), sampled at two
//!   resolutions from one tile mosaic:
//!   - a **local window** (`--size-km`, 30 m pixels) — the [`Mesh2D`] the
//!     shallow-water solver actually integrates over.
//!   - a **basin window** (`--basin-window-km`, coarser `--basin-pixel-m`
//!     pixels, default 90 m) — used *only* to estimate the drainage area
//!     upstream of the point via D8 flow accumulation + watershed
//!     delineation. It is deliberately larger than the local window because
//!     the contributing area of a real river is almost always bigger than a
//!     10-12 km flood window.
//!
//! Two effects are modelled:
//!
//! **A) Local rain** (always run): the forecast rain (mm/day → m/s)
//! falling directly on the local window via [`Inundation::run_rain`],
//! showing ponding/concentration in local quebradas and esteros.
//!
//! **B) Upstream headwater discharge** (run only when the guard below
//! passes): the drainage area upstream of the point is delineated on the
//! basin grid with `surtgis_algorithms::hydrology::watershed`. If that
//! watershed **closes within the basin window** (does not touch its edge,
//! and is not a suspiciously large fraction of it), the area is converted
//! to a discharge via [`discharge_to_inflow_m3s`] and injected as a
//! [`PointSource`] at the local window's channel-entry cell.
//!
//! **Honesty guard**: if the watershed does *not* close within the basin
//! window — the classic case of a locality sitting on a trunk river
//! (Biobío-scale, tens of thousands of km²), where no operationally
//! sized window can capture the true contributing area — no discharge
//! number is invented. The run falls back to local rain only, and
//! `stats.json` says so explicitly (`basin.truncated = true` +
//! `basin.reason`).
//!
//! When both effects apply they are combined within *one* integration via
//! [`Inundation::run_rain_and_point_sources`] (see that method's docs for why
//! two separate runs cannot be superposed after the fact).
//!
//! Outputs in `--out`: `flood_depth.tif` (max water depth per cell,
//! georeferenced on the local window) and `stats.json`.
//!
//! Needs network access to the public Copernicus DEM bucket only.

use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use clap::Parser;
use surtgis_core::ndarray::Array2;
use nowcast_hydroflux::{
    Boundaries2D, Inundation, Mesh2D, PointSource, discharge_to_inflow_m3s,
};
use surtgis_algorithms::hydrology::d8::{D8_OFFSETS, opposite};
use surtgis_algorithms::hydrology::{
    FillSinksParams, StreamNetworkParams, WatershedParams, fill_sinks, flow_accumulation,
    flow_direction, stream_network, watershed,
};
use surtgis_cloud::blocking::read_cog;
use surtgis_cloud::reproject::{utm_to_wgs84, wgs84_to_utm};
use surtgis_cloud::{BBox, CogReaderOptions};
use surtgis_core::io::write_geotiff;
use surtgis_core::{CRS, GeoTransform, Raster};

const COPERNICUS_BUCKET: &str = "https://copernicus-dem-30m.s3.amazonaws.com";

/// On-demand flood inundation over a lon/lat window: Copernicus DEM + local
/// rain + (when the basin closes) an upstream headwater discharge, routed
/// through the Hydroflux 2D shallow-water solver.
#[derive(Parser, Debug)]
#[command(name = "flood_ondemand")]
struct Args {
    /// Longitude of the window centre (WGS84 degrees).
    #[arg(long, allow_negative_numbers = true)]
    lon: f64,
    /// Latitude of the window centre (WGS84 degrees).
    #[arg(long, allow_negative_numbers = true)]
    lat: f64,
    /// Local flood window side, km (Mesh2D grid is size_km × size_km at
    /// 30 m pixels).
    #[arg(long, default_value_t = 10.0)]
    size_km: f64,
    /// Basin delineation window side, km — must be ≥ `size_km`. Coarser and
    /// larger than the local window so a real upstream drainage area has a
    /// chance to close within it.
    #[arg(long, default_value_t = 50.0)]
    basin_window_km: f64,
    /// Pixel size of the basin delineation grid, m. Coarser than the local
    /// 30 m grid to keep flow-accumulation/watershed tractable over a much
    /// larger window (functionally the GLO-90 degradation the on-demand
    /// runout runner documents for the Araucanía transfer, obtained here by
    /// resampling the same GLO-30 tile mosaic instead of a second bucket).
    #[arg(long, default_value_t = 90.0)]
    basin_pixel_m: f64,
    /// Forecast rain, mm/day (single value: the Hydroflux rain forcing is a
    /// constant rate for the run's `--duration-hours`).
    #[arg(long, default_value_t = 80.0)]
    rain_mm_per_day: f64,
    /// Manning roughness n [s/m^(1/3)], constant over the window — v0
    /// approximation, same value used in `couple_flood.rs` (documented
    /// there as an approximation, like the runout runner's constant
    /// sediment=0.5).
    #[arg(long, default_value_t = 0.035)]
    manning_n: f64,
    /// Simulated duration, hours.
    #[arg(long, default_value_t = 3.0)]
    duration_hours: f64,
    /// CFL number (0,1].
    #[arg(long, default_value_t = 0.4)]
    cfl: f64,
    /// Step cap for the integration.
    #[arg(long, default_value_t = 20_000)]
    max_steps: usize,
    /// Minimum depth counted as "inundated", m.
    #[arg(long, default_value_t = 0.05)]
    min_depth: f64,
    /// Flow-accumulation threshold for the local QA stream layer, km² of
    /// contributing area (same role as in `runout_ondemand`).
    #[arg(long, default_value_t = 1.0)]
    stream_km2: f64,
    /// Honesty-guard threshold: if the delineated basin area exceeds this
    /// fraction of the basin window's own area, treat it as truncated (the
    /// true divide likely lies outside the window) even if it does not
    /// literally touch the window edge.
    #[arg(long, default_value_t = 0.6)]
    basin_max_window_frac: f64,
    /// Nowcast probability used only to compute the illustrative refined
    /// hazard field reported in stats (not written as a separate raster).
    #[arg(long, default_value_t = 0.7)]
    nowcast_prob: f64,
    /// Output directory (created if missing).
    #[arg(long, default_value = "flood_out")]
    out: PathBuf,
    /// Also write the assembled layers (dem/streams/basin) as GeoTIFFs into
    /// --out, for QGIS inspection.
    #[arg(long, default_value_t = false)]
    write_layers: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    if args.basin_window_km < args.size_km {
        bail!("--basin-window-km ({}) must be >= --size-km ({})", args.basin_window_km, args.size_km);
    }
    if args.duration_hours <= 0.0 {
        bail!("--duration-hours must be > 0");
    }
    fs::create_dir_all(&args.out)
        .with_context(|| format!("creating output dir {}", args.out.display()))?;

    // ── 1. UTM zone of the run (no susceptibility raster to take it from —
    //       unlike runout_ondemand, this runner is DEM-only) ────────────────
    let zone = ((args.lon + 180.0) / 6.0).floor() as u32 + 1;
    let north = args.lat >= 0.0;
    let epsg = if north { 32600 + zone } else { 32700 + zone };
    let (cx, cy) = wgs84_to_utm(args.lon, args.lat, zone, north);
    eprintln!("UTM {zone}{} (EPSG:{epsg}) · centro ({cx:.0}, {cy:.0})", if north { "N" } else { "S" });

    // ── 2. Two grids sharing the same centre: local (fine) and basin
    //       (coarse, larger) ─────────────────────────────────────────────────
    const PIXEL_M: f64 = 30.0;
    let n_local = ((args.size_km * 1000.0 / PIXEL_M).round() as usize).max(32);
    let half_local = n_local as f64 * PIXEL_M / 2.0;
    let n_basin = ((args.basin_window_km * 1000.0 / args.basin_pixel_m).round() as usize).max(32);
    let half_basin = n_basin as f64 * args.basin_pixel_m / 2.0;
    let snap = |v: f64, px: f64| (v / px).round() * px;
    let (ox_l, oy_l) = (snap(cx - half_local, PIXEL_M), snap(cy + half_local, PIXEL_M));
    let (ox_b, oy_b) = (snap(cx - half_basin, args.basin_pixel_m), snap(cy + half_basin, args.basin_pixel_m));
    let transform_local = GeoTransform::new(ox_l, oy_l, PIXEL_M, -PIXEL_M);
    let transform_basin = GeoTransform::new(ox_b, oy_b, args.basin_pixel_m, -args.basin_pixel_m);
    eprintln!(
        "Ventana local: {n_local}×{n_local} @ {PIXEL_M:.0} m (~{:.1} km) · origen ({ox_l:.0}, {oy_l:.0})",
        n_local as f64 * PIXEL_M / 1000.0,
    );
    eprintln!(
        "Ventana de cuenca: {n_basin}×{n_basin} @ {:.0} m (~{:.1} km) · origen ({ox_b:.0}, {oy_b:.0})",
        args.basin_pixel_m,
        n_basin as f64 * args.basin_pixel_m / 1000.0,
    );

    // ── 3. Copernicus GLO-30: read the tiles covering the (larger) basin
    //       window once, sample both grids from the same mosaic ────────────
    let (tiles, tile_ids) = read_copernicus_tiles(&transform_basin, n_basin, args.basin_pixel_m, zone, north)?;
    if tiles.is_empty() {
        bail!("no Copernicus GLO-30 tile could be read for this window");
    }
    let dem_local = sample_grid(&tiles, &transform_local, n_local, PIXEL_M, zone, north, epsg);
    let dem_basin = sample_grid(&tiles, &transform_basin, n_basin, args.basin_pixel_m, zone, north, epsg);

    let (dl_min, dl_max, dl_mean, dl_valid) = raster_stats(&dem_local);
    eprintln!(
        "DEM local: {n_local}×{n_local} · {dl_min:.0}-{dl_max:.0} m (media {dl_mean:.0} m) · {:.1}% válidas · tiles {tile_ids:?}",
        dl_valid * 100.0,
    );
    if dl_valid < 0.5 {
        bail!("more than half the local DEM window is nodata — wrong tiles or ocean?");
    }
    let (db_min, db_max, db_mean, db_valid) = raster_stats(&dem_basin);
    eprintln!(
        "DEM cuenca: {n_basin}×{n_basin} · {db_min:.0}-{db_max:.0} m (media {db_mean:.0} m) · {:.1}% válidas",
        db_valid * 100.0,
    );
    if db_valid < 0.5 {
        bail!("more than half the basin DEM window is nodata — wrong tiles or ocean?");
    }

    // ── 4. Local terrain derivatives: filled DEM → flow dir/accum, streams
    //       (QA layer + channel-entry search) ───────────────────────────────
    let filled_local = fill_sinks(&dem_local, FillSinksParams::default()).context("fill_sinks local")?;
    let fdir_local = flow_direction(&filled_local).context("flow_direction local")?;
    let facc_local = flow_accumulation(&fdir_local).context("flow_accumulation local")?;
    let threshold_cells_local = (args.stream_km2 * 1.0e6 / (PIXEL_M * PIXEL_M)).max(1.0);
    let streams_local = stream_network(
        &facc_local,
        StreamNetworkParams { threshold: threshold_cells_local },
    )
    .context("stream_network local")?;
    let stream_frac_local =
        streams_local.data().iter().filter(|&&v| v > 0).count() as f64 / (n_local * n_local) as f64;
    eprintln!(
        "Streams locales: umbral {:.1} km² ({:.0} celdas) → {:.2}% de la ventana",
        args.stream_km2, threshold_cells_local, stream_frac_local * 100.0,
    );

    // The locality's own coordinates are usually a geocoded town centroid, not
    // a point on the river — snapping the pour point to whatever channel lies
    // within a small radius of it risks catching a minor local rill instead
    // of the actual river that would flood the town. Anchor on the **largest
    // channel that crosses the local flood window** instead (its global
    // flow-accumulation maximum): that is, by construction, the river whose
    // behaviour actually matters for this locality.
    let (chan_row, chan_col) = argmax_cell(&facc_local);
    let chan_x = transform_local.origin_x + (chan_col as f64 + 0.5) * PIXEL_M;
    let chan_y = transform_local.origin_y - (chan_row as f64 + 0.5) * PIXEL_M;
    eprintln!(
        "Canal principal de la ventana local: fila {chan_row} col {chan_col} \
         (facc local {:.0} celdas)",
        facc_local.data()[[chan_row, chan_col]],
    );

    // ── 5. Basin terrain derivatives + watershed from that channel ──────────
    let filled_basin = fill_sinks(&dem_basin, FillSinksParams::default()).context("fill_sinks basin")?;
    let fdir_basin = flow_direction(&filled_basin).context("flow_direction basin")?;
    let facc_basin = flow_accumulation(&fdir_basin).context("flow_accumulation basin")?;

    let (row0_b, col0_b) = xy_to_rc(&transform_basin, chan_x, chan_y);
    // Wider snap radius than a straight point-to-channel snap: it only has to
    // re-find the *same* channel on a coarser/differently-registered grid,
    // not search for a channel from scratch.
    let (pour_row, pour_col) = snap_to_channel(&facc_basin, row0_b, col0_b, 6);
    let mut watershed_params = WatershedParams::default();
    watershed_params.pour_points = vec![(pour_row, pour_col)];
    let basins = watershed(&fdir_basin, watershed_params)
        .context("watershed")?;
    let basin_cells = basins.data().iter().filter(|&&v| v == 1).count();
    let area_basin_km2 = basin_cells as f64 * args.basin_pixel_m * args.basin_pixel_m / 1.0e6;
    let window_basin_km2 = (n_basin * n_basin) as f64 * args.basin_pixel_m * args.basin_pixel_m / 1.0e6;
    let ratio = area_basin_km2 / window_basin_km2;
    let touches_edge = {
        let (rows, cols) = basins.shape();
        (0..cols).any(|c| basins.data()[[0, c]] == 1 || basins.data()[[rows - 1, c]] == 1)
            || (0..rows).any(|r| basins.data()[[r, 0]] == 1 || basins.data()[[r, cols - 1]] == 1)
    };
    let truncated = touches_edge || ratio > args.basin_max_window_frac;
    let basin_reason = if !truncated {
        format!(
            "cuenca cierra dentro de la ventana ({area_basin_km2:.1} km² de {window_basin_km2:.1} km², {:.0}%)",
            ratio * 100.0
        )
    } else if touches_edge {
        format!(
            "cuenca troncal: la divisoria de aguas no cierra dentro de la ventana de {window_basin_km2:.1} km² \
             (celdas de la cuenca tocan el borde de la ventana) — no se inyecta caudal de cabecera"
        )
    } else {
        format!(
            "cuenca sospechosamente grande: {area_basin_km2:.1} km² es {:.0}% de la ventana de \
             {window_basin_km2:.1} km² (umbral {:.0}%) — no se inyecta caudal de cabecera",
            ratio * 100.0,
            args.basin_max_window_frac * 100.0,
        )
    };
    eprintln!("Cuenca aportante (punto de vertido fila {pour_row} col {pour_col}): {basin_reason}");

    // ── 6. Local channel-entry cell (only needed if we will inject): walk the
    //       main channel found in step 4 upstream to where it crosses the
    //       local window's boundary ─────────────────────────────────────────
    let (entry_row, entry_col) = if !truncated {
        walk_upstream_to_edge(&fdir_local, &facc_local, (chan_row, chan_col))
    } else {
        (0, 0)
    };

    // ── 7. Forcing: rain always, headwater discharge only if the basin
    //       closed ──────────────────────────────────────────────────────────
    let rain_rate_m_s = args.rain_mm_per_day * 1.0e-3 / 86_400.0;
    let duration_s = args.duration_hours * 3600.0;
    eprintln!(
        "Forzante: lluvia {:.1} mm/día ({:.3e} m/s) · duración {:.1} h ({:.0} s)",
        args.rain_mm_per_day, rain_rate_m_s, args.duration_hours, duration_s,
    );

    // The hydraulic bed must be the REAL terrain, not the sink-filled DEM:
    // `fill_sinks` raises cells inside closed depressions so D8 routing has
    // somewhere to flow (needed for flow_direction/flow_accumulation/streams
    // above), which can turn a real pit into an artificial flat plateau —
    // exactly the kind of discontinuity that made an early version of this
    // runner blow up to hundreds of metres of "depth" over a real closed
    // basin near the DEM edge. The shallow-water solver does not need
    // sink-filling (a real depression legitimately holds water); it only
    // needs no NaN gaps, so gaps are patched from the filled DEM without
    // altering any cell that already has a real elevation.
    let bed_local: Vec<f64> = dem_local
        .data()
        .iter()
        .zip(filled_local.data().iter())
        .map(|(&raw, &filled)| if raw.is_finite() { raw } else { filled })
        .collect();
    let bed_local = Array2::from_shape_vec((n_local, n_local), bed_local)
        .expect("dem_local and filled_local share the n_local x n_local shape");
    let mesh = Mesh2D::new(bed_local, PIXEL_M, PIXEL_M, args.manning_n);
    let inund = Inundation::new(mesh, Boundaries2D::TRANSMISSIVE, duration_s)
        .context("Inundation::new")?
        .with_cfl(args.cfl)
        .context("with_cfl")?
        .with_max_steps(args.max_steps)
        .context("with_max_steps")?;

    let (headwater_used, inflow_m3s, field, stats) = if !truncated {
        let inflow = discharge_to_inflow_m3s(args.rain_mm_per_day, area_basin_km2);
        let sources = vec![PointSource { row: entry_row, col: entry_col, q_mass: inflow }];
        eprintln!(
            "Cabecera: inyectando {inflow:.1} m³/s en celda local (fila {entry_row}, col {entry_col}) \
             — {area_basin_km2:.1} km² × {:.1} mm/día (balance instantáneo, sin tránsito)",
            args.rain_mm_per_day,
        );
        let (field, stats) = inund
            .run_rain_and_point_sources(rain_rate_m_s, &sources)
            .context("run_rain_and_point_sources")?;
        (true, inflow, field, stats)
    } else {
        eprintln!("Sin cabecera: solo lluvia local (guardia de honestidad rechazó la cuenca)");
        let (field, stats) = inund.run_rain(rain_rate_m_s);
        (false, 0.0, field, stats)
    };
    if stats.truncated {
        eprintln!(
            "warning: integration hit the {}-step cap at t={:.0}s of {:.0}s requested — depth field is PARTIAL",
            args.max_steps, stats.t_reached_s, duration_s,
        );
    }
    if stats.unstable {
        eprintln!("warning: solver reported non-finite depth in at least one cell — field is UNSTABLE, do not trust it for an alert");
    }

    let max_depth = field.max_depth();
    let mean_depth = field.mean_depth();
    let inundated_fraction = field.inundated_fraction(args.min_depth);
    let inundated_km2 = inundated_fraction * (n_local * n_local) as f64 * PIXEL_M * PIXEL_M / 1.0e6;
    eprintln!(
        "Inundación: profundidad máx {max_depth:.2} m · media {mean_depth:.3} m · {:.1}% de la ventana \
         ({inundated_km2:.2} km²)",
        inundated_fraction * 100.0,
    );
    if max_depth <= 0.0 {
        eprintln!("warning: zero depth everywhere — check rain/duration/manning inputs");
    }

    // ── 8. Physical sanity: does depth concentrate on streams / low ground? ─
    let depth = field.depth();
    let mut on_stream = 0usize;
    let (mut elev_in, mut n_in) = (0.0f64, 0usize);
    let (mut elev_out, mut n_out) = (0.0f64, 0usize);
    for (i, &h) in depth.iter().enumerate() {
        let (row, col) = (i / n_local, i % n_local);
        let z = filled_local.data()[[row, col]];
        if !z.is_finite() {
            continue;
        }
        if h >= args.min_depth {
            if streams_local.data()[[row, col]] > 0 {
                on_stream += 1;
            }
            elev_in += z;
            n_in += 1;
        } else {
            elev_out += z;
            n_out += 1;
        }
    }
    let frac_stream_in = if n_in > 0 { on_stream as f64 / n_in as f64 } else { 0.0 };
    let enrichment = if stream_frac_local > 0.0 { frac_stream_in / stream_frac_local } else { 0.0 };
    let mean_elev_in = if n_in > 0 { elev_in / n_in as f64 } else { f64::NAN };
    let mean_elev_out = if n_out > 0 { elev_out / n_out as f64 } else { f64::NAN };
    eprintln!(
        "Sanidad física: {:.1}% de las celdas inundadas caen sobre streams (ventana {:.1}% → \
         enriquecimiento ×{:.1}) · elev media inundada {mean_elev_in:.0} m vs seca {mean_elev_out:.0} m",
        frac_stream_in * 100.0, stream_frac_local * 100.0, enrichment,
    );
    if n_in > 0 && mean_elev_in > mean_elev_out {
        eprintln!(
            "warning: inundated cells sit HIGHER on average ({mean_elev_in:.0} m) than dry cells \
             ({mean_elev_out:.0} m) — check inputs, this contradicts water flowing downhill"
        );
    }

    // ── 9. Outputs: GeoTIFF depth + stats JSON ───────────────────────────────
    let crs = Some(CRS::from_epsg(epsg));
    let depth_f32: Vec<f32> = depth.iter().map(|&h| h as f32).collect();
    let depth_array = Array2::from_shape_vec((n_local, n_local), depth_f32)
        .expect("DepthField length matches n_local x n_local");
    let mut depth_raster = Raster::from_array(depth_array);
    depth_raster.set_transform(transform_local);
    depth_raster.set_crs(crs.clone());
    let tif_path = args.out.join("flood_depth.tif");
    write_geotiff(&depth_raster, &tif_path, None).context("writing flood_depth.tif")?;

    // Verification: re-read the GeoTIFF and re-check shape/stats.
    let back: Raster<f32> = surtgis_core::io::read_geotiff(&tif_path, Some(0)).context("re-reading output")?;
    let back_max = back.data().iter().cloned().fold(0.0f32, f32::max);
    if back.shape() != (n_local, n_local) || (back_max - max_depth as f32).abs() > 1e-3 {
        bail!(
            "output GeoTIFF verification failed: shape {:?} max {back_max} (expected ({n_local},{n_local}) / {max_depth})",
            back.shape(),
        );
    }
    eprintln!(
        "GeoTIFF verificado (releído): {}×{} px · max {back_max:.2} m · {}",
        back.rows(), back.cols(), tif_path.display(),
    );

    if args.write_layers {
        write_geotiff(&filled_local, args.out.join("dem_local_filled.tif"), None)?;
        write_geotiff(&streams_local, args.out.join("streams_local.tif"), None)?;
        write_geotiff(&filled_basin, args.out.join("dem_basin_filled.tif"), None)?;
        write_geotiff(&basins, args.out.join("basin_watershed.tif"), None)?;
        eprintln!("Capas intermedias escritas en {}", args.out.display());
    }

    let refined_flagged = field
        .refined_hazard(0, args.nowcast_prob, args.min_depth)
        .map(|h| h.probability().iter().filter(|&&p| p > 0.0).count())
        .unwrap_or(0);

    let stats_json = serde_json::json!({
        "point": { "lon": args.lon, "lat": args.lat },
        "local_grid": {
            "epsg": epsg, "pixel_m": PIXEL_M, "cells": n_local,
            "origin_x": ox_l, "origin_y": oy_l, "size_km": n_local as f64 * PIXEL_M / 1000.0,
        },
        "basin_grid": {
            "epsg": epsg, "pixel_m": args.basin_pixel_m, "cells": n_basin,
            "origin_x": ox_b, "origin_y": oy_b,
            "size_km": n_basin as f64 * args.basin_pixel_m / 1000.0,
        },
        "dem": {
            "source": format!("Copernicus GLO-30 COG ({COPERNICUS_BUCKET}), basin grid resampled to \
                {:.0} m from the same mosaic (functionally GLO-90 degradation)", args.basin_pixel_m),
            "tiles": tile_ids,
            "local": { "min_m": dl_min, "max_m": dl_max, "mean_m": dl_mean, "valid_frac": dl_valid },
            "basin": { "min_m": db_min, "max_m": db_max, "mean_m": db_mean, "valid_frac": db_valid },
        },
        "streams_local": {
            "threshold_km2": args.stream_km2,
            "threshold_cells": threshold_cells_local,
            "window_frac": stream_frac_local,
        },
        "basin": {
            "local_main_channel_row_col": [chan_row, chan_col],
            "pour_point_row_col": [pour_row, pour_col],
            "area_km2": area_basin_km2,
            "window_km2": window_basin_km2,
            "window_frac": ratio,
            "touches_edge": touches_edge,
            "truncated": truncated,
            "reason": basin_reason,
            "max_window_frac_guard": args.basin_max_window_frac,
        },
        "headwater_source": {
            "used": headwater_used,
            "inflow_m3s": inflow_m3s,
            "entry_cell_row_col": [entry_row, entry_col],
            "note": "instantaneous water balance (mm/day -> m3/s over the estimated basin area), \
                     no routing/transit time or losses; reasonable order-of-magnitude only for \
                     fast-response (Andean / coastal-range) basins, overestimates for large or \
                     low-gradient basins",
        },
        "forcing": {
            "rain_mm_per_day": args.rain_mm_per_day,
            "rain_rate_m_s": rain_rate_m_s,
            "duration_hours": args.duration_hours,
            "duration_s": duration_s,
        },
        "solver": {
            "manning_n": args.manning_n, "cfl": args.cfl, "max_steps": args.max_steps,
            "min_depth_m": args.min_depth,
        },
        "integration": {
            "t_reached_s": stats.t_reached_s, "steps": stats.steps,
            "truncated": stats.truncated, "unstable": stats.unstable,
        },
        "result": {
            "max_depth_m": max_depth, "mean_depth_m": mean_depth,
            "inundated_fraction": inundated_fraction, "inundated_km2": inundated_km2,
        },
        "sanity": {
            "frac_inundated_on_stream": frac_stream_in,
            "stream_window_frac": stream_frac_local,
            "stream_enrichment": enrichment,
            "mean_elev_inundated_m": mean_elev_in,
            "mean_elev_dry_m": mean_elev_out,
        },
        "refined_hazard": { "nowcast_prob": args.nowcast_prob, "flagged_cells": refined_flagged },
        "outputs": { "depth_geotiff": tif_path.display().to_string() },
    });
    let json_path = args.out.join("stats.json");
    fs::write(&json_path, serde_json::to_string_pretty(&stats_json)?)?;
    eprintln!("Stats: {}", json_path.display());
    Ok(())
}

/// Copernicus GLO-30 tile id for the 1°×1° cell whose SW corner is
/// (`tlon`, `tlat`), e.g. (-71, -31) → `S31_00_W071_00`.
fn copernicus_tile_id(tlat: i32, tlon: i32) -> String {
    let lat = if tlat < 0 { format!("S{:02}", -tlat) } else { format!("N{tlat:02}") };
    let lon = if tlon < 0 { format!("W{:03}", -tlon) } else { format!("E{tlon:03}") };
    format!("{lat}_00_{lon}_00")
}

/// Read every Copernicus GLO-30 tile intersecting a `n`×`n` window at
/// `pixel_m` centred by `transform` (≤ ~9 tiles for windows under ~100 km).
/// Tiles that fail to read (e.g. all-ocean cells absent from the bucket) are
/// skipped with a warning. Same pattern as `runout_ondemand`.
fn read_copernicus_tiles(
    transform: &GeoTransform,
    n: usize,
    pixel_m: f64,
    zone: u32,
    north: bool,
) -> Result<(Vec<Raster<f32>>, Vec<String>)> {
    let side = n as f64 * pixel_m;
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
            let bbox = BBox::new(
                min_lon.max(f64::from(tlon)),
                min_lat.max(f64::from(tlat)),
                max_lon.min(f64::from(tlon) + 1.0),
                max_lat.min(f64::from(tlat) + 1.0),
            );
            match read_cog::<f32>(&url, &bbox, CogReaderOptions::default()) {
                Ok(r) => {
                    eprintln!("  tile {id}: {}×{} px leídos del bucket público", r.rows(), r.cols());
                    rasters.push(r);
                    ids.push(id);
                }
                Err(e) => eprintln!("  tile {id}: no leído ({e}) — se omite"),
            }
        }
    }
    Ok((rasters, ids))
}

/// Sample a mosaic of tiles onto an `n`×`n` grid at `pixel_m`, centred by
/// `transform`. NaN where no tile covers a cell.
#[allow(clippy::too_many_arguments)]
fn sample_grid(
    tiles: &[Raster<f32>],
    transform: &GeoTransform,
    n: usize,
    pixel_m: f64,
    zone: u32,
    north: bool,
    epsg: u32,
) -> Raster<f64> {
    let mut data = Array2::<f64>::from_elem((n, n), f64::NAN);
    for row in 0..n {
        for col in 0..n {
            let e = transform.origin_x + (col as f64 + 0.5) * pixel_m;
            let nn = transform.origin_y - (row as f64 + 0.5) * pixel_m;
            let (lon, lat) = utm_to_wgs84(e, nn, zone, north);
            for tile in tiles {
                let v = sample_bilinear(tile, lon, lat);
                if v.is_finite() {
                    data[[row, col]] = v;
                    break;
                }
            }
        }
    }
    let mut r = Raster::from_array(data);
    r.set_transform(*transform);
    r.set_crs(Some(CRS::from_epsg(epsg)));
    r.set_nodata(Some(f64::NAN));
    r
}

/// NaN-tolerant bilinear sample of `r` at WGS84 (`lon`, `lat`) (pixel-centre
/// convention; edges clamped). Returns NaN outside the raster or where every
/// neighbour is nodata.
fn sample_bilinear(r: &Raster<f32>, lon: f64, lat: f64) -> f64 {
    let gt = r.transform();
    let (rows, cols) = r.shape();
    let cf = (lon - gt.origin_x) / gt.pixel_width - 0.5;
    let rf = (lat - gt.origin_y) / gt.pixel_height - 0.5;
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
        if !v.is_finite() || nodata.is_some_and(|nd| v == nd) { f64::NAN } else { f64::from(v) }
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

/// (row, col) of the finite cell with the largest value in `r` — used to find
/// the dominant channel (max flow accumulation) crossing a window.
fn argmax_cell(r: &Raster<f64>) -> (usize, usize) {
    let (rows, cols) = r.shape();
    let mut best = (0usize, 0usize);
    let mut best_val = f64::MIN;
    for row in 0..rows {
        for col in 0..cols {
            let v = r.data()[[row, col]];
            if v.is_finite() && v > best_val {
                best_val = v;
                best = (row, col);
            }
        }
    }
    best
}

/// UTM (x, y) to (row, col) on a grid described by `transform`, clamped to
/// bounds.
fn xy_to_rc(transform: &GeoTransform, x: f64, y: f64) -> (usize, usize) {
    let col = ((x - transform.origin_x) / transform.pixel_width).floor().max(0.0) as usize;
    let row = ((y - transform.origin_y) / transform.pixel_height).floor().max(0.0) as usize;
    (row, col)
}

/// Snap a seed cell to the highest-flow-accumulation cell within a
/// `radius`-cell square neighbourhood — standard pour-point snapping, so a
/// locality's coordinates (rarely exactly on the D8 channel pixel) land on
/// the actual channel instead of an adjacent hillslope cell.
fn snap_to_channel(facc: &Raster<f64>, row0: usize, col0: usize, radius: usize) -> (usize, usize) {
    let (rows, cols) = facc.shape();
    let row0 = row0.min(rows.saturating_sub(1));
    let col0 = col0.min(cols.saturating_sub(1));
    let r0 = row0.saturating_sub(radius);
    let r1 = (row0 + radius).min(rows - 1);
    let c0 = col0.saturating_sub(radius);
    let c1 = (col0 + radius).min(cols - 1);
    let mut best = (row0, col0);
    let mut best_val = f64::MIN;
    for r in r0..=r1 {
        for c in c0..=c1 {
            let v = facc.data()[[r, c]];
            if v.is_finite() && v > best_val {
                best_val = v;
                best = (r, c);
            }
        }
    }
    best
}

/// Walk upstream from `start` along the D8 network, at each step following
/// the incoming neighbour with the highest flow accumulation (the local
/// mainstem), until the grid boundary is reached. This is how the "channel
/// entry point at the edge of the local window" is found — trace the
/// mainstem passing through the point back to where it crosses the window
/// boundary. Mirrors `surtgis_algorithms::hydrology::watershed`'s own
/// "does this neighbour flow into me" check (`neighbour_dir ==
/// opposite(direction towards it)`).
fn walk_upstream_to_edge(fdir: &Raster<u8>, facc: &Raster<f64>, start: (usize, usize)) -> (usize, usize) {
    let (rows, cols) = fdir.shape();
    let mut cur = start;
    loop {
        let (row, col) = cur;
        if row == 0 || row == rows - 1 || col == 0 || col == cols - 1 {
            return cur;
        }
        let mut best: Option<(usize, usize)> = None;
        let mut best_facc = f64::MIN;
        for (idx, &(dr, dc)) in D8_OFFSETS.iter().enumerate() {
            let nr = row as isize + dr;
            let nc = col as isize + dc;
            if nr < 0 || nc < 0 || nr as usize >= rows || nc as usize >= cols {
                continue;
            }
            let (nr, nc) = (nr as usize, nc as usize);
            let neighbor_dir = fdir.data()[[nr, nc]];
            if neighbor_dir == 0 {
                continue;
            }
            if neighbor_dir == opposite((idx + 1) as u8) {
                let f = facc.data()[[nr, nc]];
                if f.is_finite() && f > best_facc {
                    best_facc = f;
                    best = Some((nr, nc));
                }
            }
        }
        match best {
            Some(next) => cur = next,
            None => return cur, // headwater reached before the boundary
        }
    }
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
