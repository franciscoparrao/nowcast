//! Producción de escorrentía por **exceso de infiltración** (Hortoniana).
//!
//! GR4J —y HBV, y casi todo modelo conceptual de uso común— genera escorrentía
//! por *exceso de saturación*: la lluvia llena un almacén de producción y lo que
//! desborda escurre. Ese es el mecanismo dominante en cuencas húmedas y
//! templadas con suelo desarrollado y vegetación.
//!
//! En una cuenca hiperárida el mecanismo es el otro. Sobre regolito desnudo,
//! costras superficiales y roca, la escorrentía se genera cuando la
//! **intensidad de la lluvia supera la capacidad de infiltración** del suelo,
//! sin necesidad de que nada se sature —el perfil puede estar seco a un
//! centímetro de profundidad mientras el agua corre por encima. Es la
//! escorrentía Hortoniana (Horton, 1933), y produce respuestas relámpago: el
//! hidrograma sube en minutos-horas, no en días.
//!
//! Esto importa operacionalmente, no es un detalle académico. Un almacén de
//! producción que nunca se llena —41 mm/año de lluvia contra 957 de
//! evapotranspiración potencial en el Salado— hace que un modelo de exceso de
//! saturación produzca escorrentía ~0 casi siempre y responda tarde y de menos
//! cuando responde.
//!
//! ## Qué implementa este módulo
//!
//! El par canónico del SCS (hoy NRCS), que se diseñó junto y para exactamente
//! este caso —cuencas sin aforo donde hay que estimar, no calibrar:
//!
//! 1. **Producción SCS-CN** (Curve Number). La escorrentía acumulada de un
//!    evento es
//!    ```text
//!    Q = (P − Ia)² / (P − Ia + S)      si P > Ia,   0 si no
//!    S = 25400/CN − 254   (mm)         Ia = λ·S
//!    ```
//!    `CN` resume suelo + cobertura + condición antecedente en un número
//!    ESTIMABLE de propiedades físicas (tablas NRCS), no ajustable a un
//!    hidrograma que no tenemos. `λ` es la fracción de abstracción inicial:
//!    0.2 en la formulación clásica, ~0.05 en las revisiones modernas
//!    (Woodward et al. 2003), que ajustan mejor en zonas áridas.
//!
//! 2. **Ruteo por hidrograma unitario triangular del SCS**, parametrizado por
//!    el tiempo de concentración `tc` de la cuenca — que se estima de la
//!    geometría del DEM (Kirpich/Giandotti), igual que `x4` en el path GR4J.
//!    Tiempo al peak `Tp = 0.6·tc + Δt/2`, base `2.67·Tp`.
//!
//! ## Lo que este módulo NO hace
//!
//! No modela evapotranspiración ni flujo base: el CN los absorbe implícitamente
//! a través de la condición antecedente. Para una cuenca hiperárida eso es
//! razonable (el flujo base es cero la mayor parte del año); para una cuenca
//! húmeda sería inaceptable, y ahí GR4J es el modelo correcto. **Los dos paths
//! coexisten a propósito** y la elección es por régimen, no por preferencia.

use crate::{Error, RainflowForcing, Result};
use nowcast_core::GridDims;

/// Fracción de abstracción inicial de la formulación clásica del SCS.
pub const LAMBDA_CLASICO: f64 = 0.20;
/// Fracción de abstracción inicial de las revisiones modernas; ajusta mejor en
/// zonas áridas (Woodward et al. 2003).
pub const LAMBDA_ARIDO: f64 = 0.05;

/// Parámetros de la producción Hortoniana, todos ESTIMABLES de propiedades
/// físicas de la cuenca — ninguno se ajusta contra un hidrograma observado.
#[derive(Debug, Clone, Copy)]
pub struct HortonianParams {
    /// Curve Number (30–100). Regolito desnudo / roca con grupo hidrológico D
    /// cae en ~85–95; suelo profundo con vegetación, en ~55–70.
    pub cn: f64,
    /// Tiempo de concentración de la cuenca, en horas (Kirpich/Giandotti sobre
    /// la geometría del DEM).
    pub tc_horas: f64,
    /// Fracción de abstracción inicial (ver [`LAMBDA_CLASICO`], [`LAMBDA_ARIDO`]).
    pub lambda: f64,
}

