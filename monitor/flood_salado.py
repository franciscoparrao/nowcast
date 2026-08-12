#!/usr/bin/env python3
"""Path de CRECIDAS para el Río Salado (Chañaral) — el canal correcto para ahí.

## Por qué existe

El monitor vigila Chañaral con el gatillo I-D de deslizamientos: acumula lluvia
en ventanas móviles y la cruza con la susceptibilidad de LADERA de la celda. Ese
es el mecanismo equivocado para ese pueblo. Lo que lo destruyó en marzo de 2015
fue una **crecida hiperconcentrada del Río Salado** —un main-stem de ~1600 km²
que integró lluvia caída a 100 km, en la cordillera— no la falla de una ladera
local. Ya está documentado en docs/representacion-area-fanapex.md: al validar el
detector de fan-apex, el footprint mapeado de Chañaral resultó ser "del género
geomorfológico equivocado", un main-stem, no un abanico de quebrada.

El motor YA tiene el canal correcto: `nowcast-rainflow` rutea la lluvia por la
cuenca (GR4J) y dispara por **exceedancia de caudal** Q/Q_c en vez de I-D,
porque el ruteo ya integró la lluvia y no hay ley de potencia duración-intensidad
que aplicar. Ese path está validado out-of-sample en el Río Itata (ROC-AUC 0.90).
Este script lo instancia para el Salado.

## Las tres limitaciones, y ninguna es menor

1. **NO hay caudal observado del Salado.** CAMELS-CL no trae estación de
   Atacama en este disco, así que GR4J NO se puede calibrar. Se corre con
   parámetros plausibles y el umbral Q_c se deriva de un cuantil de la serie
   SIMULADA: la exceedancia es entonces "cuán inusual es este caudal para lo que
   ESTE modelo produce en ESTA cuenca", no una magnitud absoluta.

2. **GR4J está fuera de su régimen.** Es un modelo de almacenamiento
   humedo-templado; en Atacama el runoff es de exceso de infiltración
   (Hortoniano) sobre regolito desnudo. Si el modelo produce runoff ~0 para el
   evento de 2015, eso NO significa que no hubo crecida: significa que el modelo
   no representa el mecanismo, y ese es un resultado que hay que reportar, no
   esconder.

3. **La crecida de 2015 fue hiperconcentrada** (cargada de sedimento), no agua
   limpia. GR4J rutea agua. El caudal simulado es una cota inferior de lo que
   bajó por el cauce.

## La validación que SÍ es posible sin aforo

No hay serie de caudal contra la cual medir NSE/KGE, pero sí hay una **fecha de
verdad conocida**: el aluvión de Chañaral del 25-26 de marzo de 2015. Si el
caudal simulado por el modelo alcanza su máximo histórico (o cerca) en esa
fecha, el modelo está capturando el evento aunque no esté calibrado. Si no lo
hace, no hay que creerle. Es el mismo criterio event-céntrico que el resto del
proyecto usa cuando el inventario no permite métricas continuas.

Uso:
    python3 flood_salado.py                     # delinea, corre y valida 2015
    python3 flood_salado.py --anios 1979 2021 --q-crit-p 0.999
"""

import argparse
import glob
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

KINGSTON = "/mnt/kingston/proyectos/postdoc/papers/paper1_susceptibilidad/factors"
BASIN = "04_rio_salado"          # default; --basin lo cambia
CR2 = "/mnt/kingston/proyectos/Agentes/CR2MET_pr_v2.5_day_1960-2021_005deg/pr"
# Desembocadura del Salado, junto a Chañaral. El outlet se ENGANCHA al cauce
# mapeado más cercano: el punto de la costa no cae exacto sobre la celda de
# mayor acumulación.
OUTLET_LON, OUTLET_LAT = -70.62, -26.35   # default; --outlet lo cambia
EVENTO_2015 = ("2015-03-24", "2015-03-27")   # aluvión de Chañaral


def delinear(snap_km=8.0, basin=None, outlet=None):
    """Cuenca aportante al Salado en Chañaral, por retroceso D8 sobre la
    hidrología de KINGSTON. Devuelve (máscara, transform, crs, área_km2)."""
    import rasterio
    from pyproj import Transformer
    from melton_cuencas import cuenca_aguas_arriba

    basin = basin or BASIN
    olon, olat = outlet or (OUTLET_LON, OUTLET_LAT)
    h = os.path.join(KINGSTON, basin, "hydrology")
    with rasterio.open(os.path.join(h, "flow_accumulation.tif")) as s:
        facc, crs, tr, res = s.read(1), s.crs, s.transform, s.res[0]
    with rasterio.open(os.path.join(h, "flow_direction_d8.tif")) as s:
        fdir = s.read(1)

    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(olon, olat)
    r0, c0 = rasterio.transform.rowcol(tr, x, y)
    # Enganche: dentro del radio, la celda de MAYOR área aportante (el
    # main-stem). Sin esto el outlet cae en una ladera costera y la cuenca sale
    # de 2 km² en vez de 1600.
    rad = int(snap_km * 1000 / res)
    r_lo, r_hi = max(0, r0 - rad), min(facc.shape[0], r0 + rad + 1)
    c_lo, c_hi = max(0, c0 - rad), min(facc.shape[1], c0 + rad + 1)
    sub = facc[r_lo:r_hi, c_lo:c_hi]
    dr, dc = np.unravel_index(np.nanargmax(sub), sub.shape)
    rs, cs = r_lo + dr, c_lo + dc
    mask = cuenca_aguas_arriba(fdir, rs, cs)
    area = float(mask.sum()) * (res * res) / 1e6
    return mask, tr, crs, area, (rs, cs), float(facc[rs, cs]) * (res * res) / 1e6


