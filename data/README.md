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

### High-resolution sub-daily (v0.2, `examples/atacama_subdaily.rs`)

| file | content | source |
|---|---|---|
| `atacama_imerg_hhr.csv` | half-hourly rainfall rate (`core_mm_hr` = fixed storm-core cell, `boxmean_mm_hr` = 3×3-cell mean) over the Mar 2015 Atacama event | **GPM IMERG** Final half-hourly `GPM_3IMERGHH` v07, ~0.1°, via `scripts/extract_atacama_imerg.py` (NASA GES DISC). Storm core located at lon −70.45, lat −27.15 (108.5 mm event total). |

> **Earthdata prerequisite.** GES DISC downloads require a one-time account step:
> log into <https://urs.earthdata.nasa.gov> → *Applications → Authorized Apps* and
> approve **"NASA GESDISC DATA ARCHIVE"** (and accept the GPM EULA). Without it
> `earthaccess` fails with `EulaNotAccepted`. Credentials are read from `~/.netrc`.
> The raw HDF5 (~1 GB) lands in `data/imerg_hhr/` (git-ignored); only the CSV is kept.

### Multi-event and single-factor experiment (`examples/multi_event_leadtime.rs`, `examples/single_factor_resolution.rs`)

| file | content | source |
|---|---|---|
| `event_<key>.csv` | half-hourly rainfall rate at each event's storm core (`atacama_2015`, `maipo_2017`, `santalucia_2017`) | **GPM IMERG** Final v07 via `scripts/extract_event_imerg.py` |
| `event_<key>_cr2met.csv` | daily precip (mm) at the **same** location: `p_mm_max` and `p_mm_mean` over the CR2MET cells inside the IMERG pixel | **CR2MET v2.5** via `scripts/extract_event_cr2met.py` |

> **Why max and mean, not the nearest cell.** An IMERG pixel (~0.1°) covers about
> 2×2 CR2MET cells (~0.05°), and at all three cores the IMERG centre falls
> *exactly* halfway between two CR2MET centres — the nearest-cell tie-break is
> numerically arbitrary and swings the wettest day by ~25 % (Atacama 24-Mar:
> 15.93 vs 12.73 mm). The single-factor experiment uses `p_mm_max`, which gives
> the daily product its best shot at the storm and so biases the comparison
> against the resolution argument.

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
