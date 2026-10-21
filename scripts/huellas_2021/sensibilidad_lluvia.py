#!/usr/bin/env python3
"""¿Responde la huella del runout ABM a la lluvia? Misma ventana (El Arrayán,
14 km), misma semilla y susceptibilidad; solo cambia la lluvia diaria (igual los
3 días). Imprime las celdas afectadas por preset y lluvia.
Hallazgo (2026-10-21): chanaral_enhanced da la MISMA huella de 0.3 a 200
mm/día; `de` es un interruptor en ~0.5 mm/día. Causa en swarm-abm
models/debris-flow/src/model.rs: Raindrop::step (umbral horario
`rain_threshold`, calibrado en [0.01, 0.3] mm/h) y agentes de lluvia fijos."""
import json, os, subprocess
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BIN = os.path.join(ROOT, "target", "release", "examples", "runout_ondemand")
SUSC = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors/09_rio_maipo/susceptibility_RandomForest.tif"
print(f"{'preset':18s} " + " ".join(f"{r:>7}" for r in (0.3, 1, 5, 15, 50, 200)) + "   (celdas afectadas; mm/día ×3)")
for p in ("chanaral_enhanced", "de"):
    fila = []
    for r in (0.3, 1, 5, 15, 50, 200):
        out = os.path.join(ROOT, "data", "huellas_2021", "runout", f"sens_{p}_{r}")
        if not os.path.exists(os.path.join(out, "stats.json")):
            subprocess.run([BIN, "--lon=-70.42", "--lat=-33.35", "--size-km", "14", "--rain-mm-per-day", f"{r},{r},{r}",
                            "--isotherm-m", "4500", "--sediment", "0.5", "--susceptibility", SUSC, "--params", p,
                            "--seed", "1", "--out", out], check=True, capture_output=True)
        fila.append(json.load(open(os.path.join(out, "stats.json")))["runout"]["affected_cells"])
    print(f"{p:18s} " + " ".join(f"{c:7d}" for c in fila))
