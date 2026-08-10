//! Path de crecidas para el Río Salado (Chañaral, Atacama) — y la pregunta de
//! si GR4J puede siquiera representar una cuenca hiperárida.
//!
//! ## Por qué este ejemplo
//!
//! El monitor vigila Chañaral con el gatillo I–D de deslizamientos, que es el
//! mecanismo equivocado: lo que destruyó el pueblo en marzo de 2015 fue una
//! crecida hiperconcentrada del Salado —un main-stem que integra lluvia caída a
//! ~100 km en la cordillera— no la falla de una ladera local. El canal correcto
//! es exceedancia de caudal Q/Q_c, que es lo que hace este crate.
//!
//! ## x4 se ESTIMA, no se calibra
//!
//! El desfase de 2-3 días de la primera versión no era una falla intrínseca de
//! GR4J: era `x4=3.5 d` elegido a ojo. `x4` es el tiempo base del hidrograma
//! unitario y se estima FÍSICAMENTE del DEM, que es lo estándar en cuencas sin
//! aforo. Sobre la geometría derivada por D8 (cauce principal 196 km, desnivel
//! 4.395 m, pendiente 0.0224) el tiempo de concentración da 16.6 h por Kirpich
//! y 12.0 h por Giandotti, así que x4 ~ 1.75·tc ~ 1 día. Con ese valor —
//! calculado ANTES de mirar el resultado, no ajustado contra él— el pico
//! simulado cae el 2015-03-25, el día exacto del aluvión. El rango 1/10.958 se
//! mantiene para todo x4 entre 0.5 y 3.5 d, así que la detección es robusta al
//! parámetro y solo el TIMING depende de él.
//!
//! ## Caso Huasco marzo 2015: por qué GR4J falla ahí
//!
//! Los dos paths de producción discrepan en ese caso —GR4J lo deja en el rango
//! 1238/10958 (percentil 88.7) y el Hortoniano en el 27 (99.75)— y la duda era
//! cuál acierta. Se resolvió con antecedentes publicados, no con criterio:
//!
//!   * Garcés et al. (SOCHID XXIV, AMTC U. de Chile), citando la modelación de
//!     Gómez et al. (2017): los valles de Atacama "se caracterizan por tener
//!     cauces con caudales de unos pocos m3/s", y durante el 25M el río El
//!     Carmen, antes de la confluencia con El Tránsito, alcanzó un caudal
//!     máximo de **63 m3/s** — un orden de magnitud sobre lo normal.
//!   * El mismo trabajo describe el mecanismo como ACOPLADO: ondas de crecida
//!     con alta carga de sedimento, más aportes de las quebradas laterales que
//!     TAPONAN y avulsionan el cauce principal, inundando aguas arriba.
//!
//! Es decir, la dicotomía que yo había planteado —"fue quebrada, no crecida de
//! main-stem, así que el path de crecidas correctamente no se dispara"— era
//! FALSA: ocurrieron las dos cosas y estaban acopladas. La respuesta ruteada de
//! la cuenca SÍ fue extrema, así que el rango 27 del Hortoniano es señal y el
//! 1238 de GR4J era un falso negativo. La lección de método: no cerrar un caso
//! ambiguo con la explicación que favorece al modelo propio; ir a buscar el
//! antecedente.
//!
//! ## La prueba que este ejemplo ejecuta
//!
//! NO hay caudal observado del Salado (CAMELS-CL no trae estación de Atacama),
//! así que GR4J no se puede calibrar y no hay NSE/KGE que reportar. Pero sí hay
//! una **fecha de verdad conocida**: el aluvión del 25-26 de marzo de 2015. La
//! pregunta operacional es binaria y se responde sin aforo:
//!
//!   ¿el caudal simulado alcanza su máximo (o cerca) en esa fecha?
//!
//! Si sí, el modelo captura el evento aunque esté sin calibrar y el path sirve
//! como vigilancia relativa. Si NO —si produce runoff ~0 porque el almacén de
//! producción nunca se llena en un desierto con 41 mm/año de lluvia contra 957
//! de PET— entonces GR4J está fuera de su régimen y **hay que decirlo en vez de
//! desplegar un número que parece un pronóstico**. Ese resultado negativo es
//! tan entregable como el positivo.
//!
//! Forzante: `monitor/flood_salado.py` delinea la cuenca por D8 sobre la
//! hidrología de KINGSTON (7.249 km² aportantes a la desembocadura) y promedia
//! CR2MET diario sobre ella; PET por Oudin desde WorldClim bio_01.
//!
//! Correr con:
//!   python3 monitor/flood_salado.py --anios 1990 2021
//!   cargo run -p nowcast-rainflow --example salado_flood

