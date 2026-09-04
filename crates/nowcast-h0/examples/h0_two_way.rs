//! El índice H0 de dos vías sobre la serie REAL de la transecta 30-36 °S.
//!
//! Hace tres cosas, en orden de lo que hay que creerle:
//!
//! 1. **Reproduce la climatología del reporte río arriba** (bloque B de
//!    `analisis/h0_vs_eventos.txt`). El p90 mensual se recalcula acá desde la
//!    serie en vez de leerse de un `.txt`, así que una divergencia entre los dos
//!    repos aparece como un FALLO impreso y no como un desacuerdo silencioso.
//! 2. **Corre los dos casos testigo** por los que el índice tiene dos vías:
//!    el aluvión de verano (2021-01-30) y el temporal de invierno (2023-06-21/26).
//! 3. **Muestra por qué esto MODULA la lluvia y no vota junto a ella**: cuántos
//!    días de verano cruzan el umbral absoluto por sí solos.
//!
//! Datos: `python3 scripts/extract_h0_transecta.py` (no versionados).
//! Correr: `cargo run -p nowcast-h0 --example h0_two_way`

use std::path::PathBuf;

use nowcast_core::{GridDims, Trigger, TriggerModel};
use nowcast_h0::{
    AbsoluteWay, H0Via, MissingPolicy, MonthlyStormClimatology, Persistence, TwoWayH0Trigger,
    ZeroIsothermSeries,
};

/// Bloque B del reporte: (mes, n de tormentas, p90). `None` = el reporte dice
/// `(n<8)`. Copiado a mano desde `h0_vs_eventos.txt` — es la referencia contra
/// la que se verifica, así que NO se deriva de la misma fuente que se compara.
const REPORTE_B: [(usize, &str, usize, Option<f64>); 12] = [
    (1, "ene", 4, None),
    (2, "feb", 0, None),
    (3, "mar", 11, Some(4633.0)),
    (4, "abr", 22, Some(4309.0)),
    (5, "may", 43, Some(4006.0)),
    (6, "jun", 88, Some(3802.0)),
    (7, "jul", 58, Some(3639.0)),
    (8, "ago", 67, Some(3393.0)),
    (9, "sep", 38, Some(4009.0)),
    (10, "oct", 13, Some(4328.0)),
    (11, "nov", 11, Some(4904.0)),
    (12, "dic", 1, None),
];

const UMBRAL_ABS_M: f64 = 4500.0;
/// Días consecutivos sobre el p75 mensual que definen una anomalía SOSTENIDA.
const MIN_DIAS: usize = 3;

fn main() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data/h0_centro_diaria.csv");
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!(
                "no pude leer {}: {e}\nGeneralo con: python3 scripts/extract_h0_transecta.py",
                path.display()
            );
            std::process::exit(1);
        }
    };
    let series = ZeroIsothermSeries::from_csv(&text).expect("serie H0 mal formada");
    let clim = MonthlyStormClimatology::from_series(&series);
    println!("serie H0 30-36 °S: {} días con dato\n", series.len());

    // --- 1. ¿coincide con el reporte de río arriba? -------------------------
    println!("1. CLIMATOLOGÍA MENSUAL DE TORMENTAS vs el reporte (bloque B)");
    println!("   {:<5}{:>6}{:>10}{:>10}", "mes", "n", "p90 acá", "reporte");
    let mut fallos = 0;
    for (m, nombre, n_rep, p90_rep) in REPORTE_B {
        let p90 = clim.p90(m);
        let n = clim.n_storm_days(m);
        // Tolerancia 1 m: el reporte imprime con %.0f, así que solo el redondeo
        // puede separarlos legítimamente.
        let ok = match (p90, p90_rep) {
            (None, None) => true,
            (Some(a), Some(b)) => (a - b).abs() <= 1.0,
            _ => false,
        };
        if !ok || n != n_rep {
            fallos += 1;
        }
        let f = |v: Option<f64>| match v {
            Some(x) => format!("{x:.0}"),
            None => "(n<8)".to_string(),
        };
        println!(
            "   {nombre:<5}{n:>6}{:>10}{:>10}   {}{}",
            f(p90),
            f(p90_rep),
            if ok { "✓" } else { "✗ DIVERGE" },
            if n == n_rep {
                String::new()
            } else {
                format!("  n del reporte: {n_rep}")
            }
        );
    }
    println!(
        "   → {}\n",
        if fallos == 0 {
            "el motor reproduce el reporte mes a mes".to_string()
        } else {
            format!("{fallos} meses DIVERGEN — revisar antes de usar esto")
        }
    );

    // --- 2. los dos casos testigo -------------------------------------------
    println!("2. LOS DOS CASOS TESTIGO (umbral absoluto {UMBRAL_ABS_M:.0} m)");
    caso(&text, &clim, None, "aluvión San José de Maipo", "2021-01-29", 3);
    caso(
        &text,
        &clim,
        None,
        "temporal río atmosférico [solo pico, p90]",
        "2023-06-21",
        6,
    );
    println!(
        "   ⚠ El pico a p90 NO ve el caso testigo de invierno: el temporal está en\n\
        \x20    p66-p84 de las tormentas de junio (medido: 65.9, 72.7, 78.4, 80.7, 83.0,\n\
        \x20    84.1) y el p90 de junio es 3802 m, por encima de los seis días. Pero el\n\
        \x20    evento se sostiene: lo que lo describe es la DURACIÓN a un percentil\n\
        \x20    moderado, no la altura a uno alto. Con la sub-vía sostenida (p75, {MIN_DIAS} días):\n"
    );
    let clim75 = MonthlyStormClimatology::from_series_at(
        &ZeroIsothermSeries::from_csv(&text).unwrap(),
        75.0,
    )
    .unwrap();
    let persistencia = Persistence::new(clim75, MIN_DIAS).unwrap();
    caso(
        &text,
        &clim,
        Some(&persistencia),
        "temporal río atmosférico [pico p90 ⊕ sostenido p75]",
        "2023-06-21",
        6,
    );
    println!(
        "   → la sub-vía sostenida AGREGA una ruta, no baja el listón del pico: un solo\n\
        \x20    día a p80 sigue sin ser alerta; {MIN_DIAS} días seguidos a p78 ahora sí lo son.\n"
    );

    // --- 3. por qué modula y no vota ----------------------------------------
    println!("3. POR QUÉ ESTO MODULA LA LLUVIA Y NO VOTA JUNTO A ELLA");
    for umbral in [4000.0, 4500.0] {
        let (mut verano, mut verano_tot) = (0usize, 0usize);
        for (fecha, h0, _) in dias(&text) {
            let mes = &fecha[5..7];
            if matches!(mes, "01" | "02" | "12") {
                verano_tot += 1;
                if h0 >= umbral {
                    verano += 1;
                }
            }
        }
        println!(
            "   umbral {umbral:.0} m → {verano}/{verano_tot} días de dic-ene-feb lo cruzan \
             ({:.0} %)",
            100.0 * verano as f64 / verano_tot as f64
        );
    }
    println!(
        "   → fusionar esto con la lluvia por noisy-OR daría alerta casi todos los días\n\
        \x20    de verano. Va por Combine::Product: solo puede amplificar un gatillo que la\n\
        \x20    lluvia ya sostiene. Y por eso un hueco vale 1.0 (neutro), no 0.0."
    );
}

