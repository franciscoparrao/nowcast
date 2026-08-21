//! Experimento de UN SOLO FACTOR: separa el efecto del **producto** del efecto de
//! la **resolución**.
//!
//! El head-to-head (`resolution_headtohead`) compara CR2MET diario contra IMERG
//! semihorario. Esos dos difieren en DOS cosas a la vez —el producto de lluvia y
//! su resolución temporal— y en el mismo píxel discrepan por un factor ~3.6 en el
//! total del evento. Atribuir toda la diferencia a la resolución es un confound.
//!
//! Aquí se corre el MISMO motor I–D sobre tres configuraciones del MISMO núcleo de
//! tormenta, de modo que cada comparación adyacente mueve un solo factor:
//!
//!     A. CR2MET diario          (producto CR2MET, resolución 24 h)
//!        ↕ cambia SOLO el producto
//!     B. IMERG agregado a 24 h  (producto IMERG,  resolución 24 h)
//!        ↕ cambia SOLO la resolución
//!     C. IMERG semihorario      (producto IMERG,  resolución 0.5 h)
//!
//! Como la excedencia es multiplicativa en la intensidad, la razón total se
//! factoriza exactamente:
//!
//!     E(C)/E(A)  =  [E(B)/E(A)]  ×  [E(C)/E(B)]
//!                    ── producto ──   ── resolución ──
//!
//! Entradas:
//!   - `data/event_<key>.csv`         (IMERG semihorario; extract_event_imerg.py)
//!   - `data/event_<key>_cr2met.csv`  (CR2MET diario;     extract_event_cr2met.py)
//!
//! Correr con: `cargo run --example single_factor_resolution`

use std::collections::BTreeMap;
use std::path::PathBuf;

use nowcast_core::IdThreshold;

#[path = "shared/curves.rs"]
mod curves;

const SUB_DT_H: f64 = 0.5;
const SUB_MAX_M: usize = 48; // ventanas I–D hasta 24 h
const DAY_DT_H: f64 = 24.0;
const DAY_MAX_M: usize = 3;

struct Event {
    key: &'static str,
    label: &'static str,
}

const EVENTS: &[Event] = &[
    Event { key: "atacama_2015", label: "Atacama / Copiapó" },
    Event { key: "maipo_2017", label: "Cajón del Maipo" },
    Event { key: "santalucia_2017", label: "Villa Santa Lucía" },
];

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data")
}

/// Serie IMERG semihoraria del núcleo: (timestamps, profundidad mm por paso).
fn read_imerg(key: &str) -> Option<(Vec<String>, Vec<f64>)> {
    let text = std::fs::read_to_string(data_dir().join(format!("event_{key}.csv"))).ok()?;
    let (mut t, mut d) = (Vec::new(), Vec::new());
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        t.push(f.next()?.to_string());
        // mm/h → mm en el paso de media hora
        d.push(f.next()?.trim().parse::<f64>().unwrap_or(0.0) * SUB_DT_H);
    }
    Some((t, d))
}

/// Serie CR2MET diaria del mismo píxel: fecha → mm/día (máximo dentro del
/// footprint IMERG, la lectura más favorable al producto diario).
fn read_cr2met(key: &str) -> Option<BTreeMap<String, f64>> {
    let text = std::fs::read_to_string(data_dir().join(format!("event_{key}_cr2met.csv"))).ok()?;
    let mut m = BTreeMap::new();
    for line in text.lines().skip(1) {
        let mut f = line.split(',');
        let date = f.next()?.to_string();
        let v = f.next()?.trim().parse::<f64>().unwrap_or(0.0);
        m.insert(date, v);
    }
    Some(m)
}

/// Agrega la serie semihoraria a totales diarios, devolviendo (día, mm).
fn to_daily(stamps: &[String], depths: &[f64]) -> Vec<(String, f64)> {
    let mut out: Vec<(String, f64)> = Vec::new();
    for (t, d) in stamps.iter().zip(depths) {
        let day = t[..10].to_string();
        match out.last_mut() {
            Some((prev, acc)) if *prev == day => *acc += d,
            _ => out.push((day, *d)),
        }
    }
    out
}

struct Response {
    /// Excedencia máxima alcanzada en la ventana del evento.
    e_max: f64,
    /// Intensidad media de la ventana que la produce (mm/h).
    intensity: f64,
    /// Duración de esa ventana (h).
    duration_h: f64,
    /// Índice del paso en que se cruza el umbral, si se cruza.
    first_cross: Option<usize>,
    /// Total acumulado de la serie (mm).
    total: f64,
}

