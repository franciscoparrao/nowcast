//! Dual cause–trigger threshold: antecedent wetness × storm-scale I–D.
//!
//! The Maipo backtest measured FAR ≈ 0.9 as *structural*: over a Mediterranean
//! wet season, an I–D trigger alone fires on every vigorous winter storm, but
//! slopes fail preferentially when a burst lands on an already-wet hillside.
//! The Bogaard & Greco (2018) "cause–trigger" recipe encodes exactly that:
//! require BOTH an antecedent-wetness state (the cause) and an
//! intensity–duration exceedance (the trigger).
//!
//! This example builds a synthetic year where failures need both ingredients,
//! then scores the I–D trigger alone against `AntecedentTrigger × IdTrigger`
//! (`Combine::Product`) with the day-resolution matcher and warning lead
//! times: same POD and lead, roughly half the false alarms.
//!
//! Run with: `cargo run --example dual_threshold`

use std::collections::BTreeSet;

use nowcast_core::{
    AntecedentTrigger, Combine, DayKey, GridDims, IdThreshold, IdTrigger, MultiNowcast,
    SusceptibilityMap, TriggerModel, UniformRain, lead_times, spatial_daily_contingency,
};

/// Deterministic LCG in [0, 1) so the example needs no rng dependency.
struct Lcg(u64);
impl Lcg {
    fn unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn main() {
    // --- A synthetic Mediterranean year of daily rain on one cell. ---
    let n_days = 365usize;
    let mut rng = Lcg(2015);
    let mut rain = vec![0.0f64; n_days];
    for (d, r) in rain.iter_mut().enumerate() {
        // Wet season (austral winter, days ~120-270): frequent moderate storms.
        let wet_season = (120..270).contains(&d);
        let p_rain = if wet_season { 0.45 } else { 0.05 };
        if rng.unit() < p_rain {
            let scale = if wet_season { 18.0 } else { 8.0 };
            *r = -scale * (1.0 - rng.unit()).ln(); // exponential-ish (mm/day)
        }
    }

    // --- Ground truth: failures need a wet hillside AND a strong burst. ---
    // Antecedent state with 0.85/day retention (half-life ~4.3 days), event on
    // any day with API > 60 mm and daily rain > 25 mm.
    let mut api = 0.0;
    let mut events: Vec<(usize, DayKey)> = Vec::new();
    for (d, &r) in rain.iter().enumerate() {
        if api > 60.0 && r > 25.0 {
            events.push((0, d as DayKey));
        }
        api = 0.85 * api + r;
    }

    // --- Two detectors over the same forcing. ---
    let dims = GridDims::new(1, 1);
    let forcing = || UniformRain::new(dims, 24.0, rain.clone()).unwrap();
    let susc = SusceptibilityMap::uniform(dims, 1.0).unwrap();
    let threshold = IdThreshold::new(1.0, 0.39).unwrap(); // regional-ish daily curve
    let model = TriggerModel::default();

    let id_only = MultiNowcast::new(
        susc.clone(),
        vec![Box::new(IdTrigger::new(forcing(), threshold, model, 7).unwrap())],
        Combine::Max,
    )
    .unwrap();
    let dual = MultiNowcast::new(
        susc,
        vec![
            Box::new(IdTrigger::new(forcing(), threshold, model, 7).unwrap()),
            Box::new(AntecedentTrigger::new(forcing(), 0.85, 60.0, model).unwrap()),
        ],
        Combine::Product,
    )
    .unwrap();

    // --- Score both with the day-resolution machinery. ---
    let days: Vec<DayKey> = (0..n_days as i64).collect();
    let alert_level = 0.5;

    println!("Umbral dual causa×gatillo — año sintético, {} eventos reales\n", events.len());
    println!("                        alertas  hits  miss  FA    FAR   lead medio");
    for (name, nc) in [("I-D solo", &id_only), ("antecedente × I-D", &dual)] {
        let hazard: Vec<f64> = (0..n_days).map(|d| nc.hazard_at(d).unwrap().max_probability()).collect();
        let alerted: BTreeSet<(usize, DayKey)> = hazard
            .iter()
            .enumerate()
            .filter(|&(_, &h)| h >= alert_level)
            .map(|(d, _)| (0usize, d as DayKey))
            .collect();
        let c = spatial_daily_contingency(dims, &days, &alerted, &events, 0, 1);
        let leads = lead_times(dims, &days, &alerted, &events, 0, 1);
        let hit_leads: Vec<i64> = leads.iter().filter_map(|(_, l)| *l).collect();
        let mean_lead = if hit_leads.is_empty() {
            f64::NAN
        } else {
            hit_leads.iter().sum::<i64>() as f64 / hit_leads.len() as f64
        };
        println!(
            "  {name:<20} {:>6}  {:>4}  {:>4}  {:>4}  {:.2}   {:+.1} días",
            alerted.len(),
            c.hits,
            c.misses,
            c.false_alarms,
            c.far().unwrap_or(f64::NAN),
            mean_lead,
        );
    }

    println!(
        "\nEl gatillo I-D dispara en cada tormenta vigorosa del invierno; el producto\n\
         causa×gatillo exige además ladera húmeda y recorta las falsas alarmas a\n\
         cerca de la mitad sin perder un solo evento ni lead time — la receta\n\
         estándar contra el FAR estructural de clima mediterráneo (Bogaard &\n\
         Greco 2018). El mismo AntecedentTrigger enchufa sin cambios sobre la\n\
         forzante real (CR2MET/IMERG) vía MultiNowcast."
    );
}
