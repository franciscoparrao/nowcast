//! Runner del path de CRECIDAS para el ciclo operacional: JSON entra, JSON sale.
//!
//! POR QUÉ EXISTE (2026-08-17). El monitor vigila la costa de Atacama con el
//! gatillo I-D de deslizamientos, que mide intensidad de lluvia SOBRE LA CELDA.
//! Ese es el mecanismo equivocado ahí: el aluvión reportado en Puerto Viejo
//! (Caldera) ocurrió con 1.5 mm de lluvia satelital en su propia celda y ~30 mm
//! en la cabecera andina, a 100 km al este — agua RUTEADA, no falla de ladera
//! local. La causal oficial de SENAPRED para esa comuna era "Crecida". El canal
//! correcto ya estaba en el motor (`FloodThreshold`, excedencia Q/Q_c) pero solo
//! existía como *example* de análisis: imprimía texto humano y se configuraba por
//! variables de entorno. Esto es su gemelo operacional.
//!
//! POR QUÉ HORTONIANO Y NO GR4J. Dos razones, ambas medidas y no supuestas.
//! (1) GR4J necesita estado: sus almacenes de producción y ruteo acumulan años de
//! historia, así que un ciclo cada 30 min tendría que persistir estado o hacer
//! spin-up largo. El SCS-CN trata cada paso como evento independiente (ver la
//! nota de honestidad en `hortonian::HortonianParams::escorrentia_mm`), así que
//! NO necesita estado — es lo único de los dos que es viable operacionalmente.
//! (2) GR4J ya dio un falso negativo documentado acá: en el Huasco de marzo 2015
//! lo dejó en el percentil 88.7 mientras el Hortoniano lo puso en 99.75, y el
//! antecedente hidráulico (Garcés et al., citando a Gómez et al. 2017: 63 m³/s en
//! el río El Carmen, un orden de magnitud sobre lo normal) confirmó que la
//! respuesta ruteada SÍ fue extrema. El almacén de exceso de saturación no
//! genera escorrentía con 20 mm/día sobre suelo seco, que es el modo de falla
//! que motivó escribir la capa Hortoniana.
//!
//! ETIQUETA DE HONESTIDAD, no negociable en la salida. No hay aforo en estas
//! cuencas (CAMELS-CL no trae estación de Atacama en este disco), así que Q_c se
//! deriva de un cuantil de la serie **SIMULADA**: la excedencia significa "cuán
//! inusual es esta respuesta para lo que ESTE modelo produce en ESTA cuenca", no
//! una magnitud absoluta. El JSON lo dice en `q_crit_origen` para que ningún
//! consumidor pueda perder el rótulo por el camino.
//!
//! Uso:
//!   nowcast-flood --forzante <f.json> --cn 88 --tc-h 13 --q-crit-p 0.99
//!   nowcast-flood --forzante <vivo.json> --cn 88 --tc-h 13 --q-crit 4.21
//!
//! El primer modo (derivar Q_c de la serie) es el de preparación offline sobre la
//! serie histórica; el segundo (Q_c dado) es el del ciclo vivo, que corre sobre
//! una serie corta y compara contra el umbral ya persistido. Un solo binario para
//! los dos usos, para que la definición de Q_c no pueda divergir entre ellos.

use nowcast_rainflow::hortonian::escorrentia_ruteada;
use nowcast_rainflow::{FloodThreshold, HortonianParams, LAMBDA_ARIDO, LAMBDA_CLASICO};

/// Extrae un arreglo JSON de números por nombre de clave. El crate no depende de
/// serde a propósito (ver Cargo.toml) y la entrada son arreglos planos, así que
/// un parse mínimo alcanza; falla ruidosamente si la clave no está.
fn arreglo_num(texto: &str, clave: &str) -> Option<Vec<f64>> {
    let pat = format!("\"{clave}\"");
    let i = texto.find(&pat)?;
    let ini = texto[i..].find('[')? + i + 1;
    let fin = texto[ini..].find(']')? + ini;
    Some(
        texto[ini..fin]
            .split(',')
            .filter_map(|x| x.trim().parse::<f64>().ok())
            .collect(),
    )
}

/// Igual que [`arreglo_num`] pero para arreglos de strings (las etiquetas de paso).
fn arreglo_str(texto: &str, clave: &str) -> Option<Vec<String>> {
    let pat = format!("\"{clave}\"");
    let i = texto.find(&pat)?;
    let ini = texto[i..].find('[')? + i + 1;
    let fin = texto[ini..].find(']')? + ini;
    Some(
        texto[ini..fin]
            .split(',')
            .map(|x| x.trim().trim_matches('"').to_string())
            .filter(|x| !x.is_empty())
            .collect(),
    )
}

fn escalar(texto: &str, clave: &str) -> Option<f64> {
    let pat = format!("\"{clave}\"");
    let i = texto.find(&pat)?;
    let resto = &texto[i + pat.len()..];
    let j = resto.find(':')? + 1;
    let fin = resto[j..]
        .find(|c: char| c == ',' || c == '}' || c == '\n')
        .map(|k| k + j)
        .unwrap_or(resto.len());
    resto[j..fin].trim().parse::<f64>().ok()
}

fn arg(nombre: &str) -> Option<String> {
    let a: Vec<String> = std::env::args().collect();
    a.iter().position(|x| x == nombre).and_then(|i| a.get(i + 1).cloned())
}