use nowcast_core::{GridDims, SusceptibilityMap, TriggerModel};
use nowcast_rainflow::{FloodNowcast, FloodThreshold, HortonianParams, RainflowForcing, LAMBDA_ARIDO};
use rainflow_core::Gr4jParams;
use std::path::PathBuf;

// Parametrizable para poder repetir el método en otra cuenca sin duplicar el
// ejemplo: SALADO_FORZANTE / SALADO_EVENTO / SALADO_X4.
const FORZANTE_DEFAULT: &str = "~/nowcast-monitor-atacama/salado_flood_forzante.json";
const QUANTILE: f64 = 0.995; // Q_c = cuantil de la serie SIMULADA
const ALERT_LEVEL: f64 = 0.5;
const EVENTO_DEFAULT: &str = "2015-03"; // aluvión de Chañaral, 25-26 mar 2015

/// Lee el JSON que produce `monitor/flood_salado.py` sin dependencias extra:
/// son tres arreglos planos y un par de escalares, no vale la pena serde_json
/// en un ejemplo.
fn leer_forzante() -> (Vec<String>, Vec<f64>, Vec<f64>, f64) {
    let forzante = std::env::var("SALADO_FORZANTE").unwrap_or_else(|_| FORZANTE_DEFAULT.to_string());
    let path = PathBuf::from(forzante.replacen('~', env!("HOME"), 1));
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "no pude leer {} ({e}).\nCorrer primero: python3 monitor/flood_salado.py --anios 1990 2021",
            path.display()
        )
    });
    let campo = |nombre: &str| -> String {
        let key = format!("\"{nombre}\":");
        let i = text.find(&key).unwrap_or_else(|| panic!("falta {nombre} en el JSON"));
        let resto = &text[i + key.len()..];
        let ini = resto.find('[').unwrap_or_else(|| panic!("{nombre} no es arreglo"));
        let fin = resto.find(']').unwrap();
        resto[ini + 1..fin].to_string()
    };
    let fechas: Vec<String> = campo("fechas")
        .split(',')
        .map(|s| s.trim().trim_matches('"').to_string())
        .collect();
    let num = |s: String| -> Vec<f64> {
        s.split(',').filter_map(|x| x.trim().parse::<f64>().ok()).collect()
    };
    let pr = num(campo("pr"));
    let pet = num(campo("pet"));
    let area = {
        let key = "\"area_km2\":";
        let i = text.find(key).expect("falta area_km2");
        let resto = &text[i + key.len()..];
        let fin = resto.find([',', '}']).unwrap();
        resto[..fin].trim().parse::<f64>().unwrap_or(f64::NAN)
    };
    assert_eq!(fechas.len(), pr.len(), "fechas y pr descuadrados");
    assert_eq!(pr.len(), pet.len(), "pr y pet descuadrados");
    (fechas, pr, pet, area)
}