/// Días `(fecha, h0, tormenta)` del CSV, para los conteos crudos.
fn dias(text: &str) -> Vec<(String, f64, bool)> {
    let mut lines = text.lines();
    let header: Vec<&str> = lines.next().unwrap_or("").split(',').collect();
    let i = |n: &str| header.iter().position(|c| c.trim() == n);
    let (i_f, i_h, i_t) = (i("fecha"), i("h0_m"), i("tormenta"));
    let (Some(i_f), Some(i_h)) = (i_f, i_h) else {
        return Vec::new();
    };
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

fn caso(
    text: &str,
    clim: &MonthlyStormClimatology,
    persistencia: Option<&Persistence>,
    nombre: &str,
    inicio: &str,
    n_dias: usize,
) {
    let series = ZeroIsothermSeries::from_csv(text).unwrap();
    let t = TwoWayH0Trigger::new(
        GridDims::new(1, 1),
        series,
        clim.clone(),
        AbsoluteWay::new(UMBRAL_ABS_M).unwrap(),
        inicio,
        24.0,
        n_dias,
        TriggerModel::default(),
    )
    .unwrap()
    .with_missing_policy(MissingPolicy::Neutral);
    let t = match persistencia {
        Some(p) => t.with_persistence(p.clone()),
        None => t,
    };

    println!("   {nombre} — desde {inicio}");
    for step in 0..n_dias {
        let s = t.signals(step);
        let h0 = s
            .h0_m
            .map(|v| format!("{v:6.0} m"))
            .unwrap_or_else(|| "  (sin dato)".to_string());
        let p90 = s
            .month_p90_m
            .map(|v| format!("{v:6.0} m"))
            .unwrap_or_else(|| " indef.".to_string());
        let via = match s.via {
            H0Via::Missing => "HUECO — no sé, no 'baja'",
            H0Via::Quiet => "ninguna vía",
            H0Via::Absolute => "VÍA ABSOLUTA (patrón de verano)",
            H0Via::Anomalous => "VÍA ANOMALÍA (tormenta cálida)",
            H0Via::Both => "AMBAS VÍAS",
        };
        let fmt = |v: Option<f64>| {
            v.map(|x| format!("{x:.2}"))
                .unwrap_or_else(|| "  --".to_string())
        };
        let racha = if s.run_days > 0 {
            format!(" racha {}d", s.run_days)
        } else {
            String::new()
        };
        println!(
            "     +{step}d  H0 {h0}   p90 mes {p90}   abs {}  pico {}  sost {}{racha}   {via}",
            fmt(s.absolute),
            fmt(s.anomaly_spike),
            fmt(s.anomaly_sustained),
        );
    }
    // El factor fusionado es lo que ve MultiNowcast; las señales de arriba son
    // lo que debe ver el operador.
    let pico = (0..n_dias)
        .map(|s| t.factor(0, s))
        .fold(f64::NEG_INFINITY, f64::max);
    println!("     factor fusionado máximo: {pico:.2}\n");
}
