//! Combined nowcast: rainfall I–D ⊕ InSAR deformation rate.
//!
//! Where the ground is already creeping (high LOS velocity), a slope sits closer
//! to failure and needs less rain to be flagged. This example runs a marginal,
//! sub-threshold rain day over a transect whose deformation rate rises from
//! stable to fast creep, and shows the combined (noisy-OR) hazard light up the
//! moving cells that rain alone would miss.
//!
//! Run with: `cargo run -p nowcast-insar --example rain_and_creep`
//!
//! In production the velocity field comes from `insar_core::run_sbas` over a
//! Sentinel-1 stack; here it is a synthetic transect to isolate the coupling.

use ndarray::Array2;
use nowcast_core::{
    Combine, GridDims, IdThreshold, IdTrigger, MultiNowcast, SusceptibilityMap, TriggerModel,
    UniformRain,
};
use nowcast_insar::deformation_trigger_from_velocity;

fn main() {
    const NC: usize = 5;
    let dims = GridDims::new(NC, 1);
    // LOS velocity (m/yr) rising along the transect: 2 → 45 mm/yr.
    let vel_mm_yr = [2.0_f32, 8.0, 20.0, 32.0, 45.0];
    let velocity = Array2::from_shape_vec((1, NC), vel_mm_yr.iter().map(|v| v / 1000.0).collect())
        .unwrap();

    // A marginal rain day: 8 mm/h for 1 h — below Caine's curve, so rain alone
    // barely triggers anywhere.
    let rain_mm_h = 8.0;
    let rain = IdTrigger::new(
        UniformRain::new(dims, 1.0, vec![rain_mm_h]).unwrap(),
        IdThreshold::caine(),
        TriggerModel::default(),
        6,
    )
    .unwrap();
    let v_crit = 20.0; // mm/yr: above this, creep is concerning
    let deform = deformation_trigger_from_velocity(&velocity, 1, 1.0, v_crit, TriggerModel::default())
        .unwrap();

    let susc = SusceptibilityMap::uniform(dims, 0.85).unwrap();
    let nowcast = MultiNowcast::new(
        susc,
        vec![Box::new(rain), Box::new(deform)],
        Combine::NoisyOr,
    )
    .unwrap();

    println!(
        "Nowcast combinado lluvia ⊕ deformación — lluvia {rain_mm_h} mm/h (marginal), v_crit {v_crit} mm/yr\n"
    );
    println!(
        "{:>4} | {:>10} | {:>9} | {:>9} | {:>9}",
        "celda", "v (mm/yr)", "f_lluvia", "f_deform", "peligro"
    );
    let field = nowcast.hazard_at(0);
    for (c, &v) in vel_mm_yr.iter().enumerate() {
        let f = nowcast.trigger_factors(c, 0);
        let haz = field.probability()[c];
        let mark = if haz >= 0.5 { "⚠" } else { " " };
        println!(
            "{mark}{c:>3} | {v:>10.0} | {:>9.2} | {:>9.2} | {haz:>9.2}",
            f[0], f[1]
        );
    }

    println!(
        "\n→ La lluvia marginal no gatilla por sí sola, pero donde el terreno ya repta\n  \
         (v ≳ v_crit) la deformación lleva el peligro sobre el umbral. Dos señales\n  \
         independientes combinadas por noisy-OR: cada una puede gatillar, juntas refuerzan."
    );
}
