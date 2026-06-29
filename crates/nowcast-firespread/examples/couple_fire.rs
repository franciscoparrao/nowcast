//! Post-fire debris-flow cascade: wildfire → susceptibility → rainfall nowcast.
//!
//! A burn scar lowers the rainfall needed to trigger a debris flow. This example
//! shows the full one-way cascade on a synthetic Andean-foothill grid:
//!
//!   1. run `firespread` from a ridge ignition under a dry, windy day → burn scar;
//!   2. amplify the static susceptibility inside the scar (`post_fire_susceptibility`);
//!   3. run the *ordinary* rainfall nowcast (`nowcast-core`) on the pre-fire and
//!      post-fire susceptibility with the *same* modest storm, and compare.
//!
//! The same storm that does not alert on the unburned slope crosses the alert
//! level across the burn scar — the documented post-wildfire debris-flow window.
//!
//! Run with: `cargo run -p nowcast-firespread --example couple_fire`

use nowcast_core::{
    GridDims, HazardField, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel, UniformRain,
};
use nowcast_firespread::{Landscape, Moisture, Weather, post_fire_susceptibility, run_fire};

const N: usize = 24; // grid is N×N
const CELL_M: f64 = 30.0;
const ALERT: f64 = 0.5;
const CASCADE: f64 = 2.0; // post-fire susceptibility amplification on the scar

/// Static susceptibility increasing downslope (toward the south rows): an
/// unburned foothill where only the steepest toe is moderately predisposed.
fn baseline_susceptibility(dims: GridDims) -> SusceptibilityMap {
    let mut v = Vec::with_capacity(dims.len());
    for row in 0..dims.nrows {
        for _col in 0..dims.ncols {
            v.push(0.20 + 0.30 * (row as f64 / (dims.nrows - 1) as f64)); // 0.20 .. 0.50
        }
    }
    SusceptibilityMap::new(dims, v).unwrap()
}

/// Index of the step with the highest basin-wide hazard.
fn peak_step(fields: &[HazardField]) -> usize {
    fields
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.max_probability().partial_cmp(&b.1.max_probability()).unwrap())
        .map(|(i, _)| i)
        .unwrap()
}

fn scar_stats(field: &HazardField, burned: &[bool]) -> (usize, f64) {
    let mut n_alert = 0;
    let mut sum = 0.0;
    let mut n = 0;
    for (p, &b) in field.probability().iter().zip(burned) {
        if b {
            n += 1;
            sum += *p;
            if *p >= ALERT {
                n_alert += 1;
            }
        }
    }
    (n_alert, if n > 0 { sum / n as f64 } else { 0.0 })
}

fn run(susc: SusceptibilityMap) -> Vec<HazardField> {
    let dims = susc.dims();
    // A modest 4-day storm: peaks at 60 mm/day, enough to matter on a scar but
    // not on the unburned slope.
    let rain = UniformRain::new(dims, 24.0, vec![5.0, 25.0, 60.0, 15.0]).unwrap();
    Nowcast::new(susc, rain, IdThreshold::new(6.0, 0.39).unwrap(), TriggerModel::default(), 7)
        .unwrap()
        .run()
}

fn main() {
    let dims = GridDims::new(N, N);

    // 1) Wildfire: dry summer, 30 km/h west wind, ignition on the upper ridge.
    let land = Landscape::uniform(N, N, CELL_M, 4); // NFFL 4 (chaparral)
    let weather = Weather {
        wind_speed_kmh: 30.0,
        wind_from_deg: 270.0,
        moisture: Moisture::DRY_SUMMER,
    };
    let fire = run_fire(&land, &weather, &[(4, 6)], 240.0).unwrap();
    let burned = fire.burned_mask();
    // Fire hazard normalized by 1730 kW/m (the limit of direct manual attack).
    let fire_hz = fire.fire_hazard(1730.0).unwrap();
    let high_intensity = fire_hz.probability().iter().filter(|&&p| p >= ALERT).count();
    println!(
        "Wildfire: {} cells burned ({:.1} ha) in {:.0} min; {} cells above the manual-attack fire-hazard level",
        fire.burned_cells(),
        fire.burned_area_ha(),
        fire.horizon_min(),
        high_intensity,
    );

    // 2) Cascade the burn into the susceptibility surface.
    let base = baseline_susceptibility(dims);
    let post = post_fire_susceptibility(&base, &fire, CASCADE).unwrap();

    // 3) Same storm on pre-fire vs post-fire susceptibility.
    let pre_fields = run(base);
    let post_fields = run(post);
    let step = peak_step(&post_fields);
    let (pre_alert, pre_mean) = scar_stats(&pre_fields[step], &burned);
    let (post_alert, post_mean) = scar_stats(&post_fields[step], &burned);

    println!("\nSame storm (peak 60 mm/day), hazard over the {} burned cells at the wettest step:", fire.burned_cells());
    println!("  pre-fire  : mean hazard {pre_mean:.3}, {pre_alert} cell(s) at/above alert {ALERT}");
    println!("  post-fire : mean hazard {post_mean:.3}, {post_alert} cell(s) at/above alert {ALERT}");
    println!(
        "\nThe burn scar raises mean debris-flow hazard {:.2}× and flips {} scar cell(s) into\n\
         alert under rainfall that left the unburned slope below threshold — the post-fire\n\
         debris-flow window, produced by the one-way fire → susceptibility → nowcast cascade.",
        if pre_mean > 0.0 { post_mean / pre_mean } else { f64::INFINITY },
        post_alert.saturating_sub(pre_alert),
    );
}
