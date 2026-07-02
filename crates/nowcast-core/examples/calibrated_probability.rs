//! Turn the hazard index into a calibrated probability with uncertainty.
//!
//! The engine emits a bounded hazard *index* (`susceptibility × trigger`), not a
//! probability. Here we (1) run the engine over a synthetic field to get real
//! indices, (2) draw events whose true probability is a monotone-but-nonlinear
//! function of the index (so the raw index is miscalibrated by construction),
//! (3) fit an isotonic [`Calibrator`] on a training split, and (4) score the
//! held-out split: the calibrated probabilities are far better calibrated (lower
//! Brier, lower ECE) and each reliability bin carries a Wilson 95 % interval —
//! the uncertainty the bare index never had.
//!
//! Run with: `cargo run --example calibrated_probability`

use nowcast_core::{
    Calibrator, GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel,
    brier_score, reliability,
};

struct Lcg(u64);
impl Lcg {
    fn unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn main() {
    // --- 1) Real hazard indices from the engine over a synthetic field --------
    let dims = GridDims::new(20, 20);
    let n = dims.len();
    let mut rng = Lcg(0xDA7A);
    let n_steps = 120;
    let mut depths = Vec::with_capacity(n_steps * n);
    for _ in 0..n_steps * n {
        // Mostly dry with occasional bursts, so indices span their range.
        let u = rng.unit();
        depths.push(if u > 0.85 { 10.0 + 60.0 * rng.unit() } else { 4.0 * rng.unit() });
    }
    let forcing = GriddedRain::new(dims, 24.0, depths).unwrap();
    let susc = SusceptibilityMap::new(dims, (0..n).map(|c| 0.15 + 0.8 * (c as f64 / n as f64)).collect()).unwrap();
    let engine = Nowcast::new(susc, forcing, IdThreshold::new(6.0, 0.39).unwrap(), TriggerModel::default(), 7).unwrap();

    let index: Vec<f64> = engine.run().iter().flat_map(|f| f.probability().to_vec()).collect();

    // --- 2) Events with a monotone, nonlinear true probability of the index ---
    // true_p = index^2: a higher index always means more risk, but the raw index
    // systematically over-states the probability — exactly what calibration fixes.
    let outcomes: Vec<bool> = index.iter().map(|&x| rng.unit() < x * x).collect();

    // --- 3) Train/test split; fit isotonic on train --------------------------
    let split = index.len() / 2;
    let cal = Calibrator::fit_isotonic(&index[..split], &outcomes[..split]).unwrap();

    let raw_test = &index[split..];
    let out_test = &outcomes[split..];
    let cal_test = cal.calibrate(raw_test);

    // --- 4) Reliability on the held-out split --------------------------------
    let base = out_test.iter().filter(|&&o| o).count() as f64 / out_test.len() as f64;
    let raw_rel = reliability(raw_test, out_test, 10).unwrap();
    let cal_rel = reliability(&cal_test, out_test, 10).unwrap();

    println!("Calibration of the hazard index ({} held-out cell-steps, base rate {:.3})\n", out_test.len(), base);
    println!("                    Brier     skill      ECE");
    println!("  raw index       {:.4}   {:>+.3}   {:.4}", raw_rel.brier, raw_rel.brier_skill, raw_rel.ece);
    println!("  calibrated      {:.4}   {:>+.3}   {:.4}", cal_rel.brier, cal_rel.brier_skill, cal_rel.ece);
    println!(
        "  (Brier {:.4}→{:.4}, ECE {:.4}→{:.4})\n",
        brier_score(raw_test, out_test).unwrap(),
        brier_score(&cal_test, out_test).unwrap(),
        raw_rel.ece,
        cal_rel.ece
    );

    println!("Reliability diagram of the calibrated probability (Wilson 95% intervals):");
    println!("  pred    obs    95% CI            n");
    for b in &cal_rel.bins {
        println!(
            "  {:.3}  {:.3}   [{:.3}, {:.3}]   {:>5}",
            b.p_pred_mean, b.p_obs, b.ci_low, b.ci_high, b.n
        );
    }
    println!(
        "\nThe calibrated probability tracks observed frequency (points near the diagonal);\n\
         the Wilson interval widens where a bin holds few cell-steps — the honest uncertainty\n\
         a bare index cannot express."
    );
}
