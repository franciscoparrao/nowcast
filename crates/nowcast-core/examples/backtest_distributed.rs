//! Distributed backtest over the Río Maipo event region — the v0.2 answer to the
//! structural false-alarm problem the v0.1 single-gauge backtest exposed.
//!
//! Inputs (regenerate with `scripts/extract_maipo_distributed.py`):
//!   - `data/maipo_dist_pr.csv`     daily CR2MET precip per cell (15×18 grid).
//!   - `data/maipo_dist_grid.csv`   per-cell real RandomForest susceptibility.
//!   - `data/maipo_dist_events.csv` rainfall-triggered events mapped to cells.
//!
//! Run with: `cargo run --example backtest_distributed`
//!
//! Three things change versus v0.1: the forcing is **distributed** (each cell
//! sees its own rainfall, not a basin centroid), the susceptibility is the
//! **real** raster (not held at 1), and verification is **spatial** (per cell).
//!
//! Metric choice matters here. Per-cell verification against a dated landslide
//! inventory is an extreme rare-event, spatially-sparse problem with an
//! *incomplete* record (SERNAGEOMIN lists only notable slides), so CSI/FAR are
//! meaningless — almost every "false alarm" is just a susceptible, wet cell with
//! no *recorded* event. The landslide-susceptibility and EWS literature scores
//! discrimination instead: **ROC-AUC** (does hazard rank event cell-months above
//! non-event ones?) and **POD at a fixed alerted-area fraction** (catch rate for
//! a given alert budget). We report both, isolating distributed forcing and real
//! susceptibility against a lumped baseline.

use std::collections::BTreeSet;
use std::path::PathBuf;

use nowcast_core::{
    GridDims, GriddedRain, IdThreshold, MonthKey, Nowcast, SusceptibilityMap, TriggerModel,
};

const A_INTERCEPT: f64 = 5.5; // regional I–D intercept calibrated in the v0.1 backtest
const B_EXPONENT: f64 = 0.39;
const MAX_WINDOW_DAYS: usize = 7;
const CELL_RADIUS: usize = 1; // ±1 CR2MET cell (~5.5 km) spatial match
const TOL_MONTHS: u32 = 1;

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

/// grid metadata → (dims, susceptibility per cell row-major).
fn read_grid() -> (GridDims, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_grid.csv")).unwrap();
    let mut max_row = 0usize;
    let mut max_col = 0usize;
    let mut rows: Vec<(usize, f64)> = Vec::new();
    for line in text.lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let cell: usize = f[0].parse().unwrap();
        let row: usize = f[1].parse().unwrap();
        let col: usize = f[2].parse().unwrap();
        let susc: f64 = f[5].parse().unwrap();
        max_row = max_row.max(row);
        max_col = max_col.max(col);
        rows.push((cell, susc));
    }
    rows.sort_by_key(|(c, _)| *c);
    let susc = rows.into_iter().map(|(_, s)| s).collect();
    (GridDims::new(max_col + 1, max_row + 1), susc)
}

/// distributed precip → (per-day month keys, GriddedRain depths step-major).
fn read_precip(dims: GridDims) -> (Vec<MonthKey>, Vec<f64>) {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_pr.csv")).unwrap();
    let n = dims.len();
    let mut months = Vec::new();
    let mut depths = Vec::new();
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let date = f.next().unwrap();
        let mut d = date.split('-');
        let y: i32 = d.next().unwrap().parse().unwrap();
        let m: u32 = d.next().unwrap().parse().unwrap();
        months.push((y, m));
        let before = depths.len();
        depths.extend(f.map(|v| v.parse::<f64>().unwrap_or(0.0)));
        debug_assert_eq!(depths.len() - before, n);
    }
    (months, depths)
}

fn read_events() -> Vec<(usize, MonthKey)> {
    let text = std::fs::read_to_string(data_dir().join("maipo_dist_events.csv")).unwrap();
    text.lines()
        .skip(1)
        .filter_map(|line| {
            let f: Vec<&str> = line.split(',').collect();
            Some((f[3].parse().ok()?, (f[1].parse().ok()?, f[2].parse().ok()?)))
        })
        .collect()
}

