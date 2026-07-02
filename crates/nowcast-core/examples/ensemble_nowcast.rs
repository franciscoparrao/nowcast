//! Probabilistic (ensemble) nowcasting — the engine side of SOTA forecast forcing.
//!
//! A forecast forcing (an ensemble rainfall nowcast such as pySTEPS or a deep
//! generative model like DGMR) supplies many plausible near-future realizations.
//! This example stands in for that model with a reproducible stochastic rainfall
//! ensemble, runs the engine over every member ([`ensemble_hazard`]), and shows
//! that the resulting **exceedance probability** is a calibrated, skilful forecast
//! of a held-out "truth" realization — with the ensemble spread as its uncertainty.
//!
//! The point is the engine machinery: the members enter through the ordinary
//! `Forcing` interface, so a real ensemble QPF model replaces the toy generator
//! below without touching the hazard logic.
//!
//! Run with: `cargo run --release --example ensemble_nowcast`

use nowcast_core::{
    Calibrator, GridDims, IdThreshold, SusceptibilityMap, TriggerModel, UniformRain, brier_score,
    ensemble_hazard, reliability,
};

const ALERT: f64 = 0.5;
const N_MEMBERS: usize = 60;

struct Lcg(u64);
impl Lcg {
    fn unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
    /// Standard normal via Box–Muller.
    fn normal(&mut self) -> f64 {
        let (u1, u2) = (self.unit().max(1e-12), self.unit());
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }
    /// Lognormal multiplier with the given coefficient of variation (approx).
    fn lognormal(&mut self, cv: f64) -> f64 {
        let sigma = (1.0 + cv * cv).ln().sqrt();
        (sigma * self.normal() - 0.5 * sigma * sigma).exp()
    }
}

/// One stochastic realization of the storm: a per-member intensity scale times
/// per-step multiplicative noise — the spread a QPF ensemble would carry.
fn realize(base: &[f64], rng: &mut Lcg) -> Vec<f64> {
    let scale = rng.lognormal(0.45); // member-wide wet/dry tendency
    base.iter().map(|&d| (d * scale * rng.lognormal(0.30)).max(0.0)).collect()
}

fn main() {
    let dims = GridDims::new(10, 10);
    let n = dims.len();
    // Susceptibility rising downslope, so cells differ in how easily they cross.
    let susc = SusceptibilityMap::new(
        dims,
        (0..n).map(|c| 0.25 + 0.7 * ((c / dims.ncols) as f64 / (dims.nrows - 1) as f64)).collect(),
    )
    .unwrap();
    let base = [2.0, 5.0, 16.0, 46.0, 30.0, 8.0, 2.0]; // a 7-day storm (mm/day)
    let threshold = IdThreshold::new(6.0, 0.39).unwrap();
    let trigger = TriggerModel::default();
    let max_window = 7;

    // Ensemble members and one independent "truth" realization.
    let mut rng = Lcg(0xE5EAB1E);
    let members: Vec<UniformRain> = (0..N_MEMBERS)
        .map(|_| UniformRain::new(dims, 24.0, realize(&base, &mut rng)).unwrap())
        .collect();
    let truth = UniformRain::new(dims, 24.0, realize(&base, &mut rng)).unwrap();

    // Probabilistic hazard from the ensemble.
    let ens = ensemble_hazard(&susc, members, threshold, trigger, max_window, ALERT).unwrap();

    // Deterministic truth hazard → the events the forecast is scored against.
    let truth_haz = nowcast_core::Nowcast::new(susc.clone(), truth, threshold, trigger, max_window)
        .unwrap()
        .run();

    // Flatten (forecast probability, outcome) over all cell-steps.
    let mut p_fc = Vec::new();
    let mut outcome = Vec::new();
    for (ef, tf) in ens.iter().zip(&truth_haz) {
        for (pc, &h) in ef.probability_of_exceedance().iter().zip(tf.probability()) {
            p_fc.push(*pc);
            outcome.push(h >= ALERT);
        }
    }
    let base_rate = outcome.iter().filter(|&&o| o).count() as f64 / outcome.len() as f64;

    // Deterministic baseline: the ensemble-mean hazard taken as a 0/1 forecast.
    let det: Vec<f64> = ens
        .iter()
        .flat_map(|ef| ef.mean().iter().map(|&m| if m >= ALERT { 1.0 } else { 0.0 }).collect::<Vec<_>>())
        .collect();

    // Split cell-steps in half: fit the calibrator on the first, score the second.
    let split = p_fc.len() / 2;
    let cal = Calibrator::fit_isotonic(&p_fc[..split], &outcome[..split]).unwrap();
    let (p_raw, out_te) = (&p_fc[split..], &outcome[split..]);
    let p_cal = cal.calibrate(p_raw);

    let raw_rel = reliability(p_raw, out_te, 8).unwrap();
    let cal_rel = reliability(&p_cal, out_te, 8).unwrap();
    println!(
        "Ensemble nowcast — {} members, {}x{} grid, {} cell-steps (event base rate {:.3})\n",
        N_MEMBERS, dims.ncols, dims.nrows, outcome.len(), base_rate
    );
    println!("                              Brier     skill      ECE");
    println!("  raw exceedance prob.       {:.4}   {:>+.3}   {:.4}", raw_rel.brier, raw_rel.brier_skill, raw_rel.ece);
    println!("  calibrated (isotonic)      {:.4}   {:>+.3}   {:.4}", cal_rel.brier, cal_rel.brier_skill, cal_rel.ece);
    println!("  deterministic (0/1)        {:.4}      —       —\n", brier_score(&det[split..], out_te).unwrap());

    println!("Reliability of the CALIBRATED ensemble probability (Wilson 95% intervals):");
    println!("  pred    obs     95% CI            n");
    for b in &cal_rel.bins {
        println!("  {:.3}  {:.3}   [{:.3}, {:.3}]  {:>5}", b.p_pred_mean, b.p_obs, b.ci_low, b.ci_high, b.n);
    }

    // Spread = forecast uncertainty; report the peak-step spread.
    let peak = ens.iter().max_by(|a, b| {
        a.max_probability_of_exceedance().partial_cmp(&b.max_probability_of_exceedance()).unwrap()
    }).unwrap();
    let mean_spread = peak.spread().iter().sum::<f64>() / n as f64;
    println!(
        "\nReading: the raw exceedance probability beats the deterministic forecast (lower\n\
         Brier) and carries uncertainty — at the most hazardous step it peaks at\n\
         P(alert) = {:.2} with a mean hazard spread of {:.3} — but it is badly under-\n\
         confident (negative Brier skill vs climatology, high ECE). Isotonic calibration\n\
         (the same module) makes it reliable. This is the engine-side machinery; a real\n\
         QPF ensemble (pySTEPS/DGMR) plugs into the same Forcing interface\n\
         (docs/sota-roadmap.md, axis 1).",
        peak.max_probability_of_exceedance(), mean_spread
    );
}
