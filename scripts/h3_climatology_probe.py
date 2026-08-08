"""Sonda R4-H3: ¿un índice de anomalía climatológica rompe la saturación?

RESPUESTA: NO. Se conserva porque el resultado NEGATIVO es el hallazgo, y
para que sea reproducible.

Pregunta: ¿un índice relativo ("cuán inusual es esta lluvia PARA ESTE LUGAR")
rompe la saturación que ningún estadístico distribucional pudo romper?

Climatología: CR2MET v2.5 diario 0.05° (observado), inviernos MJJAS 1991-2020.
Evento: rain_liquid_total.tif del ciclo (acumulado 120 h del pronóstico).
"""
import glob, os, sys
import numpy as np, pandas as pd, geopandas as gpd, rasterio, xarray as xr
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterstats import zonal_stats

CR2 = "/mnt/kingston/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"
F = os.path.expanduser("~/nowcast-forecast/20260731T06z")
SCR = os.path.dirname(os.path.abspath(__file__))
ANIOS = range(1991, 2021)
MESES = (5, 6, 7, 8, 9)
VENTANA = 5          # días acumulados, ~el horizonte de 120 h del pronóstico

gdf = gpd.read_file(F + "/comuna_impact/comuna_impact.geojson")[["COMUNA", "poblacion", "peak_terreno", "geometry"]]
print(f"{len(gdf)} comunas", flush=True)

# --- mapeo celda CR2MET → comuna (una sola vez) -------------------------------
ds0 = xr.open_dataset(sorted(glob.glob(f"{CR2}/*_1991_07_*.nc"))[0])
lat = ds0.lat.values; lon = ds0.lon.values
res = float(abs(lat[1] - lat[0]))
norte_up = lat[0] > lat[-1]
tr = from_origin(lon[0] - res / 2, (lat[0] if norte_up else lat[-1]) + res / 2, res, res)
shapes = [(g, i + 1) for i, g in enumerate(gdf.geometry) if g is not None]
lab = rasterize(shapes, out_shape=(len(lat), len(lon)), transform=tr, fill=0,
                dtype=np.int32, all_touched=True)
if not norte_up:
    lab = lab[::-1, :]
ncom = len(gdf)
cnt = np.bincount(lab.ravel(), minlength=ncom + 1)[1:]
print(f"celdas CR2MET por comuna: mediana {int(np.median(cnt))} · sin celdas: {(cnt==0).sum()}", flush=True)

# --- serie diaria por comuna --------------------------------------------------
flat = lab.ravel()
series, fechas = [], []
for y in ANIOS:
    for m in MESES:
        hits = glob.glob(f"{CR2}/CR2MET_pr_v2.5_day_{y}_{m:02d}_005deg.nc")
        if not hits:
            continue
        with xr.open_dataset(hits[0]) as ds:
            pr = ds["pr"].values  # (t, lat, lon)
            t = ds.time.values
        for k in range(pr.shape[0]):
            a = np.nan_to_num(pr[k], nan=0.0).ravel()
            s = np.bincount(flat, weights=a, minlength=ncom + 1)[1:]
            series.append(np.divide(s, cnt, out=np.full(ncom, np.nan), where=cnt > 0))
            fechas.append(t[k])
    print(f"  {y} ok", flush=True)
S = pd.DataFrame(np.array(series), index=pd.to_datetime(fechas), columns=gdf["COMUNA"].values)
S = S.sort_index()

# --- climatología de acumulados de VENTANA días -------------------------------
roll = S.rolling(VENTANA, min_periods=VENTANA).sum()
roll = roll.dropna(how="all")
np.save(f"{SCR}/h3_roll.npy", roll.values)
roll.to_pickle(f"{SCR}/h3_roll.pkl")
print(f"climatología: {len(roll)} acumulados de {VENTANA} d por comuna", flush=True)

# --- evento: lluvia acumulada del ciclo, por comuna ---------------------------
ev = np.full(len(gdf), np.nan)
for dom in ["atacama", "coquimbo", "rm", "nuble-biobio", "araucania", "chile-centro-sur"]:
    p = f"{F}/{dom}/rain_liquid_total.tif"
    if not os.path.exists(p):
        continue
    with rasterio.open(p) as s:
        arr = s.read(1); aff = s.transform; nod = s.nodata
    if nod is not None and not (isinstance(nod, float) and np.isnan(nod)):
        arr = np.where(arr == nod, np.nan, arr)
    st = zonal_stats(gdf.geometry, arr, affine=aff, nodata=np.nan, all_touched=True, stats=["mean"])
    for i, s_ in enumerate(st):
        v = s_.get("mean")
        if v is not None and np.isfinite(v):
            ev[i] = v if not np.isfinite(ev[i]) else max(ev[i], v)

# --- percentil del evento contra la climatología de SU comuna -----------------
pct = np.full(len(gdf), np.nan)
ret = np.full(len(gdf), np.nan)
for i, c in enumerate(gdf["COMUNA"].values):
    col = roll[c].dropna().values if c in roll.columns else np.array([])
    if col.size < 100 or not np.isfinite(ev[i]):
        continue
    pct[i] = float((col < ev[i]).mean())
    # periodo de retorno empírico aproximado sobre máximos de temporada
    n_temp = len(ANIOS)
    exced = max(1, int((col >= ev[i]).sum()))
    ret[i] = (len(col) / exced) / (len(col) / n_temp)

out = pd.DataFrame({"COMUNA": gdf["COMUNA"], "pob": gdf["poblacion"],
                    "peak_terreno": gdf["peak_terreno"], "lluvia_mm": ev,
                    "pct_clim": pct, "retorno_temporadas": ret})
out.to_csv(f"{SCR}/h3_anom.csv", index=False)
print("→ h3_anom.csv", flush=True)