def tiempo_concentracion(mask, basin, res):
    """Tiempo de concentración por Kirpich y Giandotti sobre la geometría REAL
    de la cuenca (cauce principal por retroceso D8 desde la salida, siguiendo
    siempre el afluente de mayor área aportante). Es lo que fija x4 sin ajustar
    nada: x4 ~ 1.75 x tc. Devuelve (area_km2, L_km, H_m, tc_kirpich_h,
    tc_giandotti_h)."""
    import rasterio
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
    from detect_fan_apex import D8_OFFSETS
    h = os.path.join(KINGSTON, basin, "hydrology")
    with rasterio.open(os.path.join(h, "flow_accumulation.tif")) as s_:
        facc = s_.read(1)
    with rasterio.open(os.path.join(h, "flow_direction_d8.tif")) as s_:
        fdir = s_.read(1)
    with rasterio.open(os.path.join(KINGSTON, basin, "dem_30m.tif")) as s_:
        dem = s_.read(1).astype(float)
    dem[dem <= 0] = np.nan
    ys, xs = np.where(mask)
    # salida = celda de la cuenca con MAYOR acumulación
    k = int(np.argmax(facc[ys, xs]))
    r, c = int(ys[k]), int(xs[k])
    L, zs = 0.0, []
    for _ in range(400000):
        if np.isfinite(dem[r, c]):
            zs.append(dem[r, c])
        best = None
        for code, (dy, dx) in enumerate(D8_OFFSETS, start=1):
            rr, cc = r - dy, c - dx
            if (0 <= rr < fdir.shape[0] and 0 <= cc < fdir.shape[1]
                    and mask[rr, cc] and int(fdir[rr, cc]) == code):
                if best is None or facc[rr, cc] > facc[best[0], best[1]]:
                    best = (rr, cc)
        if best is None:
            break
        L += res * (math.sqrt(2) if (best[0] != r and best[1] != c) else 1.0)
        r, c = best
    H = (max(zs) - min(zs)) if zs else float("nan")
    area = float(mask.sum()) * res * res / 1e6
    tc_k = 0.0195 * (L ** 0.77) * ((H / L) ** -0.385) / 60.0 if L > 0 and H > 0 else float("nan")
    tc_g = (4 * math.sqrt(area) + 1.5 * L / 1000) / (0.8 * math.sqrt(max(H, 1)))
    return area, L / 1000.0, H, tc_k, tc_g


def serie_cr2met(mask, tr, crs, y0, y1):
    """Precipitación media diaria sobre la cuenca (mm/día), de CR2MET."""
    import rasterio
    import xarray as xr
    from rasterio.warp import Resampling, reproject

    # la máscara está en la grilla de 30 m de KINGSTON; se lleva a la grilla
    # CR2MET (0.05°) como FRACCIÓN de celda dentro de la cuenca, que es el peso
    # correcto para un promedio areal (una celda a medias pesa la mitad).
    ds0 = xr.open_dataset(sorted(glob.glob(f"{CR2}/*_2000_01_*.nc"))[0])
    lat, lon = ds0.lat.values, ds0.lon.values
    res = float(abs(lat[1] - lat[0]))
    nu = lat[0] > lat[-1]
    from rasterio.transform import from_origin
    dst_tr = from_origin(lon[0] - res / 2, (lat[0] if nu else lat[-1]) + res / 2, res, res)
    peso = np.zeros((len(lat), len(lon)), dtype="float32")
    reproject(source=mask.astype("float32"), destination=peso,
              src_transform=tr, src_crs=crs, dst_transform=dst_tr, dst_crs="EPSG:4326",
              src_nodata=None, dst_nodata=0.0, resampling=Resampling.average)
    if not nu:
        peso = peso[::-1, :]
    tot = peso.sum()
    if tot <= 0:
        sys.exit("[salado] la cuenca no intersecta la grilla CR2MET")
    peso = peso / tot

    fechas, pr = [], []
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            hits = glob.glob(f"{CR2}/CR2MET_pr_v2.5_day_{y}_{m:02d}_005deg.nc")
            if not hits:
                continue
            with xr.open_dataset(hits[0]) as ds:
                a = ds["pr"].values
                t = ds.time.values
            for k in range(a.shape[0]):
                fechas.append(str(t[k])[:10])
                pr.append(float(np.nansum(np.nan_to_num(a[k], nan=0.0) * peso)))
    return np.array(fechas), np.array(pr)