fn respond(depths: &[f64], dt: f64, max_m: usize, th: &IdThreshold) -> Response {
    let mut prefix = vec![0.0_f64; depths.len() + 1];
    for (i, d) in depths.iter().enumerate() {
        prefix[i + 1] = prefix[i] + d;
    }
    let (mut e_max, mut intensity, mut duration_h) = (0.0_f64, 0.0_f64, 0.0_f64);
    let mut first_cross = None;
    for t in 0..depths.len() {
        let avail = (t + 1).min(max_m);
        if avail == 0 {
            continue;
        }
        let (e, m) = th.worst_window(dt, avail, |m| prefix[t + 1] - prefix[t + 1 - m]);
        if e >= 1.0 && first_cross.is_none() {
            first_cross = Some(t);
        }
        if e > e_max {
            e_max = e;
            duration_h = m as f64 * dt;
            intensity = (prefix[t + 1] - prefix[t + 1 - m]) / duration_h;
        }
    }
    Response { e_max, intensity, duration_h, first_cross, total: prefix[depths.len()] }
}

fn verdict(e: f64) -> &'static str {
    if e >= 1.0 { "GATILLA" } else { "no" }
}

fn main() {
    let (a, b) = curves::REGIONAL;
    let th = IdThreshold::new(a, b).unwrap();

    println!("Experimento de UN SOLO FACTOR — producto vs resolución (I–D a*={a}, b={b})\n");
    println!(
        "Cada comparación adyacente mueve UN factor:\n  \
         A CR2MET diario  →(producto)→  B IMERG diario  →(resolución)→  C IMERG semihorario\n"
    );

    for ev in EVENTS {
        let Some((stamps, sub_depths)) = read_imerg(ev.key) else {
            println!("{}: sin IMERG — corre scripts/extract_event_imerg.py {}\n", ev.label, ev.key);
            continue;
        };
        let Some(cr2) = read_cr2met(ev.key) else {
            println!("{}: sin CR2MET — corre scripts/extract_event_cr2met.py {}\n", ev.label, ev.key);
            continue;
        };

        let imerg_daily = to_daily(&stamps, &sub_depths);
        // CR2MET restringido EXACTAMENTE a los días que cubre la ventana IMERG,
        // para que las tres configuraciones vean el mismo evento.
        let cr2_daily: Vec<f64> = imerg_daily
            .iter()
            .map(|(day, _)| cr2.get(day).copied().unwrap_or(0.0))
            .collect();
        let imerg_daily_depths: Vec<f64> = imerg_daily.iter().map(|(_, v)| *v).collect();

        let ra = respond(&cr2_daily, DAY_DT_H, DAY_MAX_M, &th);
        let rb = respond(&imerg_daily_depths, DAY_DT_H, DAY_MAX_M, &th);
        let rc = respond(&sub_depths, SUB_DT_H, SUB_MAX_M, &th);

        println!("── {} ──────────────────────────────────────────", ev.label);
        println!(
            "  {:<26} | {:>7} | {:>9} | {:>7} | {:>8} | ¿gatilla?",
            "configuración", "total", "I máx", "D", "E máx"
        );
        for (name, r) in [
            ("A. CR2MET diario", &ra),
            ("B. IMERG agregado a 24 h", &rb),
            ("C. IMERG semihorario", &rc),
        ] {
            println!(
                "  {:<26} | {:>4.1} mm | {:>5.2} mm/h | {:>4.1} h | {:>8.2} | {}",
                name, r.total, r.intensity, r.duration_h, r.e_max, verdict(r.e_max)
            );
        }

        if ra.e_max > 0.0 && rb.e_max > 0.0 {
            let f_prod = rb.e_max / ra.e_max;
            let f_res = rc.e_max / rb.e_max;
            println!(
                "  descomposición: producto ×{:.2}  ·  resolución ×{:.2}  =  total ×{:.2}",
                f_prod, f_res, rc.e_max / ra.e_max
            );
            let dominante = if f_res > f_prod { "la RESOLUCIÓN" } else { "el PRODUCTO" };
            println!("  factor dominante: {dominante}");
        } else {
            println!("  descomposición: no computable (CR2MET no registra lluvia en la ventana)");
        }

        // Lo que realmente cambia la decisión operacional.
        let flip_producto = (rb.e_max >= 1.0) != (ra.e_max >= 1.0);
        let flip_resolucion = (rc.e_max >= 1.0) != (rb.e_max >= 1.0);
        println!(
            "  ¿voltea la detección?  producto: {}   resolución: {}",
            if flip_producto { "SÍ" } else { "no" },
            if flip_resolucion { "SÍ" } else { "no" }
        );
        if let Some(s) = rc.first_cross {
            let day_cross = rb.first_cross.map(|d| imerg_daily[d].0.clone());
            println!(
                "  timing: sub-diario cruza {} | diario cruza {}",
                stamps[s][5..16].replace('T', " "),
                day_cross.unwrap_or_else(|| "nunca".into())
            );
        }
        println!();
    }

    println!(
        "→ La comparación CR2MET-diario vs IMERG-semihorario mezcla dos factores. Separados, el\n  \
         producto y la resolución actúan distinto según el evento: donde el producto diario ya ve\n  \
         la lluvia, subir la resolución cambia el TIMING pero no la detección; donde la ráfaga es\n  \
         breve, la resolución es la que voltea la decisión. La columna '¿voltea la detección?' es\n  \
         la que sostiene —o no— la claim de que hace falta forzante sub-diaria."
    );
}
