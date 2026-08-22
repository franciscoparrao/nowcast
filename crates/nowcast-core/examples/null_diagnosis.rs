//! Diagnóstico del null distribuido: ¿por qué AUC ≈ 0.48, y de qué acusa?
//!
//! El backtest distribuido (`backtest_distributed`) no discrimina, y el dato que
//! se ofrecía como explicación —la lluvia media en las celdas-mes de evento
//! (45 mm) es MENOR que el promedio de la grilla (49 mm)— en realidad no apunta a
//! la resolución: si los eventos registrados caen donde llueve menos, eso acusa
//! al inventario o al campo de lluvia, no al paso temporal. Este ejemplo separa
//! las hipótesis en vez de discutirlas, con los mismos datos del backtest.
//!
//! (1) **¿Son celdas secas o meses secos?** La lluvia de una celda-mes de evento
//!     mezcla dos componentes: cuán lluviosa es ESA CELDA en general (gradiente
//!     espacial) y cuán húmedo fue ESE MES para esa celda (señal temporal). Se
//!     descomponen: media climatológica por celda, y anomalía estandarizada
//!     dentro de la celda (z-score). Si los eventos caen en celdas secas pero en
//!     meses húmedos PARA SU CELDA, hay señal temporal que el AUC global esconde
//!     porque lo domina el gradiente espacial.
//!
//! (2) **¿Está mal localizado el inventario?** Se recomputa la discriminación
//!     permitiendo que el evento se empareje con el máximo de lluvia en un
//!     vecindario de radio r = 0, 1, 2 celdas. Si el AUC sube con r, el problema
//!     es la localización del registro, no la forzante.
//!
//! (3) **¿Sesgo de exposición?** Se compara la susceptibilidad y la lluvia
//!     climatológica de las celdas CON evento contra las demás: los aluviones se
//!     registran donde hay gente y camino, no donde más llueve.
//!
//! Entradas: `data/maipo_dist_{pr,grid,events}.csv` (extract_maipo_distributed.py).
//!
//! Correr con: `cargo run --release --example null_diagnosis`

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

const NROWS: usize = 15;
const NCOLS: usize = 18;
const N_CELLS: usize = NROWS * NCOLS;
/// Ventana de acumulación usada por el backtest distribuido.
const WINDOW_DAYS: usize = 7;

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

fn read_text(name: &str) -> String {
    let p = data_dir().join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("cannot read {}: {e}", p.display()))
}

/// Lluvia diaria por celda + el mes de cada paso.
fn read_precip() -> (Vec<(i32, u32)>, Vec<Vec<f64>>) {
    let text = read_text("maipo_dist_pr.csv");
    let mut months = Vec::new();
    let mut rows = Vec::new();
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let date = f.next().unwrap();
        let mut d = date.split('-');
        let year: i32 = d.next().unwrap().parse().unwrap();
        let month: u32 = d.next().unwrap().parse().unwrap();
        months.push((year, month));
        rows.push(f.map(|v| v.trim().parse().unwrap_or(0.0)).collect::<Vec<f64>>());
    }
    (months, rows)
}

fn read_events() -> BTreeSet<((i32, u32), usize)> {
    read_text("maipo_dist_events.csv")
        .lines()
        .skip(1)
        .filter_map(|line| {
            let f: Vec<&str> = line.split(',').collect();
            Some((
                (f.get(1)?.parse().ok()?, f.get(2)?.parse().ok()?),
                f.get(3)?.trim().parse().ok()?,
            ))
        })
        .collect()
}

fn read_susceptibility() -> Vec<f64> {
    let mut susc = vec![0.0; N_CELLS];
    for line in read_text("maipo_dist_grid.csv").lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let cell: usize = f[0].parse().unwrap();
        susc[cell] = f[5].trim().parse().unwrap_or(0.0);
    }
    susc
}

/// Máxima lluvia acumulada en `WINDOW_DAYS` por (mes, celda) — el predictor que
/// usa el backtest distribuido.
fn monthly_max_window(months: &[(i32, u32)], pr: &[Vec<f64>]) -> BTreeMap<(i32, u32), Vec<f64>> {
    let mut out: BTreeMap<(i32, u32), Vec<f64>> = BTreeMap::new();
    for (t, month) in months.iter().enumerate().take(pr.len()) {
        let lo = t.saturating_sub(WINDOW_DAYS - 1);
        let entry = out.entry(*month).or_insert_with(|| vec![0.0; N_CELLS]);
        for (cell, slot) in entry.iter_mut().enumerate() {
            let acc: f64 = (lo..=t).map(|k| pr[k][cell]).sum();
            if acc > *slot {
                *slot = acc;
            }
        }
    }
    out
}

