//! Dump the distributed Maipo hazard field at the wettest step to CSV, for
//! plotting an example hazard map. Run: `cargo run --example dump_hazard`.

use std::path::PathBuf;

use nowcast_core::{
    GridDims, GriddedRain, IdThreshold, Nowcast, SusceptibilityMap, TriggerModel,
};

fn data(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data").join(name)
}

fn main() {
    // grid + real susceptibility
    let text = std::fs::read_to_string(data("maipo_dist_grid.csv")).unwrap();
    let (mut nr, mut nc) = (0usize, 0usize);
    let mut rows: Vec<(usize, usize, usize, f32, f32, f64)> = Vec::new();
    for line in text.lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let (cell, r, c): (usize, usize, usize) =
            (f[0].parse().unwrap(), f[1].parse().unwrap(), f[2].parse().unwrap());
        let (lat, lon): (f32, f32) = (f[3].parse().unwrap(), f[4].parse().unwrap());
        let susc: f64 = f[5].parse().unwrap();
        nr = nr.max(r + 1);
        nc = nc.max(c + 1);
        rows.push((cell, r, c, lat, lon, susc));
    }
    rows.sort_by_key(|x| x.0);
    let dims = GridDims::new(nc, nr);
    let susc_vals: Vec<f64> = rows.iter().map(|x| x.5).collect();
    let susceptibility = SusceptibilityMap::new(dims, susc_vals).unwrap();

    // distributed precip → GriddedRain
    let ptext = std::fs::read_to_string(data("maipo_dist_pr.csv")).unwrap();
    let mut depths = Vec::new();
    let mut day_total: Vec<(usize, f64)> = Vec::new();
    for (s, line) in ptext.lines().skip(1).enumerate() {
        let before = depths.len();
        depths.extend(line.split(',').skip(1).map(|v| v.parse::<f64>().unwrap_or(0.0)));
        let tot: f64 = depths[before..].iter().sum();
        day_total.push((s, tot));
    }
    let wettest = day_total.iter().copied().fold((0, 0.0), |a, b| if b.1 > a.1 { b } else { a }).0;

    let forcing = GriddedRain::new(dims, 24.0, depths).unwrap();
    let nowcast = Nowcast::new(
        susceptibility,
        forcing,
        IdThreshold::new(5.5, 0.39).unwrap(),
        TriggerModel::default(),
        7,
    )
    .unwrap();
    let field = nowcast.hazard_at(wettest).unwrap();

    let mut out = String::from("row,col,lat,lon,susc,hazard\n");
    for (i, &(_, r, c, lat, lon, susc)) in rows.iter().enumerate() {
        out.push_str(&format!("{r},{c},{lat:.4},{lon:.4},{susc:.4},{:.4}\n", field.probability()[i]));
    }
    std::fs::write(data("fig_hazard.csv"), out).unwrap();
    eprintln!("wettest step {wettest}, peak hazard {:.3} → data/fig_hazard.csv", field.max_probability());
}
