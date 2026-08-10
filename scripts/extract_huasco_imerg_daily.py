"""Serie diaria de lluvia sobre la cuenca del Huasco desde IMERG Late Daily.

CR2MET termina en 2021, así que el evento de julio 2026 necesita otra fuente.
Se usa GPM_3IMERGDL (Late, ~14 h de latencia, 1 archivo por día) en vez del
semihorario: para un test de TIMING a resolución diaria no hace falta bajar 2928
gránulos semihorarios. Cada archivo se procesa y se BORRA.
"""
import glob, json, os, sys
import numpy as np, rasterio, xarray as xr, earthaccess
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

sys.path.insert(0, "/home/franciscoparrao/proyectos/nowcast/monitor")
from flood_salado import delinear, KINGSTON

WORK = "/tmp/claude-1000/-home-franciscoparrao-proyectos-nowcast/6b42f0af-9d23-416b-b5d2-93f63acd8342/scratchpad/imerg_dl"
OUT = "/home/franciscoparrao/nowcast-monitor-atacama/huasco_imerg_2026.json"

mask, tr, crs, area, _, _ = delinear(basin="06_rio_huasco", outlet=(-71.22, -28.46))
print(f"cuenca Huasco: {area:,.0f} km²", flush=True)

earthaccess.login(strategy="netrc")
gr = earthaccess.search_data(short_name="GPM_3IMERGDL",
                             temporal=("2026-06-01", "2026-07-31"),
                             bounding_box=(-71.3, -29.7, -69.1, -27.9))
print(f"{len(gr)} gránulos diarios", flush=True)
os.makedirs(WORK, exist_ok=True)

peso = None
fechas, pr = [], []
for i, g in enumerate(sorted(gr, key=lambda x: str(x.data_links()[0]))):
    f = earthaccess.download([g], WORK, show_progress=False)[0]
    fecha = os.path.basename(f).split(".3IMERG.")[1][:8]
    with xr.open_dataset(f) as ds:
        v = "precipitation" if "precipitation" in ds else list(ds.data_vars)[0]
        da = ds[v].squeeze()
        lat = ds.lat.values; lon = ds.lon.values
        arr = da.transpose("lat", "lon").values if "lat" in da.dims else da.values.T
    if peso is None:
        res = float(abs(lat[1] - lat[0]))
        nu = lat[0] > lat[-1]
        dst_tr = from_origin(lon[0] - res / 2, (lat[0] if nu else lat[-1]) + res / 2, res, res)
        peso = np.zeros((len(lat), len(lon)), dtype="float32")
        reproject(source=mask.astype("float32"), destination=peso,
                  src_transform=tr, src_crs=crs, dst_transform=dst_tr, dst_crs="EPSG:4326",
                  src_nodata=None, dst_nodata=0.0, resampling=Resampling.average)
        if not nu:
            peso = peso[::-1, :]
        peso = peso / peso.sum()
        print(f"  peso areal listo ({(peso>0).sum()} celdas IMERG en la cuenca)", flush=True)
    a = np.nan_to_num(arr.astype(float), nan=0.0)
    if a.shape != peso.shape:
        a = a.T
    fechas.append(f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:8]}")
    pr.append(float(np.sum(a * peso)))   # IMERG DL viene en mm/día
    os.remove(f)
    if (i + 1) % 15 == 0:
        print(f"  {i+1}/{len(gr)}", flush=True)

json.dump({"fechas": fechas, "pr": pr, "area_km2": area}, open(OUT, "w"))
print(f"\n→ {OUT}")
top = sorted(zip(fechas, pr), key=lambda x: -x[1])[:6]
print("días más lluviosos sobre la cuenca (IMERG Late, media areal):")
for f, v in top:
    print(f"  {f}  {v:6.2f} mm/día")