fn arg_f64(nombre: &str) -> Option<f64> {
    arg(nombre).and_then(|v| v.parse().ok())
}

fn morir(msg: &str) -> ! {
    eprintln!("nowcast-flood: {msg}");
    std::process::exit(1);
}

fn main() {
    let forzante = arg("--forzante").unwrap_or_else(|| morir("falta --forzante <archivo.json>"));
    let texto = std::fs::read_to_string(&forzante)
        .unwrap_or_else(|e| morir(&format!("no se pudo leer {forzante}: {e}")));

    let pr = arreglo_num(&texto, "pr")
        .unwrap_or_else(|| morir(&format!("{forzante} no tiene el arreglo \"pr\"")));
    if pr.is_empty() {
        morir("el arreglo \"pr\" está vacío");
    }
    if pr.iter().any(|p| !p.is_finite() || *p < 0.0) {
        // Entradas hostiles: NaN o lluvia negativa producirían un caudal
        // plausible y silenciosamente falso. Se rechazan acá, no río abajo.
        morir("\"pr\" tiene valores no finitos o negativos");
    }
    let etiquetas = arreglo_str(&texto, "fechas").unwrap_or_default();

    // dt del forzante: diario por default porque así se derivó el Q_c histórico
    // (CR2MET es diario) y así se hizo la comparación event-céntrica del Huasco.
    // Mezclar un dt vivo distinto del dt del umbral compararía peras con manzanas.
    let dt_dias = arg_f64("--dt-dias")
        .or_else(|| escalar(&texto, "dt_dias"))
        .unwrap_or(1.0);

    let cn = arg_f64("--cn").unwrap_or_else(|| morir("falta --cn (30-100)"));
    let tc_h = arg_f64("--tc-h").unwrap_or_else(|| morir("falta --tc-h (horas)"));
    let lambda = match arg("--lambda").as_deref() {
        None | Some("arido") => LAMBDA_ARIDO,
        Some("clasico") => LAMBDA_CLASICO,
        Some(otro) => morir(&format!("--lambda debe ser arido|clasico, se recibió {otro}")),
    };
    let params = HortonianParams { cn, tc_horas: tc_h, lambda };
    if let Err(e) = params.validar() {
        morir(&format!("parámetros Hortonianos inválidos: {e}"));
    }

    let q = escorrentia_ruteada(&pr, &params, dt_dias)
        .unwrap_or_else(|e| morir(&format!("ruteo falló: {e}")));

    // Q_c: derivado de la serie (uso offline) o dado (uso vivo). Nunca ambos.
    let (thr, origen) = match (arg_f64("--q-crit"), arg_f64("--q-crit-p")) {
        (Some(_), Some(_)) => morir("--q-crit y --q-crit-p son mutuamente excluyentes"),
        (Some(qc), None) => (
            FloodThreshold::new(qc).unwrap_or_else(|e| morir(&format!("--q-crit inválido: {e}"))),
            "dado (persistido de la corrida historica; cuantil de serie SIMULADA, sin aforo)"
                .to_string(),
        ),
        (None, Some(p)) => (
            FloodThreshold::quantile(&q, p)
                .unwrap_or_else(|e| morir(&format!("--q-crit-p inválido: {e}"))),
            format!("cuantil p={p} de esta serie SIMULADA (sin aforo: no es magnitud absoluta)"),
        ),
        (None, None) => morir("falta --q-crit <mm/dia> o --q-crit-p <p>"),
    };

    // Excedencia y su máximo. El paso del máximo es lo que el ciclo reporta.
    let mut e_max = f64::NEG_INFINITY;
    let mut i_max = 0usize;
    for (i, &qi) in q.iter().enumerate() {
        let e = thr.exceedance(qi);
        if e > e_max {
            e_max = e;
            i_max = i;
        }
    }
    let etiqueta_max = etiquetas.get(i_max).cloned().unwrap_or_else(|| i_max.to_string());
    // Rango del máximo dentro de la serie (1 = el mayor). Es la lectura
    // event-céntrica que el proyecto usa cuando no hay aforo contra el cual medir.
    let mayores = q.iter().filter(|&&x| x > q[i_max]).count() + 1;

    let esc = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"");
    println!("{{");
    println!("  \"forzante\": \"{}\",", esc(&forzante));
    println!("  \"modelo\": \"hortoniano-scs-cn\",");
    println!("  \"cn\": {cn}, \"tc_horas\": {tc_h}, \"lambda\": {lambda},");
    println!("  \"dt_dias\": {dt_dias}, \"n_steps\": {},", q.len());
    println!("  \"q_crit\": {:.6},", thr.q_crit);
    println!("  \"q_crit_origen\": \"{}\",", esc(&origen));
    println!("  \"q_max\": {:.6},", q[i_max]);
    println!("  \"e_max\": {e_max:.6},");
    println!("  \"paso_max\": {i_max}, \"etiqueta_max\": \"{}\",", esc(&etiqueta_max));
    println!("  \"rango_max\": {mayores},");
    println!("  \"excede\": {},", e_max >= 1.0);
    println!("  \"q\": [{}]", q.iter().map(|x| format!("{x:.6}")).collect::<Vec<_>>().join(","));
    println!("}}");
}
