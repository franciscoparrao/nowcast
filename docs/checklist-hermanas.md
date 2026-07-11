# Checklist de hermanas — barrido obligatorio al arreglar un bug de frontera

**Por qué existe**: en 3 rondas de auditoría, el mismo patrón reincidió **seis
veces**: se blinda la función nombrada en el hallazgo y queda sin blindar una
"hermana" igual de alcanzable con el mismo contrato. Cada reincidencia costó
una ronda de auditoría completa en detectarse.

| # | Se blindó | Quedó expuesta | Detectado en |
|---|-----------|----------------|--------------|
| 1 | `Nowcast::explain` (OOB, ronda 1) | `hazard_at`, `intensity_to_alert`, y el trío espejo de nowcast-rainflow | Ronda 2 (N-H1, N-H2, N-H4) |
| 2 | `Calibrator::fit_isotonic` (NaN, ronda 2) | `probability`/`calibrate` del **mismo struct** | Ronda 3 (R3-H2) |
| 3 | `MultiNowcast::hazard_at` (OOB, ronda 2) | `trigger_factors` del **mismo struct** | Ronda 3 (R3-M3) |
| 4 | `gridded_rain_from_rasters` (±inf, ronda 2) | `susceptibility_from_raster` del **mismo archivo** | Ronda 3 (R3-M8) |
| 5 | `run_runout` (pixel_size, ronda 1) | `from_footprint`, bypass público del mismo guard | Ronda 3 (R3-M10) |
| 6 | `DepthField::from_states` (NaN, ronda 2) | `q_mass` NaN lo rodeaba **río arriba** (`apply_point_sources`) | Ronda 3 (R3-M7) |

**Regla**: un fix de frontera (validación de entrada, panic OOB, NaN/±inf,
rango [0,1]) no se cierra a nivel de *función*, se cierra a nivel de
**struct/módulo/contrato**. Antes de dar por terminado el fix, ejecutar este
barrido y dejar constancia en el mensaje del commit.

## El barrido (6 pasos + greps)

1. **Mismo struct/trait** — enumerar TODOS los métodos públicos del tipo
   tocado y preguntarse cuáles reciben la misma clase de input (índices
   cell/step, floats que deben ser finitos, probabilidades, tamaños):

   ```bash
   grep -n "pub fn" crates/<crate>/src/<archivo>.rs
   ```

   Blindar cada una, o anotar en el commit por qué no aplica (p. ej. accessor
   interno bajo contrato documentado del trait, como `api_at`).

2. **Mismo archivo/módulo** — funciones hermanas que consumen la misma clase
   de dato (dos conversores de raster, dos parsers CSV, dos constructores):
   ¿aplican la misma política? La política divergente **es** el bug
   (`csv_events` con `skip(1)` vs los otros parsers que toleran header).

3. **Espejos declarados en otros crates** — si el doc-comment dice
   "counterpart/espejo/analogue de X", o un adapter replica la API del core,
   el fix viaja con él:

   ```bash
   grep -rn "<nombre_funcion>" crates/ --include="*.rs"
   grep -rni "counterpart\|espejo\|analogue\|mirror" crates/ --include="*.rs"
   ```

4. **Río arriba y río abajo del guard** — ¿puede otro camino producir o
   consumir el mismo valor venenoso saltándose el guard nuevo? (el caso 6:
   el detector de NaN post-integración era correcto, pero `(h + NaN).max(0.0)`
   lo lavaba en cada paso *antes* de que pudiera verlo). Seguir el dato una
   capa hacia cada lado.

5. **Simetría de verbos y superficies** — si `run` valida X, ¿`watch` y
   `backtest` validan X? ¿El binding Python delega en el core o duplica (y
   des-sincroniza) la validación? Preferir: guard en el core + delegación;
   la CLI puede validar antes para fallar rápido, como backstop redundante
   pero nunca como única línea.

6. **Un test hostil por hermana** — cada función blindada en el barrido recibe
   su caso adversarial propio (OOB, NaN, ±inf, vacío, negativo), no solo la
   función del hallazgo original. Los tests de la ronda 3 son la plantilla
   (`point_sources_outside_the_mesh_or_non_finite_are_rejected`,
   `probability_rejects_non_finite_scores_instead_of_panicking`).

## Búsqueda de panics residuales en el módulo tocado

```bash
grep -n "unwrap()\|expect(\|\[\(cell\|step\|row\|col\|idx\|i\)\]" crates/<crate>/src/<archivo>.rs
```

Cada `unwrap`/`expect`/indexación directa que sobreviva debe poder justificarse
en una frase ("t está en 0..n_steps por construcción", "hazard finito por
invariante de HazardField"). Si la justificación necesita más de una frase,
probablemente es una hermana.

## Constancia en el commit

Al final del cuerpo del commit del fix:

```
Barrido de hermanas: <struct/módulo> revisado completo — <funciones
blindadas/descartadas y por qué>.
```

Un fix sin esta línea no cierra el hallazgo de frontera que lo motivó.