/// Per-(cell, month) maximum hazard, given a susceptibility map.
fn monthly_max_hazard(
    dims: GridDims,
    susc: &SusceptibilityMap,
    depths: &[f64],
    day_month: &[MonthKey],
) -> (Vec<MonthKey>, Vec<Vec<f64>>) {
    let forcing = GriddedRain::new(dims, 24.0, depths.to_vec()).unwrap();
    let nowcast = Nowcast::new(
        susc.clone(),
        forcing,
        IdThreshold::new(A_INTERCEPT, B_EXPONENT).unwrap(),
        TriggerModel::default(),
        MAX_WINDOW_DAYS,
    )
    .unwrap();

    // Distinct months in order of first appearance.
    let mut months: Vec<MonthKey> = Vec::new();
    let mut seen: BTreeSet<MonthKey> = BTreeSet::new();
    for &mk in day_month {
        if seen.insert(mk) {
            months.push(mk);
        }
    }
    let month_idx = |mk: MonthKey| months.iter().position(|&x| x == mk).unwrap();

    let n = dims.len();
    let mut max_haz = vec![vec![0.0_f64; n]; months.len()];
    for (step, field) in nowcast.run().into_iter().enumerate() {
        let mi = month_idx(day_month[step]);
        for (cell, &p) in field.probability().iter().enumerate() {
            if p > max_haz[mi][cell] {
                max_haz[mi][cell] = p;
            }
        }
    }
    (months, max_haz)
}

/// Space–time positive set: cell-months within ±radius / ±tol of any event,
/// as flat indices `month_idx * n_cells + cell`.
fn positive_footprint(
    dims: GridDims,
    months: &[MonthKey],
    events: &[(usize, MonthKey)],
) -> BTreeSet<usize> {
    let n = dims.len();
    let month_idx = |mk: MonthKey| months.iter().position(|&x| x == mk);
    let mut pos = BTreeSet::new();
    let r = CELL_RADIUS as i64;
    let tol = TOL_MONTHS as i32;
    for &(ec, (ey, em)) in events {
        let (er, ecol) = ((ec / dims.ncols) as i64, (ec % dims.ncols) as i64);
        for dr in -r..=r {
            for dc in -r..=r {
                let (nr, nc) = (er + dr, ecol + dc);
                if nr < 0 || nc < 0 || nr >= dims.nrows as i64 || nc >= dims.ncols as i64 {
                    continue;
                }
                let cell = nr as usize * dims.ncols + nc as usize;
                for d in -tol..=tol {
                    let z = ey * 12 + (em as i32 - 1) + d;
                    let mk = (z.div_euclid(12), (z.rem_euclid(12) + 1) as u32);
                    if let Some(mi) = month_idx(mk) {
                        pos.insert(mi * n + cell);
                    }
                }
            }
        }
    }
    pos
}

/// Flatten per-(month, cell) hazard into a single score vector, month-major.
fn flatten(max_haz: &[Vec<f64>]) -> Vec<f64> {
    max_haz.iter().flat_map(|row| row.iter().copied()).collect()
}

/// ROC-AUC via the rank (Mann–Whitney U) identity.
fn auc(scores: &[f64], positive: &BTreeSet<usize>) -> f64 {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| scores[a].partial_cmp(&scores[b]).unwrap());
    // Average ranks (1-based), handling ties.
    let mut rank = vec![0.0_f64; scores.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i;
        while j + 1 < order.len() && scores[order[j + 1]] == scores[order[i]] {
            j += 1;
        }
        let avg = (i + j) as f64 / 2.0 + 1.0; // mean of ranks i+1..=j+1
        for &idx in &order[i..=j] {
            rank[idx] = avg;
        }
        i = j + 1;
    }
    let n_pos = positive.len();
    let n_neg = scores.len() - n_pos;
    if n_pos == 0 || n_neg == 0 {
        return f64::NAN;
    }
    let sum_pos: f64 = positive.iter().map(|&idx| rank[idx]).sum();
    (sum_pos - n_pos as f64 * (n_pos as f64 + 1.0) / 2.0) / (n_pos as f64 * n_neg as f64)
}

/// POD when the top `frac` of cell-months (by score) are alerted: fraction of
/// events with ≥1 alerted cell-month in their footprint.
fn pod_at_area(
    dims: GridDims,
    months: &[MonthKey],
    max_haz: &[Vec<f64>],
    events: &[(usize, MonthKey)],
    frac: f64,
) -> f64 {
    let scores = flatten(max_haz);
    let mut sorted = scores.clone();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let thr = sorted[((sorted.len() as f64) * (1.0 - frac)) as usize];

    let month_idx = |mk: MonthKey| months.iter().position(|&x| x == mk);
    let r = CELL_RADIUS as i64;
    let tol = TOL_MONTHS as i32;
    let (mut hits, mut total) = (0u32, 0u32);
    for &(ec, (ey, em)) in events {
        if month_idx((ey, em)).is_none() {
            continue;
        }
        total += 1;
        let (er, ecol) = ((ec / dims.ncols) as i64, (ec % dims.ncols) as i64);
        let mut hit = false;
        'outer: for dr in -r..=r {
            for dc in -r..=r {
                let (nr, nc) = (er + dr, ecol + dc);
                if nr < 0 || nc < 0 || nr >= dims.nrows as i64 || nc >= dims.ncols as i64 {
                    continue;
                }
                let cell = nr as usize * dims.ncols + nc as usize;
                for d in -tol..=tol {
                    let z = ey * 12 + (em as i32 - 1) + d;
                    let mk = (z.div_euclid(12), (z.rem_euclid(12) + 1) as u32);
                    if let Some(mi) = month_idx(mk)
                        && max_haz[mi][cell] >= thr
                    {
                        hit = true;
                        break 'outer;
                    }
                }
            }
        }
        if hit {
            hits += 1;
        }
    }
    hits as f64 / total as f64
}

