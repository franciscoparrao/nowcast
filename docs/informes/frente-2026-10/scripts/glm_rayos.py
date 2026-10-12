#!/usr/bin/env python3
"""Rayos GLM (GOES-19, bucket público noaa-goes19) en un día y rango de horas UTC:
flashes por 10 min en un bbox grande y en uno chico. Streaming: cada archivo de 20 s
(~450 KB) se baja a memoria, se filtra y se descarta (~90 MB por hora, sin disco).
Uso: glm_rayos.py 2026 281 18 23 "-70.85,-34.25,-69.75,-33.05" "-70.70,-33.55,-70.30,-33.30"
(281 = 8-oct-2026). Salida de referencia: evidencia/glm_20261008/flashes_por_10min.txt"""
import collections, json, sys, time, urllib.request, xml.etree.ElementTree as ET
import netCDF4
year, doy, h0, h1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
A = tuple(float(x) for x in sys.argv[5].split(",")); B = tuple(float(x) for x in sys.argv[6].split(","))
ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
ca, cb = collections.Counter(), collections.Counter(); t0 = time.time()
for hour in range(h0, h1 + 1):
    x = urllib.request.urlopen(f"https://noaa-goes19.s3.amazonaws.com/?list-type=2&prefix=GLM-L2-LCFA/{year}/{doy:03d}/{hour:02d}/&max-keys=1000", timeout=60).read()
    for c in ET.fromstring(x).findall("s:Contents", ns):
        k = c.find("s:Key", ns).text
        raw = urllib.request.urlopen("https://noaa-goes19.s3.amazonaws.com/" + k, timeout=60).read()
        d = netCDF4.Dataset("mem", memory=raw); lat = d["flash_lat"][:]; lon = d["flash_lon"][:]; d.close()
        st = k.split("_s")[1][:11]; b10 = f"{st[7:9]}:{int(st[9:11]) // 10 * 10:02d}"
        for bb, cnt in ((A, ca), (B, cb)):
            cnt[b10] += int(((lon >= bb[0]) & (lon <= bb[2]) & (lat >= bb[1]) & (lat <= bb[3])).sum())
    print(f"hora {hour:02d}Z lista ({time.time() - t0:.0f} s)", flush=True)
print("\nflashes por 10 min (UTC)  bbox A | bbox B")
for b in sorted(set(ca) | set(cb)):
    if ca[b] or cb[b]: print(f"  {b}  {ca[b]:4d} | {cb[b]:4d}")
print("total A", sum(ca.values()), "B", sum(cb.values()))
