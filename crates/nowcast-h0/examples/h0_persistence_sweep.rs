//! Barrido de la sub-vía SOSTENIDA: ¿qué percentil y cuántos días consecutivos?
//!
//! El parámetro `min_days` se eligió a ojo (3). Esto lo mide contra los 6
//! eventos fechados, y —más importante— contra su COSTO: un criterio laxo
//! detecta todo porque marca todo, así que la detección sola no decide nada.
//! Por eso cada configuración se reporta con tres números:
//!
//!   · **detecta**  — eventos con la vía anomalía activa en algún día de su
//!     ventana documentada (de los 6, y aparte de los 3 con foco pleno en la
//!     transecta, que son los únicos que esta serie 30-36 °S puede juzgar).
//!   · **lead**     — días entre el disparo y el inicio documentado. Negativo =
//!     dispara tarde, dentro del evento. La persistencia siempre paga acá.
//!   · **marcados** — % de días de TORMENTA de toda la serie que la vía deja
//!     marcados. Es la tasa base: el precio de la sensibilidad.
//!
//! ADVERTENCIA que hay que leer antes que la tabla: n=6, tres de ellos con foco
//! solo parcial en la transecta y uno con confianza de fecha BAJA (la fuente es
//! un pronóstico de prensa, no un reporte post-evento). Esto descarta
//! configuraciones malas; no calibra un parámetro con precisión. Cualquier
//! diferencia de un evento entre dos filas está dentro del ruido.
//!
//! Datos: `python3 scripts/extract_h0_transecta.py`
//! Correr: `cargo run -p nowcast-h0 --example h0_persistence_sweep`

use std::path::PathBuf;

use nowcast_core::{GridDims, TriggerModel};
use nowcast_h0::{
    AbsoluteWay, H0Via, MonthlyStormClimatology, Persistence, TwoWayH0Trigger, ZeroIsothermSeries,
};

const UMBRAL_ABS_M: f64 = 4500.0;
const PICO_PCT: f64 = 90.0;

struct Evento {
    inicio: String,
    fin: String,
    nombre: String,
    pleno: bool,
    confianza: String,
}

