#!/usr/bin/env python3
"""Efecto de la cascada en la grilla NATIVA de la cuenca: susceptibilidad mediana y fracción
≥0.5 dentro de cada cicatriz, antes y después de ×factor (clamp [0,1]), tal como la aplica
susc_overlay.aplicar_cascada. Lee solo la ventana de la cicatriz del ráster de KINGSTON."""
import json, os, sys
import numpy as np, rasterio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.windows import from_bounds
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "monitor"))
from susc_overlay import KINGSTON, DOMAIN_BASINS
REG = os.path.join(ROOT, "monitor", "data", "postfire", "scars.json")
layers = sorted({p for lst in DOMAIN_BASINS.values() for p in lst})
for r in json.load(open(REG)):
    mask_path = os.path.join(os.path.dirname(REG), r["mask"])
    with rasterio.open(mask_path) as m:
        mb = transform_bounds(m.crs, "EPSG:4326", *m.bounds)
    for lay in layers:
        if not lay.startswith(r["basin"]): continue
        src_path = os.path.join(KINGSTON, lay)
        if not os.path.exists(src_path): print(f"   falta {src_path}"); continue
        with rasterio.open(src_path) as s:
            b = transform_bounds("EPSG:4326", s.crs, *mb)
            win = from_bounds(*b, transform=s.transform).round_offsets().round_lengths()
            if win.width < 1 or win.height < 1: continue
            arr = s.read(1, window=win).astype("float32"); t = s.window_transform(win)
            if arr.size == 0 or min(arr.shape) < 2: continue   # ventana fuera del ráster
            if s.nodata is not None and not np.isnan(s.nodata): arr[arr == s.nodata] = np.nan
            dst = np.full(arr.shape, 255, "uint8")
            with rasterio.open(mask_path) as m:
                reproject(rasterio.band(m, 1), dst, dst_transform=t, dst_crs=s.crs,
                          resampling=Resampling.nearest, src_nodata=255, dst_nodata=255)
        q = (dst == 1) & np.isfinite(arr)
        if not q.any(): print(f"== {r['nombre']} × {lay}: la cicatriz no cae en celdas con dato"); continue
        antes = arr[q]; despues = np.clip(antes * float(r["factor"]), 0, 1)
        print(f"== {r['nombre']} × {lay}: {q.sum()} celdas nativas con dato en la cicatriz; "
              f"susc mediana {np.median(antes):.3f} → {np.median(despues):.3f}; ≥0.5: {np.mean(antes >= 0.5):.0%} → {np.mean(despues >= 0.5):.0%}; "
              f"fuera de la cicatriz (misma ventana) mediana {np.nanmedian(arr[(dst == 0) & np.isfinite(arr)]):.3f}")
