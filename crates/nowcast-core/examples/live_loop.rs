//! Real-time nowcasting loop: ingest forcing step by step and alert as it arrives.
//!
//! The batch `backtest`/`quickstart` examples replay a whole series at once. This
//! one drives the streaming [`LiveNowcast`] the way an operational system would:
//! each time step a new rainfall field "arrives", the engine updates and emits an
//! alert immediately, with no knowledge of the future. At the end we confirm the
//! streamed hazard is bit-identical to the batch engine on the same data.
//!
//! Run with: `cargo run --example live_loop`

use nowcast_core::{
    GridDims, IdThreshold, LiveNowcast, Nowcast, ReplaySource, SusceptibilityMap, TriggerModel,
    UniformRain, run_live,
};

const ALERT: f64 = 0.5;
const DT_H: f64 = 24.0;
const MAX_WINDOW: usize = 7;

fn main() {
    // A small foothill grid: susceptibility rising downslope.
    let dims = GridDims::new(4, 4);
    let susc = SusceptibilityMap::new(
        dims,
        (0..dims.len()).map(|c| 0.2 + 0.5 * ((c / dims.ncols) as f64 / (dims.nrows - 1) as f64)).collect(),
    )
    .unwrap();
    let threshold = IdThreshold::new(6.0, 0.39).unwrap();
    let trigger = TriggerModel::default();

    // A storm that builds over a week, then clears (mm/day, basin-wide).
    let storm = vec![1.0, 3.0, 8.0, 25.0, 60.0, 40.0, 8.0, 1.0];

    // --- Live loop: data arrives one step at a time --------------------------
    let mut engine = LiveNowcast::new(susc.clone(), threshold, trigger, MAX_WINDOW, DT_H).unwrap();
    println!("Live nowcast (alert at peak hazard ≥ {ALERT}):");
    println!("step  rain(mm)  peak_hazard  status");
    let mut streamed = Vec::new();
    for (t, &rain) in storm.iter().enumerate() {
        // One step's field "arrives" (uniform here; could be a raster per cell).
        let depths = vec![rain; dims.len()];
        let field = engine.push(&depths).unwrap();
        let peak = field.max_probability();
        let status = match field.alert(ALERT) {
            Some(a) => format!("ALERT — {} cell(s), {:.0}% of grid", a.n_cells, 100.0 * a.fraction),
            None => "quiet".to_string(),
        };
        println!("{t:>4}  {rain:>7.0}  {peak:>11.3}  {status}");
        streamed.push(field);
    }

    // --- Parity: the stream equals the batch engine, bit for bit -------------
    let forcing = UniformRain::new(dims, DT_H, storm.clone()).unwrap();
    let batch = Nowcast::new(susc, forcing, threshold, trigger, MAX_WINDOW).unwrap().run();
    let identical = batch.iter().zip(&streamed).all(|(b, s)| {
        b.probability()
            .iter()
            .zip(s.probability())
            .all(|(pb, ps)| pb.to_bits() == ps.to_bits())
    });
    assert!(identical, "streaming diverged from batch");
    println!("\nStreamed {} steps; hazard is bit-identical to the batch engine.", streamed.len());

    // The same engine also consumes any batch forcing through a StepSource,
    // which is how a file tail or a polled feed would plug in:
    let mut engine2 = LiveNowcast::new(
        SusceptibilityMap::uniform(dims, 0.6).unwrap(),
        threshold,
        trigger,
        MAX_WINDOW,
        DT_H,
    )
    .unwrap();
    let mut source = ReplaySource::new(UniformRain::new(dims, DT_H, storm).unwrap());
    let mut n_alerts = 0;
    run_live(&mut engine2, &mut source, |f| {
        if f.alert(ALERT).is_some() {
            n_alerts += 1;
        }
    })
    .unwrap();
    println!("Via run_live + StepSource: {n_alerts} step(s) raised an alert.");
}