impl HortonianParams {
    /// Valida los rangos físicos. `CN` fuera de (30, 100] o `tc` no positivo no
    /// son "parámetros extremos": son entradas sin sentido, y el modelo
    /// devolvería números plausibles con ellas si no se rechazaran.
    pub fn validar(&self) -> Result<()> {
        if !self.cn.is_finite() || self.cn <= 30.0 || self.cn > 100.0 {
            return Err(Error::Invalid {
                name: "cn",
                reason: format!("Curve Number debe estar en (30, 100]; se recibió {}", self.cn),
            });
        }
        if !self.tc_horas.is_finite() || self.tc_horas <= 0.0 {
            return Err(Error::Invalid {
                name: "tc_horas",
                reason: format!("tiempo de concentración debe ser > 0; se recibió {}", self.tc_horas),
            });
        }
        if !self.lambda.is_finite() || self.lambda < 0.0 || self.lambda >= 1.0 {
            return Err(Error::Invalid {
                name: "lambda",
                reason: format!("abstracción inicial debe estar en [0, 1); se recibió {}", self.lambda),
            });
        }
        Ok(())
    }

    /// Retención potencial máxima `S` (mm).
    pub fn s_mm(&self) -> f64 {
        25400.0 / self.cn - 254.0
    }

    /// Escorrentía directa (mm) para una lámina de lluvia `p` (mm) en el paso.
    ///
    /// Nota de honestidad sobre la aplicación paso a paso: el SCS-CN se formuló
    /// para el TOTAL de un evento, no para una serie continua. Aplicarlo por
    /// paso diario —como se hace acá y en la mayoría de las implementaciones
    /// operacionales— trata cada día como un evento independiente y por tanto
    /// ignora la humedad que deja el día anterior. Es conservador (subestima el
    /// segundo día de una tormenta larga) y es la razón por la que el CN debe
    /// entenderse como "condición antecedente media", no seca.
    pub fn escorrentia_mm(&self, p: f64) -> f64 {
        if !p.is_finite() || p <= 0.0 {
            return 0.0;
        }
        let s = self.s_mm();
        let ia = self.lambda * s;
        if p <= ia {
            return 0.0;
        }
        let num = (p - ia) * (p - ia);
        num / (p - ia + s)
    }
}

/// Hidrograma unitario triangular del SCS discretizado al paso `dt_horas`.
///
/// Devuelve las ordenadas normalizadas (suman 1), de modo que la convolución
/// conserva el volumen: lo que entra como escorrentía sale como hidrograma, solo
/// redistribuido en el tiempo.
pub fn uh_triangular_scs(tc_horas: f64, dt_horas: f64) -> Result<Vec<f64>> {
    if !tc_horas.is_finite() || tc_horas <= 0.0 || !dt_horas.is_finite() || dt_horas <= 0.0 {
        return Err(Error::Invalid {
            name: "uh",
            reason: format!("tc y dt deben ser > 0; se recibió tc={tc_horas}, dt={dt_horas}"),
        });
    }
    let tp = 0.6 * tc_horas + dt_horas / 2.0; // tiempo al peak
    let tb = 2.67 * tp; // tiempo base
    let n = (tb / dt_horas).ceil() as usize;
    if n == 0 {
        // El paso es más largo que el hidrograma entero: toda la escorrentía
        // sale en el mismo paso. Es el caso degenerado correcto, no un error.
        return Ok(vec![1.0]);
    }
    let mut u = Vec::with_capacity(n);
    for k in 0..n {
        let t = (k as f64 + 0.5) * dt_horas;
        // triángulo: sube hasta tp, baja hasta tb
        let v = if t <= tp {
            t / tp
        } else if t < tb {
            (tb - t) / (tb - tp)
        } else {
            0.0
        };
        u.push(v.max(0.0));
    }
    let suma: f64 = u.iter().sum();
    if suma <= 0.0 {
        return Ok(vec![1.0]);
    }
    for v in u.iter_mut() {
        *v /= suma;
    }
    Ok(u)
}

/// Serie de escorrentía Hortoniana ruteada (mm/paso) a partir de la lluvia.
pub fn escorrentia_ruteada(
    precip_mm: &[f64],
    params: &HortonianParams,
    dt_dias: f64,
) -> Result<Vec<f64>> {
    params.validar()?;
    if !dt_dias.is_finite() || dt_dias <= 0.0 {
        return Err(Error::Invalid {
            name: "dt_dias",
            reason: format!("el paso debe ser > 0; se recibió {dt_dias}"),
        });
    }
    let u = uh_triangular_scs(params.tc_horas, dt_dias * 24.0)?;
    let mut q = vec![0.0_f64; precip_mm.len()];
    for (i, &p) in precip_mm.iter().enumerate() {
        let exceso = params.escorrentia_mm(p);
        if exceso <= 0.0 {
            continue;
        }
        for (k, &uk) in u.iter().enumerate() {
            if let Some(slot) = q.get_mut(i + k) {
                *slot += exceso * uk;
            }
        }
    }
    Ok(q)
}