fn main() {
    let (dims, susc_vals) = read_grid();
    let (day_month, depths) = read_precip(dims);
    let events = read_events();
    println!(
        "Distributed Maipo backtest — {}×{} grid ({} cells), {} days, {} events\n\
         a={A_INTERCEPT} mm/h, b={B_EXPONENT}, window {MAX_WINDOW_DAYS}d, footprint ±{CELL_RADIUS} cell / ±{TOL_MONTHS} mo\n",
        dims.nrows,
        dims.ncols,
        dims.len(),
        depths.len() / dims.len(),
        events.len(),
    );

    let real = SusceptibilityMap::new(dims, susc_vals).unwrap();
    let flat = SusceptibilityMap::uniform(dims, 1.0).unwrap();

    // Three configurations sharing the same I–D trigger.
    let (months, haz_dist_flat) = monthly_max_hazard(dims, &flat, &depths, &day_month);
    let (_, haz_dist_real) = monthly_max_hazard(dims, &real, &depths, &day_month);

    // Lumped baseline: every cell sees the basin-mean rain (broadcast), × real susc.
    let n = dims.len();
    let n_steps = depths.len() / n;
    let mut lumped = vec![0.0; depths.len()];
    for s in 0..n_steps {
        let mean = depths[s * n..(s + 1) * n].iter().sum::<f64>() / n as f64;
        for c in 0..n {
            lumped[s * n + c] = mean;
        }
    }
    let (_, haz_lumped_real) = monthly_max_hazard(dims, &real, &lumped, &day_month);

    let pos = positive_footprint(dims, &months, &events);
    println!(
        "{} cell-months, {} positive (event footprint)\n",
        months.len() * n,
        pos.len()
    );

    println!("{:>30} | {:>6} | {:>8} {:>8} {:>8}", "configuration", "AUC", "POD@5%", "POD@10%", "POD@20%");
    let row = |label: &str, haz: &[Vec<f64>]| {
        let a = auc(&flatten(haz), &pos);
        println!(
            "{label:>30} | {a:.3}  | {:>7.0}% {:>7.0}% {:>7.0}%",
            100.0 * pod_at_area(dims, &months, haz, &events, 0.05),
            100.0 * pod_at_area(dims, &months, haz, &events, 0.10),
            100.0 * pod_at_area(dims, &months, haz, &events, 0.20),
        );
    };
    row("lumped (basin mean) × susc", &haz_lumped_real);
    row("distributed, susc = 1", &haz_dist_flat);
    row("distributed × real susc", &haz_dist_real);

    let best_auc = [&haz_lumped_real, &haz_dist_flat, &haz_dist_real]
        .iter()
        .map(|h| auc(&flatten(h), &pos))
        .fold(f64::MIN, f64::max);
    println!("\nReading the result (AUC > 0.5 = hazard ranks event cell-months above quiet ones):");
    if best_auc < 0.55 {
        println!(
            "  All three configurations sit near AUC 0.5 — at CR2MET's 5 km / daily resolution the\n  \
             gridded rainfall does not discriminate the recorded event cell-months (their mean\n  \
             windowed rainfall is no higher than average), and 30 m susceptibility averaged to 5 km\n  \
             loses its edge. The bottleneck here is forcing and susceptibility *resolution* (and the\n  \
             inventory's month-level dating), not the lumping. This is the honest case for plugging in\n  \
             higher-resolution forcing — sub-basin rainflow/snowmelt, radar/satellite QPE — which is\n  \
             exactly what the `Forcing` trait makes swappable."
        );
    } else {
        println!(
            "  Distributing the forcing and/or weighting by real susceptibility lifts discrimination\n  \
             above the lumped baseline — resolving where rain fell and where the terrain is prone."
        );
    }

    // Dump the cell-month score/label table for bootstrap-CI analysis (Python).
    let mut out = String::from("month_idx,cell,label,s_lumped,s_dist1,s_distsusc\n");
    for mi in 0..months.len() {
        for c in 0..n {
            let label = u8::from(pos.contains(&(mi * n + c)));
            out.push_str(&format!(
                "{mi},{c},{label},{:.4},{:.4},{:.4}\n",
                haz_lumped_real[mi][c], haz_dist_flat[mi][c], haz_dist_real[mi][c]
            ));
        }
    }
    std::fs::write(data_dir().join("cellmonths.csv"), out).unwrap();
    eprintln!("wrote {} cell-months to data/cellmonths.csv", months.len() * n);
}