/// ROC-AUC (Mann–Whitney con rangos promedio para empates).
fn auc(scores: &[f64], labels: &[bool]) -> f64 {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| scores[a].partial_cmp(&scores[b]).unwrap());
    let mut rank = vec![0.0_f64; scores.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i;
        while j + 1 < order.len() && scores[order[j + 1]] == scores[order[i]] {
            j += 1;
        }
        let avg = (i + j) as f64 / 2.0 + 1.0;
        for &idx in &order[i..=j] {
            rank[idx] = avg;
        }
        i = j + 1;
    }
    let n_pos = labels.iter().filter(|&&l| l).count();
    let n_neg = labels.len() - n_pos;
    if n_pos == 0 || n_neg == 0 {
        return f64::NAN;
    }
    let sum_pos: f64 = (0..scores.len()).filter(|&i| labels[i]).map(|i| rank[i]).sum();
    (sum_pos - n_pos as f64 * (n_pos as f64 + 1.0) / 2.0) / (n_pos as f64 * n_neg as f64)
}

fn mean(v: &[f64]) -> f64 {
    if v.is_empty() { 0.0 } else { v.iter().sum::<f64>() / v.len() as f64 }
}

fn sd(v: &[f64]) -> f64 {
    if v.len() < 2 {
        return 0.0;
    }
    let m = mean(v);
    (v.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (v.len() - 1) as f64).sqrt()
}

fn neighbours(cell: usize, radius: isize) -> Vec<usize> {
    let (row, col) = ((cell / NCOLS) as isize, (cell % NCOLS) as isize);
    let mut out = Vec::new();
    for dr in -radius..=radius {
        for dc in -radius..=radius {
            let (r, c) = (row + dr, col + dc);
            if r >= 0 && r < NROWS as isize && c >= 0 && c < NCOLS as isize {
                out.push(r as usize * NCOLS + c as usize);
            }
        }
    }
    out
}

