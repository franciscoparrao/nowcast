# Backtest data (derived, not version-controlled)

The CSVs in this directory are **derived** from local research datasets and are
git-ignored. Regenerate them with:

```bash
python3 scripts/extract_maipo_cr2met.py       # lumped (centroid) — needs numpy + netCDF4
python3 scripts/extract_maipo_distributed.py  # distributed grid — also needs rasterio
```

## Files

### Lumped (v0.1 backtest, `examples/backtest.rs`)

| file | content | source |
|---|---|---|
| `maipo_cr2met_pr_1979_2016.csv` | daily precip (mm) 1979–2016 at the event-centroid cell (lat −33.625, lon −70.225) | **CR2MET v2.5** daily, 0.05°, CC-BY (`~/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/`) |
| `maipo_events_dated.csv` | rainfall-triggered landslide events, dated `(year, month)` + location | **SERNAGEOMIN** inventory, basin 09 Río Maipo (`~/proyectos/postdoc/.../basin_inventory/09_rio_maipo.csv`) |

### Distributed (v0.2 backtest, `examples/backtest_distributed.rs`)

| file | content | source |
|---|---|---|
| `maipo_dist_pr.csv` | daily precip (mm) per cell over a 15×18 CR2MET subgrid, row-major (rows N→S, cols W→E) | CR2MET v2.5 |
| `maipo_dist_grid.csv` | per-cell `lat, lon, susceptibility` | susceptibility = real **RandomForest** raster (`factors/09_rio_maipo/susceptibility_RandomForest.tif`, EPSG:32719, 30 m) reprojected to the CR2MET grid by area-average |
| `maipo_dist_events.csv` | rainfall-triggered events mapped to grid cells | SERNAGEOMIN inventory |

## Notes

- Event dates come from the record **id** (`RM-YYYY-MM-NNN`); the inventory
  `year` column is unreliable and is ignored. The id month itself is only
  approximate (e.g. the 3 May 1993 Quebrada de Macul debris flow is filed under
  March), which is why the backtest matches with a ±1–2 month tolerance.
- A single basin-centroid CR2MET cell is a v0.1 simplification: it cannot
  resolve the strong orographic rainfall gradient across the ~50 km event
  cluster. Distributed forcing arrives with the v0.2 providers.