def pet_oudin(fechas, lat_deg, t_media_c):
    """PET diaria (mm) por Oudin et al. (2005), la fórmula estándar para cuencas
    sin datos: solo necesita radiación extraterrestre (geometría) y temperatura.
    Se usa la temperatura media anual de WorldClim (bio_01) modulada por un ciclo
    estacional simple — es una aproximación GRUESA, y su efecto es de segundo
    orden acá porque en el desierto la PET satura el almacenamiento igual."""
    phi = math.radians(lat_deg)
    out = np.empty(len(fechas))
    for i, f in enumerate(fechas):
        import datetime as _dt
        d = _dt.date.fromisoformat(f)
        j = d.timetuple().tm_yday
        dr = 1 + 0.033 * math.cos(2 * math.pi * j / 365)
        dec = 0.409 * math.sin(2 * math.pi * j / 365 - 1.39)
        ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(dec))))
        ra = (24 * 60 / math.pi) * 0.0820 * dr * (
            ws * math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.sin(ws))
        # ciclo estacional de temperatura (hemisferio sur: máx en enero)
        t = t_media_c + 6.0 * math.cos(2 * math.pi * (j - 15) / 365)
        out[i] = max(0.0, ra * (t + 5.0) / 100.0 / 2.45) if t > -5 else 0.0
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--basin", default=BASIN, help="cuenca KINGSTON (04_rio_salado, 06_rio_huasco…)")
    ap.add_argument("--outlet", nargs=2, type=float, default=[OUTLET_LON, OUTLET_LAT],
                    metavar=("LON", "LAT"), help="desembocadura; se engancha al cauce mayor")
    ap.add_argument("--anios", nargs=2, type=int, default=[1979, 2021])
    ap.add_argument("--q-crit-p", type=float, default=0.995,
                    help="cuantil de la serie SIMULADA que define Q_c")
    ap.add_argument("--out", default=os.path.expanduser("~/nowcast-monitor-atacama/salado_flood.json"))
    a = ap.parse_args()

    print("[salado] delineando la cuenca aportante a Chañaral…")
    mask, tr, crs, area, (rs, cs), area_outlet = delinear(basin=a.basin, outlet=tuple(a.outlet))
    print(f"[salado] cuenca: {area:,.0f} km² (celda de salida declara {area_outlet:,.0f} km²)")
    ar, L, H, tck, tcg = tiempo_concentracion(mask, a.basin, 30.0)
    print(f"[salado] geometría: cauce principal {L:.0f} km · desnivel {H:.0f} m · "
          f"pendiente {H/(L*1000):.4f}")
    print(f"[salado] tiempo de concentración: Kirpich {tck:.1f} h · Giandotti {tcg:.1f} h")
    print(f"[salado]   → x4 físico (~1.75·tc) = {1.75*tcg/24:.2f} d (Giandotti) / "
          f"{1.75*tck/24:.2f} d (Kirpich)")

    print(f"[salado] serie CR2MET {a.anios[0]}-{a.anios[1]} sobre la cuenca…")
    fechas, pr = serie_cr2met(mask, tr, crs, *a.anios)
    print(f"[salado]   {len(pr)} días · media {pr.mean()*365:.0f} mm/año · "
          f"días con lluvia>1mm: {(pr>1).sum()} ({(pr>1).mean()*100:.1f}%)")

    import rasterio
    with rasterio.open(os.path.join(KINGSTON, a.basin, "climate", "bio_01.tif")) as s:
        b1 = s.read(1).astype(float)
        b1[b1 < -100] = np.nan
        t_media = float(np.nanmean(b1))
    pet = pet_oudin(fechas, a.outlet[1], t_media)
    print(f"[salado]   T media {t_media:.1f} °C → PET media {pet.mean()*365:.0f} mm/año (Oudin)")

    json.dump({"fechas": fechas.tolist(), "pr": pr.tolist(), "pet": pet.tolist(),
               "area_km2": area, "t_media_c": t_media, "basin": a.basin,
               "L_km": L, "H_m": H, "tc_kirpich_h": tck, "tc_giandotti_h": tcg,
               "x4_fisico_d": round(1.75*tcg/24, 3)},
              open(a.out.replace(".json", "_forzante.json"), "w"))
    print(f"[salado] → {a.out.replace('.json', '_forzante.json')}")
    return 0


if __name__ == "__main__":
    # Hermana de fetch_imerg_early/fetch_goes_qpe: mismo par xarray/h5 +
    # rasterio/GDAL, luego mismo riesgo de SIGSEGV en la finalización. Acá el
    # código de retorno SÍ tiene contrato (main() devuelve 0), así que se
    # preserva en vez de forzar 0: las rutas de falla usan sys.exit() antes.
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc or 0)