fn main() {
    let (months, pr) = read_precip();
    let events = read_events();
    let susc = read_susceptibility();
    let monthly = monthly_max_window(&months, &pr);
    let month_keys: Vec<(i32, u32)> = monthly.keys().copied().collect();

    // Climatología por celda y desviación, sobre todos los meses.
    let mut per_cell: Vec<Vec<f64>> =
        (0..N_CELLS).map(|_| Vec::with_capacity(month_keys.len())).collect();
    for mk in &month_keys {
        let row = &monthly[mk];
        for (cell, item) in per_cell.iter_mut().enumerate() {
            item.push(row[cell]);
        }
    }
    let clim: Vec<f64> = per_cell.iter().map(|v| mean(v)).collect();
    let disp: Vec<f64> = per_cell.iter().map(|v| sd(v)).collect();

    // Celdas que alguna vez registran evento.
    let event_cells: BTreeSet<usize> = events.iter().map(|&(_, c)| c).collect();

    println!("Diagnóstico del null distribuido — Río Maipo, {} meses × {N_CELLS} celdas\n",
             month_keys.len());

    // ── (1) ¿celdas secas o meses secos? ─────────────────────────────────────
    let (mut raw_ev, mut raw_no) = (Vec::new(), Vec::new());
    let (mut z_ev, mut z_no) = (Vec::new(), Vec::new());
    let (mut scores_raw, mut scores_z, mut labels) = (Vec::new(), Vec::new(), Vec::new());
    for mk in &month_keys {
        let row = &monthly[mk];
        for cell in 0..N_CELLS {
            let is_ev = events.contains(&(*mk, cell));
            let raw = row[cell];
            let z = if disp[cell] > 0.0 { (raw - clim[cell]) / disp[cell] } else { 0.0 };
            scores_raw.push(raw);
            scores_z.push(z);
            labels.push(is_ev);
            if is_ev {
                raw_ev.push(raw);
                z_ev.push(z);
            } else {
                raw_no.push(raw);
                z_no.push(z);
            }
        }
    }

    println!("(1) ¿ES UN PROBLEMA DE CELDA O DE MES?");
    println!("    lluvia máx-{WINDOW_DAYS}d en celdas-mes de EVENTO : {:>6.1} mm  (n = {})",
             mean(&raw_ev), raw_ev.len());
    println!("    lluvia máx-{WINDOW_DAYS}d en el resto             : {:>6.1} mm  (n = {})",
             mean(&raw_no), raw_no.len());
    println!("    → AUC con lluvia cruda                    : {:.3}", auc(&scores_raw, &labels));
    println!();
    println!("    MISMA lluvia como anomalía dentro de la celda (z-score, quita el gradiente espacial):");
    println!("    z en celdas-mes de EVENTO                 : {:>+6.2}", mean(&z_ev));
    println!("    z en el resto                             : {:>+6.2}", mean(&z_no));
    let auc_z = auc(&scores_z, &labels);
    println!("    → AUC con anomalía                        : {auc_z:.3}");
    println!(
        "    {}",
        if auc_z > 0.55 {
            "El mes SÍ es anómalamente húmedo para su celda: hay señal temporal que el\n    \
             AUC crudo esconde porque lo domina el gradiente espacial."
        } else {
            "Tampoco hay señal tras quitar el gradiente espacial: el mes del evento no es\n    \
             anómalamente húmedo para su propia celda."
        }
    );

    // ── (2) ¿dónde cae el evento en su propia distribución? ──────────────────
    // Percentiles, que responden directo sin construir un test ambiguo: si el
    // registro está bien fechado y localizado, su celda-mes debería estar alta
    // tanto en el tiempo (dentro de su celda) como en el espacio (dentro de su mes).
    let (mut pct_time, mut pct_space) = (Vec::new(), Vec::new());
    for &(mk, cell) in &events {
        let Some(row) = monthly.get(&mk) else { continue };
        let v = row[cell];
        // percentil temporal: contra los demás meses de ESA celda
        let n_below = per_cell[cell].iter().filter(|&&x| x < v).count();
        pct_time.push(100.0 * n_below as f64 / per_cell[cell].len() as f64);
        // percentil espacial: contra las demás celdas de ESE mes
        let n_below_s = row.iter().filter(|&&x| x < v).count();
        pct_space.push(100.0 * n_below_s as f64 / N_CELLS as f64);
    }
    println!("\n(2) ¿DÓNDE CAE EL EVENTO EN SU PROPIA DISTRIBUCIÓN? (percentil medio, 50 = azar)");
    println!("    en el tiempo, dentro de su celda : {:>5.1} %", mean(&pct_time));
    println!("    en el espacio, dentro de su mes  : {:>5.1} %", mean(&pct_space));
    println!("    Un registro bien fechado y localizado estaría alto en ambos.");

    // ── (3) ¿sesgo de exposición? ─────────────────────────────────────────────
    let clim_ev: Vec<f64> = (0..N_CELLS).filter(|c| event_cells.contains(c)).map(|c| clim[c]).collect();
    let clim_no: Vec<f64> = (0..N_CELLS).filter(|c| !event_cells.contains(c)).map(|c| clim[c]).collect();
    let susc_ev: Vec<f64> = (0..N_CELLS).filter(|c| event_cells.contains(c)).map(|c| susc[c]).collect();
    let susc_no: Vec<f64> = (0..N_CELLS).filter(|c| !event_cells.contains(c)).map(|c| susc[c]).collect();
    println!("\n(3) ¿SESGO DE EXPOSICIÓN? (celdas CON registro vs el resto, {} vs {})",
             clim_ev.len(), clim_no.len());
    println!("    lluvia climatológica máx-{WINDOW_DAYS}d : {:>6.1} vs {:>6.1} mm", mean(&clim_ev), mean(&clim_no));
    println!("    susceptibilidad media           : {:>6.3} vs {:>6.3}", mean(&susc_ev), mean(&susc_no));
    let rel = (mean(&clim_ev) - mean(&clim_no)) / mean(&clim_no);
    println!(
        "    diferencia relativa de lluvia   : {:>+6.1} %",
        100.0 * rel
    );
    println!(
        "    {}",
        if rel.abs() < 0.05 {
            "Las celdas CON registro reciben esencialmente la MISMA lluvia climatológica que\n    \
             el resto: la sequedad no está en la celda, así que tiene que estar en el MES."
        } else if rel < 0.0 {
            "Las celdas con registro son más secas que la grilla: sesgo de exposición."
        } else {
            "Las celdas con registro son más lluviosas que la grilla."
        }
    );

    // ── (4) ¿está el evento simplemente CORRIDO? ─────────────────────────────
    // Se dilata la ETIQUETA, no el predictor: una celda-mes cuenta como positiva
    // si hay un evento registrado dentro de ±k meses y radio r celdas. Si el
    // registro está meramente desplazado en tiempo o espacio, la señal de lluvia
    // debería aparecer al aflojar. (Dilatar el predictor en vez de la etiqueta
    // suaviza positivos y negativos por igual y no responde la pregunta.)
    println!("\n(4) ¿ESTÁ EL REGISTRO SIMPLEMENTE CORRIDO? (se dilata la ETIQUETA)");
    println!("    {:>12} | {:>8} | {:>8} | {:>10}", "dilatación", "r=0", "r=1", "positivas");
    for tol in 0..=2usize {
        let mut line = format!("    ±{tol} mes(es)  |");
        let mut n_pos_last = 0usize;
        for radius in 0..=1isize {
            let (mut sc, mut lb) = (Vec::new(), Vec::new());
            for (mi, mk) in month_keys.iter().enumerate() {
                let lo = mi.saturating_sub(tol);
                let hi = (mi + tol).min(month_keys.len() - 1);
                let row = &monthly[mk];
                for (cell, &value) in row.iter().enumerate().take(N_CELLS) {
                    let near = neighbours(cell, radius).into_iter().any(|c| {
                        (lo..=hi).any(|j| events.contains(&(month_keys[j], c)))
                    });
                    sc.push(value);
                    lb.push(near);
                }
            }
            n_pos_last = lb.iter().filter(|&&l| l).count();
            line.push_str(&format!(" {:>8.3} |", auc(&sc, &lb)));
        }
        println!("{line} {n_pos_last:>10}");
    }
    println!("    Si el AUC no sube al dilatar, el registro no está meramente corrido.");

    println!(
        "\n→ El null distribuido NO es evidencia limpia sobre la resolución temporal. Lo que estos\n  \
         diagnósticos ubican es de qué acusa el dato: dónde se registran los eventos, y qué\n  \
         componente de la lluvia (espacial o temporal) lleva la poca señal disponible."
    );
}
