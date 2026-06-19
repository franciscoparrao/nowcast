//! Agent-based refinement of a landslide alert into a debris-flow runout, on the
//! real 2015 Atacama event — the landslide-side counterpart of the Hydroflux
//! flood coupling.
//!
//! The nowcast flags a debris-flow alert over the Copiapó basin (the same event
//! the sub-daily IMERG lead-time example pins to 24-Mar-2015). Here we run the
//! `swarm-abm` debris-flow model on the calibrated terrain stack to simulate
//! *where the flow actually runs out*, then downscale the coarse alert
//! probability onto that physical footprint.
//!
//! Run with: `cargo run -p nowcast-swarm --example couple_runout`
//! (needs the swarm-abm debris-flow data stack; falls back to a note if absent.)

use std::path::Path;
use std::sync::Arc;

use nowcast_swarm::{DebrisParams, load, run_runout};

const DATA: &str = "/home/franciscoparrao/proyectos/swarm-abm/models/debris-flow/data/copiapo";

fn main() {
    let dir = Path::new(DATA);
    if !dir.exists() {
        println!("(stack debris-flow de Copiapó no encontrado en {DATA};\n  \
                  corre el adapter sobre tu propio Layers vía run_runout)");
        return;
    }

    let data = load(dir).unwrap_or_else(|e| panic!("cargando {DATA}: {e}"));
    let nowcast_prob = 0.7; // alerta gruesa del nowcast para la zona

    println!("Evento Atacama 2015 — refinamiento por agentes (debris-flow ABM)\n");
    let runout = run_runout(
        Arc::new(data.layers),
        DebrisParams::default(),
        data.pixel_size,
        42,
        300,
    );

    println!(
        "Runout simulado: {} celdas alcanzadas · {:.1} km² (pixel {:.0} m)",
        runout.affected_cells(),
        runout.affected_km2(),
        data.pixel_size,
    );

    let hazard = runout.refined_hazard(0, nowcast_prob);
    let flagged = hazard.probability().iter().filter(|&&p| p > 0.0).count();
    println!(
        "Peligro refinado: la prob {nowcast_prob} del nowcast se concentra en {flagged}/{} celdas\n  \
         del trazado físico del flujo — el resto del polígono de alerta queda en 0.",
        hazard.probability().len()
    );
    println!(
        "\n→ Donde Hydroflux refina CRECIDAS con aguas someras 2D, swarm-abm refina\n  \
         DESLIZAMIENTOS/aluviones modelando el evento con agentes (lluvia + flujo).\n  \
         Mismo patrón de acople: caro, físico, solo donde el nowcast ya alertó."
    );
    println!("  params: Default (existen calibrados DE en data/best_params_de.json, IoU≈0.17).");
}