fn main() {
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data");
    let text = leer(&base.join("h0_centro_diaria.csv"));
    let ev_text = leer(&base.join("eventos_mayores.csv"));
    let eventos = eventos(&ev_text);
    let series = ZeroIsothermSeries::from_csv(&text).expect("serie H0 mal formada");
    let dias: Vec<(String, f64, bool)> = dias(&text);
    let n_tormentas = dias.iter().filter(|d| d.2).count();

    println!("BARRIDO DE LA SUB-VÍA SOSTENIDA — {} eventos fechados", eventos.len());
    for e in &eventos {
        println!(
            "   {} .. {}  {}{}",
            e.inicio,
            e.fin,
            e.nombre,
            match (e.pleno, e.confianza.as_str()) {
                (true, "baja") => "  [confianza de fecha BAJA]",
                (false, "baja") => "  [foco PARCIAL + confianza BAJA]",
                (false, _) => "  [foco PARCIAL en la transecta]",
                _ => "",
            }
        );
    }
    let n_pleno = eventos.iter().filter(|e| e.pleno).count();
    println!(
        "\n   n={} y heterogéneos ({} con foco pleno). Esto DESCARTA configuraciones\n   \
         malas; no calibra con precisión. Un evento de diferencia es ruido.\n",
        eventos.len(),
        n_pleno
    );

    println!(
        "   {:<10}{:>9}{:>10}{:>10}{:>12}{:>10}",
        "sostenido", "anomalía", "de pleno", "índice", "lead medio", "marcados"
    );
    println!(
        "   {:<10}{:>9}{:>10}{:>10}{:>12}{:>10}",
        "", "(vía sola)", "", "completo", "", "(tormenta)"
    );
    println!("   {}", "-".repeat(72));

    // Fila de referencia: solo pico, sin sub-vía sostenida.
    fila(&text, &series, &eventos, &dias, n_tormentas, None, "solo pico");

    for pct in [70.0, 75.0, 80.0] {
        for min_days in [2usize, 3, 4] {
            let clim = MonthlyStormClimatology::from_series_at(&series, pct).unwrap();
            let p = Persistence::new(clim, min_days).unwrap();
            fila(
                &text,
                &series,
                &eventos,
                &dias,
                n_tormentas,
                Some(&p),
                &format!("p{pct:.0} × {min_days}d"),
            );
        }
    }

    println!(
        "\n   Cómo leer esto: bajar el percentil o los días sube 'detecta' y sube\n   \
         'marcados' a la vez. La pregunta no es cuál detecta más, sino cuál detecta\n   \
         sin marcar tanto que el índice deje de discriminar. Y el lead siempre es\n   \
         negativo: una racha necesita días para existir, así que la sub-vía sostenida\n   \
         confirma un temporal en curso — NO lo anticipa."
    );

    // --- por qué ninguna configuración detecta mucho -------------------------
    println!("\n\nPOR QUÉ NINGUNA CONFIGURACIÓN DETECTA MUCHO");
    println!(
        "   No es falta de datos: la serie cubre 23 de los 25 días de evento. Es que\n   \
         la mayoría de estos temporales NO FUERON CÁLIDOS.\n"
    );
    println!(
        "   {:<26}{:>8}{:>10}{:>10}{:>10}",
        "evento", "días", "H0 máx", "p50 mes", "p90 mes"
    );
    println!("   {}", "-".repeat(66));
    let p50 = MonthlyStormClimatology::from_series_at(&series, 50.0).unwrap();
    let p90 = MonthlyStormClimatology::from_series_at(&series, PICO_PCT).unwrap();
    for e in &eventos {
        let n = dias_entre(&e.inicio, &e.fin) + 1;
        let t = armar(&text, &series, &e.inicio, n, None);
        let mut mx: Option<f64> = None;
        let mut mes = 0;
        for step in 0..n {
            let s = t.signals(step);
            if let Some(h) = s.h0_m {
                mx = Some(mx.map_or(h, |m: f64| m.max(h)));
                mes = s.month;
            }
        }
        let f = |v: Option<f64>| v.map(|x| format!("{x:.0}")).unwrap_or("  --".into());
        let corto: String = e.nombre.chars().take(25).collect();
        println!(
            "   {corto:<26}{n:>8}{:>10}{:>10}{:>10}{}",
            f(mx),
            f(p50.p90(mes)),
            f(p90.p90(mes)),
            match (mx, p50.p90(mes), p90.p90(mes)) {
                (Some(h), Some(m), _) if h < m => "   bajo la mediana: FRÍO",
                (Some(h), _, Some(q)) if h >= q => "   sobre el p90: CÁLIDO",
                (Some(_), Some(_), Some(_)) => "   entre p50 y p90: tibio",
                _ => "   (mes sin climatología)",
            }
        );
    }
    println!(
        "\n   CONCLUSIÓN: H0 no es un detector de eventos mayores — es un discriminador\n   \
         de MECANISMO. Los cinco temporales de invierno tienen su H0 máximo ENTRE la\n   \
         mediana y el p90 de su propio mes: son TIBIOS, ninguno extremo. Por eso\n   \
         ningún percentil alto los marca — y bajar el umbral hasta alcanzarlos marca,\n   \
         por construcción, una fracción enorme de los días de tormenta (la mediana\n   \
         marcaría el 50 %). El único caso claramente cálido es el aluvión de verano,\n   \
         y su mes ni siquiera tiene climatología: lo cubre la vía absoluta.\n\n   \
         Es decir: este catálogo NO puede calibrar min_days, porque no está\n   \
         estratificado por mecanismo y casi no contiene el mecanismo que la vía\n   \
         anomalía busca. Elegir el parámetro por esta tabla sería ajustar ruido.\n   \
         Hace falta un inventario que distinga aluvión de crecida fría."
    );
}

fn fila(
    text: &str,
    series: &ZeroIsothermSeries,
    eventos: &[Evento],
    dias: &[(String, f64, bool)],
    n_tormentas: usize,
    persistencia: Option<&Persistence>,
    etiqueta: &str,
) {
    let (mut det, mut det_pleno, mut leads) = (0usize, 0usize, Vec::new());
    let mut det_full = 0usize;
    for e in eventos {
        if let Some(lead) = detecta(text, series, e, persistencia, false) {
            det += 1;
            if e.pleno {
                det_pleno += 1;
            }
            leads.push(lead);
        }
        if detecta(text, series, e, persistencia, true).is_some() {
            det_full += 1;
        }
    }
    // Tasa base sobre días de tormenta de toda la serie.
    let marcados = marcados_en_tormentas(text, series, persistencia, dias);
    let lead_txt = if leads.is_empty() {
        "  --".to_string()
    } else {
        format!("{:+.1} d", leads.iter().sum::<f64>() / leads.len() as f64)
    };
    println!(
        "   {etiqueta:<10}{:>9}{:>10}{:>10}{:>12}{:>9.1}%",
        format!("{det}/{}", eventos.len()),
        format!("{det_pleno}/{}", eventos.iter().filter(|e| e.pleno).count()),
        format!("{det_full}/{}", eventos.len()),
        lead_txt,
        100.0 * marcados as f64 / n_tormentas as f64
    );
    let _ = n_tormentas;
}