fn main() {
    let evento = std::env::var("SALADO_EVENTO").unwrap_or_else(|_| EVENTO_DEFAULT.to_string());
    let (fechas, pr, pet, area) = leer_forzante();
    println!(
        "cuenca {area:.0} km² — {} días ({}..{})",
        pr.len(),
        fechas.first().unwrap(),
        fechas.last().unwrap()
    );
    let mm_anual = pr.iter().sum::<f64>() / (pr.len() as f64) * 365.0;
    let pet_anual = pet.iter().sum::<f64>() / (pet.len() as f64) * 365.0;
    // El índice de aridez solo tiene sentido sobre varios años completos:
    // anualizar una ventana de dos meses de invierno da un número absurdo.
    if pr.len() >= 3 * 365 {
        println!(
            "  clima: {mm_anual:.0} mm/año de lluvia contra {pet_anual:.0} de PET \
             (índice de aridez {:.3})",
            mm_anual / pet_anual
        );
    } else {
        println!(
            "  ventana corta ({} días): lluvia total {:.0} mm — sin índice de aridez \
             (necesita años completos)",
            pr.len(),
            pr.iter().sum::<f64>()
        );
    }

    // Parámetros GR4J plausibles para cuenca grande y seca. NO calibrados: sin
    // caudal observado no hay contra qué. x1 alto (almacén profundo, regolito),
    // x4 largo (7.249 km²: el hidrograma tarda días en concentrar).
    // x4 ESTIMADO FÍSICAMENTE del DEM, no ajustado: el tiempo de concentración de
    // la cuenca (Kirpich 16.6 h, Giandotti 12.0 h sobre un cauce principal de
    // 196 km con 4.395 m de desnivel) da tc ~0.5-0.7 d, y x4 —tiempo base del
    // hidrograma unitario— es ~1.75x tc. El 3.5 d de la primera versión era
    // 3-4x demasiado largo y por eso el pico salía 2-3 días tarde.
    let x4: f64 = std::env::var("SALADO_X4").ok().and_then(|v| v.parse().ok()).unwrap_or(1.0); // días — estimación física, ver cabecera
    let params = Gr4jParams { x1: 350.0, x2: -1.0, x3: 40.0, x4 };
    println!(
        "  GR4J SIN CALIBRAR (x1={}, x2={}, x3={}, x4={}) — no hay aforo del Salado",
        params.x1, params.x2, params.x3, params.x4
    );

    let dims = GridDims::new(1, 1);
    // SALADO_MODELO=hortoniano cambia la capa de PRODUCCIÓN (SCS-CN + UH
    // triangular) manteniendo todo lo demás igual: misma forzante, mismo
    // criterio de umbral, misma validación. Es la comparación controlada.
    let modelo = std::env::var("SALADO_MODELO").unwrap_or_else(|_| "gr4j".to_string());
    let forcing = if modelo == "hortoniano" {
        let cn: f64 = std::env::var("SALADO_CN").ok().and_then(|v| v.parse().ok()).unwrap_or(88.0);
        let tc_h: f64 = std::env::var("SALADO_TC_H").ok().and_then(|v| v.parse().ok()).unwrap_or(13.0);
        println!("  HORTONIANO (SCS-CN cn={cn}, tc={tc_h} h, lambda={LAMBDA_ARIDO}) — todo estimado, nada ajustado");
        let hp = HortonianParams { cn, tc_horas: tc_h, lambda: LAMBDA_ARIDO };
        RainflowForcing::hortoniana(dims, 1.0, hp, &pr).expect("producción Hortoniana falló")
    } else {
        RainflowForcing::gr4j(dims, 1.0, params, &pr, &pet).expect("GR4J falló")
    };
    let q_bruto = forcing.discharge().to_vec();

    // GR4J arranca con los almacenes en un estado por defecto, así que los
    // primeros meses son transitorio de spin-up, no hidrología. Sin descartarlo
    // el "máximo de la serie" cae en el día 1 —lo que pasó en la primera
    // corrida de este ejemplo— y contamina cualquier ranking del evento.
    // Parametrizable y acotado: con una ventana corta (p. ej. dos meses de
    // IMERG para un evento reciente) no se pueden descartar 2 años. Nunca más
    // de un tercio de la serie, para no quedarse sin registro que rankear.
    let spinup_pedido: usize = std::env::var("SALADO_SPINUP")
        .ok().and_then(|v| v.parse().ok()).unwrap_or(730);
    let spinup = spinup_pedido.min(fechas.len() / 3);
    let fechas: Vec<String> = fechas[spinup..].to_vec();
    let q: Vec<f64> = q_bruto[spinup..].to_vec();
    println!(
        "  descartados {spinup} días de spin-up (arranca en {}); quedan {} días",
        fechas.first().unwrap(),
        q.len()
    );

    let qmax = q.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let qsum: f64 = q.iter().sum();
    let ndias_con_q = q.iter().filter(|&&x| x > 0.01).count();
    println!(
        "\ncaudal simulado: máx {qmax:.3} mm/día · media {:.4} · días con Q>0.01 mm: {ndias_con_q} \
         ({:.1}%)",
        qsum / q.len() as f64,
        100.0 * ndias_con_q as f64 / q.len() as f64
    );

    // --- LA PRUEBA: ¿el máximo cae en el evento conocido? --------------------
    let (mut i_max, mut v_max) = (0usize, f64::NEG_INFINITY);
    for (i, &v) in q.iter().enumerate() {
        if v > v_max {
            v_max = v;
            i_max = i;
        }
    }
    println!("  máximo de la serie: {v_max:.3} mm/día el {}", fechas[i_max]);

    let mut q_evento: Vec<(String, f64)> = fechas
        .iter()
        .zip(q.iter())
        .filter(|(f, _)| f.starts_with(evento.as_str()))
        .map(|(f, &v)| (f.clone(), v))
        .collect();
    q_evento.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
    if let Some((fecha, valor)) = q_evento.first() {
        let rango = q.iter().filter(|&&x| x > *valor).count() + 1;
        let pct = 100.0 * (1.0 - rango as f64 / q.len() as f64);
        println!(
            "  máximo de {evento} (evento conocido): {valor:.3} mm/día el {fecha} \
             → rango {rango} de {} (percentil {pct:.2})",
            q.len()
        );
        // El test de RANGO solo significa algo con un registro largo: sobre una
        // ventana de pocas semanas que contiene el evento, "rango 1" es trivial.
        let registro_util = q.len() >= 3 * 365;
        let captura = rango <= 10;
        if registro_util {
            println!(
                "\n  VEREDICTO (rango, registro de {:.0} años): {}",
                q.len() as f64 / 365.0,
                if captura {
                    "el modelo pone el evento entre los 10 caudales más altos del \
                     registro — lo captura sin calibrar"
                } else {
                    "el modelo NO destaca el evento en el registro. Antes de concluir que \
                     el mecanismo no era de crecida, hay que VERIFICARLO con antecedentes: \
                     en el Huasco 2015 la dicotomía 'quebrada vs main-stem' resultó FALSA \
                     (ver cabecera) y el falso negativo era del modelo, no del mecanismo"
                }
            );
        } else {
            println!(
                "\n  VEREDICTO: registro de solo {} días — el rango NO es informativo \
                 (con una ventana que contiene el evento, rango 1 es trivial). \
                 Lo que este caso prueba es el TIMING: pico simulado el {fecha}.",
                q.len()
            );
        }
    }

    // --- El path completo, para dejarlo instanciado --------------------------
    // El nowcast se arma sobre la MISMA serie post-spin-up de la que sale Q_c;
    // usar el forcing completo (con el transitorio) contra un umbral derivado
    // de la serie recortada mezcla dos poblaciones y sobrecuenta las alertas.
    let forcing_post = RainflowForcing::new(dims, 1.0, q.clone()).expect("forzante post-spin-up");
    let thr = FloodThreshold::quantile(&q, QUANTILE).expect("umbral de caudal");
    let susc = SusceptibilityMap::uniform(dims, 1.0).expect("susceptibilidad");
    let trigger = TriggerModel::new(4.0).expect("trigger");
    let nc = FloodNowcast::from_rainflow(susc, &forcing_post, thr, trigger).expect("flood nowcast");
    let alerts = nc.alerts(ALERT_LEVEL).expect("alertas");
    println!(
        "\npath instanciado: Q_c = cuantil {QUANTILE} de la serie simulada · \
         {} paso(s) de {} sobre nivel de alerta {ALERT_LEVEL}",
        alerts.len(),
        q.len()
    );
    println!(
        "  (Q_c se deriva de la serie SIMULADA, no de aforo: la exceedancia dice \
         'cuán inusual para este modelo', no una magnitud absoluta)"
    );
}