impl RainflowForcing {
    /// Forzante de caudal por producción **Hortoniana** (SCS-CN + UH triangular),
    /// el análogo de [`RainflowForcing::gr4j`] para régimen árido.
    ///
    /// No pide PET: el CN absorbe la condición antecedente. A diferencia de
    /// GR4J, no está restringido a paso diario — el UH se discretiza al `dt`
    /// que se le dé.
    pub fn hortoniana(
        dims: GridDims,
        dt_dias: f64,
        params: HortonianParams,
        precip_mm: &[f64],
    ) -> Result<Self> {
        let q = escorrentia_ruteada(precip_mm, &params, dt_dias)?;
        Self::new(dims, dt_dias, q)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(cn: f64, tc: f64) -> HortonianParams {
        HortonianParams { cn, tc_horas: tc, lambda: LAMBDA_ARIDO }
    }

    #[test]
    fn cn_fuera_de_rango_se_rechaza() {
        assert!(p(0.0, 12.0).validar().is_err());
        assert!(p(101.0, 12.0).validar().is_err());
        assert!(p(88.0, 0.0).validar().is_err());
        assert!(p(88.0, f64::NAN).validar().is_err());
        assert!(p(88.0, 12.0).validar().is_ok());
    }

    #[test]
    fn lluvia_bajo_la_abstraccion_inicial_no_escurre() {
        let par = p(88.0, 12.0);
        let ia = par.lambda * par.s_mm();
        assert_eq!(par.escorrentia_mm(ia * 0.99), 0.0);
        assert!(par.escorrentia_mm(ia * 1.5) > 0.0);
    }

    #[test]
    fn entradas_hostiles_no_producen_escorrentia() {
        let par = p(88.0, 12.0);
        // Todo lo no-finito se rechaza ANTES de entrar a la fórmula. Importa
        // que +inf también: (inf-Ia)²/(inf-Ia+S) daría NaN, que se propagaría
        // en silencio por la convolución y envenenaría toda la serie. Una
        // entrada no-finita es dato corrupto, no "lluvia infinita": aportar
        // cero es la lectura correcta y deja el resto de la serie utilizable.
        for v in [0.0, -5.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(par.escorrentia_mm(v), 0.0, "p = {v}");
        }
        // y la serie ruteada tampoco se contamina
        let q = escorrentia_ruteada(&[10.0, f64::NAN, 60.0, 0.0, 0.0, 0.0], &par, 1.0).unwrap();
        assert!(q.iter().all(|x| x.is_finite()), "la convolución propagó un no-finito");
    }

    #[test]
    fn cn_mas_alto_escurre_mas() {
        let seco = p(70.0, 12.0).escorrentia_mm(50.0);
        let impermeable = p(95.0, 12.0).escorrentia_mm(50.0);
        assert!(impermeable > seco, "{impermeable} debería superar a {seco}");
    }

    #[test]
    fn el_uh_conserva_volumen() {
        for tc in [1.0, 12.0, 48.0] {
            let u = uh_triangular_scs(tc, 24.0).unwrap();
            let s: f64 = u.iter().sum();
            assert!((s - 1.0).abs() < 1e-12, "tc={tc}: suma {s}");
        }
    }

    #[test]
    fn el_ruteo_conserva_el_volumen_escurrido() {
        let par = p(90.0, 12.0);
        let pr = vec![0.0, 5.0, 80.0, 20.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        let q = escorrentia_ruteada(&pr, &par, 1.0).unwrap();
        let esperado: f64 = pr.iter().map(|&x| par.escorrentia_mm(x)).sum();
        let obtenido: f64 = q.iter().sum();
        // la cola puede caerse del final de la serie; con margen suficiente no
        assert!((obtenido - esperado).abs() < 1e-9, "{obtenido} vs {esperado}");
    }

    #[test]
    fn tc_mas_corto_adelanta_el_peak() {
        let pr = vec![0.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        let rapido = escorrentia_ruteada(&pr, &p(90.0, 6.0), 1.0).unwrap();
        let lento = escorrentia_ruteada(&pr, &p(90.0, 96.0), 1.0).unwrap();
        let arg_max = |v: &[f64]| {
            v.iter().enumerate().fold((0usize, f64::NEG_INFINITY), |(bi, bv), (i, &x)| {
                if x > bv { (i, x) } else { (bi, bv) }
            }).0
        };
        assert!(arg_max(&rapido) <= arg_max(&lento));
    }
}