/// Días de lead del primer disparo respecto del inicio documentado, o `None` si
/// la vía anomalía no se activa en toda la ventana del evento.
fn detecta(
    text: &str,
    series: &ZeroIsothermSeries,
    e: &Evento,
    persistencia: Option<&Persistence>,
    cualquier_via: bool,
) -> Option<f64> {
    let n = dias_entre(&e.inicio, &e.fin) + 1;
    let t = armar(text, series, &e.inicio, n, persistencia);
    (0..n).find_map(|step| {
        let via = t.signals(step).via;
        let hit = if cualquier_via {
            matches!(via, H0Via::Anomalous | H0Via::Both | H0Via::Absolute)
        } else {
            matches!(via, H0Via::Anomalous | H0Via::Both)
        };
        hit.then_some(-(step as f64))
    })
}

fn marcados_en_tormentas(
    text: &str,
    series: &ZeroIsothermSeries,
    persistencia: Option<&Persistence>,
    dias: &[(String, f64, bool)],
) -> usize {
    let Some((primera, _, _)) = dias.first() else {
        return 0;
    };
    let ultima = &dias.last().unwrap().0;
    let n = dias_entre(primera, ultima) + 1;
    let t = armar(text, series, primera, n, persistencia);
    // Solo cuentan los días que además son de tormenta: el índice modula lluvia,
    // así que marcar un día seco no le cuesta nada al sistema.
    let tormenta: std::collections::HashSet<&str> = dias
        .iter()
        .filter(|d| d.2)
        .map(|d| d.0.as_str())
        .collect();
    (0..n)
        .filter(|&step| {
            let s = t.signals(step);
            matches!(s.via, H0Via::Anomalous | H0Via::Both)
                && s.h0_m.is_some()
                && tormenta.contains(fecha_de(primera, step).as_str())
        })
        .count()
}

fn armar(
    text: &str,
    _series: &ZeroIsothermSeries,
    inicio: &str,
    n: usize,
    persistencia: Option<&Persistence>,
) -> TwoWayH0Trigger {
    let s = ZeroIsothermSeries::from_csv(text).unwrap();
    let pico = MonthlyStormClimatology::from_series_at(&s, PICO_PCT).unwrap();
    let t = TwoWayH0Trigger::new(
        GridDims::new(1, 1),
        s,
        pico,
        AbsoluteWay::new(UMBRAL_ABS_M).unwrap(),
        inicio,
        24.0,
        n.max(1),
        TriggerModel::default(),
    )
    .unwrap();
    match persistencia {
        Some(p) => t.with_persistence(p.clone()),
        None => t,
    }
}

// --- utilidades de fecha (mismo algoritmo que el crate, sin exponerlo) -------

fn civil(s: &str) -> i64 {
    let (y, m, d) = (
        s[0..4].parse::<i64>().unwrap(),
        s[5..7].parse::<i64>().unwrap(),
        s[8..10].parse::<i64>().unwrap(),
    );
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = if y2 >= 0 { y2 } else { y2 - 399 } / 400;
    let yoe = y2 - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    era * 146097 + (yoe * 365 + yoe / 4 - yoe / 100 + doy) - 719468
}

fn dias_entre(a: &str, b: &str) -> usize {
    (civil(b) - civil(a)).max(0) as usize
}

fn fecha_de(inicio: &str, step: usize) -> String {
    let z = civil(inicio) + step as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}")
}

fn leer(p: &std::path::Path) -> String {
    std::fs::read_to_string(p).unwrap_or_else(|e| {
        eprintln!(
            "no pude leer {}: {e}\nGeneralo con: python3 scripts/extract_h0_transecta.py",
            p.display()
        );
        std::process::exit(1);
    })
}

fn eventos(text: &str) -> Vec<Evento> {
    let mut lines = text.lines();
    let header: Vec<&str> = lines.next().unwrap_or("").split(',').collect();
    let idx = |n: &str| header.iter().position(|c| c.trim() == n).unwrap();
    let (i_i, i_f, i_n) = (idx("fecha_inicio"), idx("fecha_fin"), idx("nombre"));
    let (i_t, i_c) = (idx("en_transecta"), idx("confianza_fecha"));
    lines
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            Evento {
                inicio: f[i_i].to_string(),
                fin: f[i_f].to_string(),
                nombre: f[i_n].to_string(),
                pleno: f[i_t] == "si",
                confianza: f[i_c].to_string(),
            }
        })
        .collect()
}

fn dias(text: &str) -> Vec<(String, f64, bool)> {
    let mut lines = text.lines();
    let header: Vec<&str> = lines.next().unwrap_or("").split(',').collect();
    let i = |n: &str| header.iter().position(|c| c.trim() == n);
    let (Some(i_f), Some(i_h)) = (i("fecha"), i("h0_m")) else {
        return Vec::new();
    };
    let i_t = i("tormenta");
    lines
        .filter_map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            let h0 = f.get(i_h)?.trim().parse::<f64>().ok()?;
            Some((
                f.get(i_f)?.to_string(),
                h0,
                i_t.and_then(|k| f.get(k)).map(|v| v.trim() == "1") == Some(true),
            ))
        })
        .collect()
}
