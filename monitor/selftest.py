#!/usr/bin/env python3
"""Autotest local del nowcast-monitor, sin red.

Modo base (sin argumentos): fabrica en /tmp/nowcast-monitor-selftest una
ventana rodante sintética de 96 pasos semihorarios (48 h) sobre una grilla
10×10 de 0.1°: 90 pasos de llovizna y una ráfaga convectiva de 3 h (25 mm por
media hora ≈ 50 mm/h) al final — muy por sobre Caine — más un status.json
fresco y un config.env apuntando al binario recién compilado. Luego:

    SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh

debe terminar en NOTIFY[urgent] ALERTA. Borrar la ráfaga (steps de mayor
valor) y volver a correr debe dar quiet.

Modo fusión (`--fusion`): fabrica el escenario de la fase A — el feed primario
IMERG llega solo hasta T−5 h con llovizna, el feed GOES llega hasta T con una
ráfaga convectiva EN LAS HORAS QUE IMERG AÚN NO VE, y el feed DGA está
deshabilitado (sin token). El mismo comando debe alertar VÍA LA FUSIÓN: si se
corre con GOES_ENABLED=0 el ciclo queda quiet, que es exactamente la latencia
que la fase A recupera. También valida fetch_dga.py --selftest.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
from rasterio.transform import from_origin

BASE = "/tmp/nowcast-monitor-selftest"
# El python con que PRODUCCIÓN corre los fetchers (venv del monitor, 2026-09-27);
# probar el contrato con el python del autotest sería probar otro entorno.
_VENV_PY = os.path.expanduser(os.environ.get("MONITOR_VENV", "~/nowcast-monitor/venv") + "/bin/python3")
MONITOR_PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Convenio de grilla IMERG del dominio piloto (idéntico a los fetchers).
TRANSFORM = from_origin(-70.80, -33.20, 0.1, 0.1)
PROFILE = dict(driver="GTiff", height=11, width=11, count=1,
               dtype="float32", crs="EPSG:4326", transform=TRANSFORM)


def write_steps(steps_dir, t_end, n_steps, burst_last=0, burst_mm=25.0):
    """n_steps semihorarios que terminan en t_end; los últimos burst_last
    llevan la ráfaga en el cuadrante NE sobre la llovizna de fondo."""
    os.makedirs(steps_dir, exist_ok=True)
    for old in os.listdir(steps_dir):
        os.remove(os.path.join(steps_dir, old))
    for i in range(n_steps):
        t = t_end - timedelta(minutes=30 * (n_steps - 1 - i))
        grid = np.full((11, 11), 0.2, dtype="float32")
        if i >= n_steps - burst_last:
            grid[2:5, 6:9] = burst_mm
        key = t.strftime("%Y%m%dT%H%M")
        with rasterio.open(os.path.join(steps_dir, f"step_{key}.tif"), "w", **PROFILE) as dst:
            dst.write(grid, 1)


def write_status(path, newest, n_steps, extra=None):
    status = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newest_step": newest.strftime("%Y%m%dT%H%M"),
        "stale_minutes": (datetime.now(timezone.utc) - newest).total_seconds() / 60,
        "n_steps": n_steps,
        "n_gaps": 0,
        "gap_fraction": 0.0,
        "fetched_this_cycle": 0,
        "convert_failures": 0,
    }
    status.update(extra or {})
    with open(path, "w") as f:
        json.dump(status, f)


def write_config(fusion):
    bin_release = os.path.join(REPO, "target", "release", "nowcast")
    bin_debug = os.path.join(REPO, "target", "debug", "nowcast")
    nowcast_bin = bin_release if os.path.exists(bin_release) else bin_debug
    with open(os.path.join(BASE, "config.env"), "w") as f:
        f.write(f"""BBOX="-70.75,-34.25,-69.75,-33.25"
WORK_DIR="{BASE}"
NOWCAST_BIN="{nowcast_bin}"
ID_A=14.82
ID_B=0.39
K=4.0
DT_HOURS=0.5
MAX_WINDOW=96
ALERT_LEVEL=0.5
CALIBRATOR=""
SUSC_TIF=""
WINDOW_HOURS=54
MIN_STEPS=96
STALE_HOURS=6
MAX_GAP_FRACTION=0.10
GOES_ENABLED={1 if fusion else 0}
GOES_BUCKET="noaa-goes19"
GOES_STALE_HOURS=6
DGA_ENABLED={1 if fusion else 0}
DMC_USUARIO=""
DMC_TOKEN=""
DGA_STATIONS=""
DGA_STALE_HOURS=3
COMBINE="noisy-or"
NTFY_URL="https://ntfy.sh"
NTFY_TOPIC=""
REALERT_HOURS=6
RESTALE_HOURS=12
WRITE_HAZARD_TIFS=0
""")
    return nowcast_bin


def susc_selftest():
    """El acople SUSC_TIF ↔ ALERT_LEVEL, sobre un evento MARGINAL.

    No prueba la generación del ráster (eso necesita KINGSTON montado): prueba
    lo que puede regresionar en silencio — que el umbral y la susceptibilidad
    van atados. Se fabrica un campo con la estadística real del piloto
    (mediana ~0.544, rango 0.285–0.634) y una ráfaga de 12 mm/h, apenas sobre
    la curva de Caine, que es donde vive la diferencia:

      - uniforme 1.0 + 0.5   → ALERTA (comportamiento de hoy)
      - susc real + 0.5      → QUIET: el evento se PIERDE (el fallo silencioso)
      - susc real + 0.272    → ALERTA, antes y solo en las celdas susceptibles

    Un evento extremo (50 mm/h) NO distingue los tres casos: satura el gatillo
    y alerta igual. Por eso el escenario es marginal a propósito."""
    import subprocess

    rng = np.random.default_rng(20260729)
    susc = rng.uniform(0.285, 0.634, size=(11, 11)).astype("float32")
    susc[5, 5] = 0.544                      # ancla la mediana del piloto
    susc_p = os.path.join(BASE, "susc_selftest.tif")

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6, burst_mm=6.0)  # 12 mm/h
    write_status(os.path.join(BASE, "status.json"), now, 96)
    shutil.rmtree(os.path.join(BASE, "feeds"), ignore_errors=True)
    write_config(fusion=False)
    with rasterio.open(susc_p, "w", **PROFILE) as dst:
        dst.write(susc, 1)

    base_cfg = open(os.path.join(BASE, "config.env")).read()
    casos = [("uniforme 1.0 + 0.5 (hoy)", "", 0.5, True),
             ("susc real + 0.5 (SIN re-derivar)", susc_p, 0.5, False),
             ("susc real + 0.272 (coherente)", susc_p, 0.272, True)]
    fallos = []
    for nombre, tif, nivel, debe_alertar in casos:
        cfg = os.path.join(BASE, "config_susc.env")
        with open(cfg, "w") as f:
            f.write(base_cfg + f'\nSUSC_TIF="{tif}"\nALERT_LEVEL={nivel}\n')
        shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
        subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                       env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg},
                       capture_output=True, timeout=1800)
        d = json.load(open(os.path.join(BASE, "out", "last_run.json")))
        alerta = d["n_alerts"] > 0
        ok = alerta == debe_alertar
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:34s} "
              f"{'ALERTA' if alerta else 'quiet':6s} ({d['n_alerts']} pasos, "
              f"pico {max(s['max_probability'] for s in d['steps']):.3f}) "
              f"— esperado {'ALERTA' if debe_alertar else 'quiet'}")
        if not ok:
            fallos.append(nombre)
    print(f"\n[selftest susc] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def rescue_selftest():
    """R4-H1: el dead-man switch decide sobre la UNIÓN de feeds, no el primario.

    Es el caso que el selftest --fusion NO cubría y que dejó vivir el bug: ahí
    IMERG traía sus 96 pasos y el guard de MIN_STEPS nunca se ejercitaba con el
    primario corto. Escenario medido en el evento real de Coelemu (31-jul):
    primario BAJO el mínimo (60 pasos, llovizna) + GOES sano cubriendo la
    ventana completa con la ráfaga en las horas que el primario no publicó.

      - con GOES:  la unión tiene 96 pasos reales → el ciclo CORRE y ALERTA
      - sin GOES:  la unión ES el primario (60 < 96) → 'datos insuficientes',
        exit 0, sin last_run.json — la semántica de siempre, intacta
    """
    import subprocess

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    # primario corto y RECIENTE en llovizna (5 h de atraso: bajo el umbral
    # stale de 6 h, para no mezclar este test con el aviso de feed caído)
    imerg_end = now - timedelta(hours=5)
    write_steps(os.path.join(BASE, "steps"), imerg_end, 60, burst_last=0)
    write_status(os.path.join(BASE, "status.json"), imerg_end, 60)
    shutil.rmtree(os.path.join(BASE, "feeds"), ignore_errors=True)
    goes_dir = os.path.join(BASE, "feeds", "goes")
    write_steps(os.path.join(goes_dir, "steps"), now, 96, burst_last=4)
    write_status(os.path.join(goes_dir, "status.json"), now, 96,
                 {"feed": "goes-qpe:selftest"})
    write_config(fusion=True)

    base_cfg = open(os.path.join(BASE, "config.env")).read()
    casos = [("primario corto + GOES sano (rescate)", "GOES_ENABLED=1\nDGA_ENABLED=0", True),
             ("primario corto, sin GOES (control)", "GOES_ENABLED=0\nDGA_ENABLED=0", False)]
    fallos = []
    for nombre, override, debe_correr in casos:
        cfg = os.path.join(BASE, "config_rescue.env")
        with open(cfg, "w") as f:
            f.write(base_cfg + "\n" + override + "\n")
        shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
        rj = os.path.join(BASE, "out", "last_run.json")
        if os.path.exists(rj):
            os.remove(rj)
        r = subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                           env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg},
                           capture_output=True, text=True, timeout=1800)
        corrio = os.path.exists(rj)
        alerto = False
        if corrio:
            d = json.load(open(rj))
            alerto = d["n_alerts"] > 0
        ok = (corrio == debe_correr) and (alerto if debe_correr else r.returncode == 0) \
            and (debe_correr or "datos insuficientes" in r.stdout + r.stderr
                 or os.path.exists(os.path.join(BASE, "state", "coldstart")))
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:38s} "
              f"{'corrió y ALERTÓ' if alerto else ('corrió sin alerta' if corrio else 'no corrió')}"
              f" — esperado {'ALERTA' if debe_correr else 'no correr (exit 0)'}")
        if not ok:
            fallos.append(nombre)
    print(f"\n[selftest rescue] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def flood_selftest():
    """R5-H3: el path de CRECIDAS corre en el ciclo y respeta el modo sombra.

    El caso que lo motivó (Puerto Viejo, 17-ago-2026) es "I-D en quiet y crecida
    en curso": el aluvión ocurrió con 1.5 mm en su propia celda y ~30 mm en la
    cabecera, 100 km al este. Así que lo que hay que guardar no es solo que el
    paso corra, sino que corra CUANDO EL I-D ESTÁ EN QUIET — si se enganchara
    dentro de la rama de alerta, el evento que motivó todo pasaría de largo.

    Sin red y sin KINGSTON: se fabrica una cuenca sintética cuyas celdas son el
    cuadrante donde `write_steps` planta la ráfaga, y un Q_c bajo pero > 0.
    """
    import subprocess

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    bdir = os.path.join(BASE, "flood_basins")
    shutil.rmtree(bdir, ignore_errors=True)
    os.makedirs(bdir, exist_ok=True)
    # write_steps pone la ráfaga en grid[2:5, 6:9] de una grilla 11×11.
    celdas = [[r, c, 1.0 / 9.0] for r in range(2, 5) for c in range(6, 9)]
    art = {
        "slug": "sintetica", "comunas": ["Selftest"], "area_km2": 1000.0,
        "cn": 88.0, "tc_horas": 12.0, "lambda": "arido", "dt_dias": 1.0,
        "q_crit": 5.0, "q_crit_p": 0.995, "sin_aforo": True,
        "q_crit_origen": "selftest (serie sintetica)", "celdas_0p1": celdas,
    }
    with open(os.path.join(bdir, "sintetica.json"), "w") as f:
        json.dump(art, f)

    runner = os.path.join(REPO, "target", "release", "nowcast-flood")
    if not os.access(runner, os.X_OK):
        print(f"  FALLA runner ausente: {runner}\n"
              "        compilar: cargo build --release -p nowcast-rainflow --bin nowcast-flood")
        return 1

    fallos = []
    # (nombre, burst_last, debe_exceder)
    for nombre, burst, debe in (("ventana seca → no excede", 0, False),
                                ("ráfaga → excede", 6, True)):
        write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=burst)
        write_status(os.path.join(BASE, "status.json"), now, 96)
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "monitor", "flood_step.py"),
             "--work", BASE, "--runner", runner],
            capture_output=True, text=True, timeout=900)
        excede = r.returncode == 2
        ok = excede == debe
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:26s} rc={r.returncode} "
              f"{'(excede)' if excede else '(bajo Q_c)'}")
        if not ok:
            fallos.append(nombre)
            print("        " + (r.stdout + r.stderr).strip()[:300])

    # El escenario de Puerto Viejo en miniatura: lluvia que NO gatilla la ladera
    # pero SÍ la cuenca. Una ráfaga fuerte no sirve para este test — con
    # susceptibilidad uniforme 1.0 el peligro I-D satura en 1.00 y supera
    # cualquier nivel. Con 1.5 mm por medio hora la intensidad queda en 3 mm/h,
    # muy por debajo de Caine a 3 h (~9.6 mm/h) → factor de gatillo ~0.06 → I-D
    # quiet; pero el total de 24 h (~17 mm) produce ~4.9 mm/día de escorrentía
    # Hortoniana, sobre un Q_c de 3 → la crecida sí excede. Es exactamente la
    # disociación que hace falta guardar.
    write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6, burst_mm=1.5)
    write_status(os.path.join(BASE, "status.json"), now, 96)
    art["q_crit"] = 3.0
    with open(os.path.join(bdir, "sintetica.json"), "w") as f:
        json.dump(art, f)
    write_config(fusion=False)
    base_cfg = open(os.path.join(BASE, "config.env")).read()
    for nombre, notify_flag, debe_notificar in (("sombra: NO notifica", "0", False),
                                                ("FLOOD_NOTIFY=1: notifica", "1", True)):
        cfg = os.path.join(BASE, "config_flood.env")
        with open(cfg, "w") as f:
            f.write(base_cfg + '\nALERT_LEVEL=0.5\nNTFY_TOPIC=""\n')
        shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
        r = subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                           env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg,
                                "FLOOD_NOTIFY": notify_flag},
                           capture_output=True, text=True, timeout=1800)
        salida = r.stdout + r.stderr
        corrio = "[flood]" in salida
        quiet = "quiet —" in salida
        notifico = "NOTIFY[urgent] nowcast-monitor: ALERTA CRECIDA" in salida
        sombra = "MODO SOMBRA" in salida
        ok = corrio and quiet and (notifico == debe_notificar) and (sombra != debe_notificar)
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:26s} corrió={corrio} "
              f"I-D quiet={quiet} notificó={notifico}")
        if not ok:
            fallos.append(nombre)

    print(f"\n[selftest flood] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def notify_contract_selftest():
    """R5-H2: la histéresis solo se estampa tras una ENTREGA confirmada.

    Estampar antes convertía un fallo de entrega en REALERT_HOURS de silencio.
    Medido: con NTFY_TOPIC vacío el dominio atacama anotó "notificado" 10 h sin
    que saliera un aviso, y con el canal ya arreglado la marca falsa tapaba la
    alerta VIVA 6 h más. El invariante que se guarda acá es "no se marca lo que
    no se entregó", y su corolario operativo —que importa más— es que un canal
    roto REINTENTA cada ciclo en vez de callarse.
    """
    import http.server
    import subprocess
    import threading

    recibidos = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            recibidos.append(self.headers.get("Title", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"selftest"}')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    puerto = srv.server_address[1]

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6)
    write_status(os.path.join(BASE, "status.json"), now, 96)
    write_config(fusion=False)
    base_cfg = open(os.path.join(BASE, "config.env")).read()

    # (topic, url, debe_estamparse, debe_llegar_al_servidor)
    casos = [
        ("canal OK → SÍ marca", "selftest", f"http://127.0.0.1:{puerto}", True, True),
        ("envío falla → NO marca", "selftest", "http://127.0.0.1:1", False, False),
        ("topic vacío → NO marca", "", "http://127.0.0.1:1", False, False),
    ]
    marca = os.path.join(BASE, "state", "alert")
    fallos = []
    for nombre, topic, url, debe_marcar, debe_llegar in casos:
        cfg = os.path.join(BASE, "config_notify.env")
        with open(cfg, "w") as f:
            f.write(base_cfg + f'\nNTFY_TOPIC="{topic}"\nNTFY_URL="{url}"\n')
        shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
        antes = len(recibidos)
        r = subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                           env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg},
                           capture_output=True, text=True, timeout=1800)
        alerto = "NOTIFY" in r.stdout + r.stderr
        marcado = os.path.exists(marca)
        llego = len(recibidos) > antes
        ok = alerto and (marcado == debe_marcar) and (llego == debe_llegar)
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:26s} alertó={alerto} "
              f"marca={'sí' if marcado else 'no':2s} (esperado {'sí' if debe_marcar else 'no'})"
              f" entregado={'sí' if llego else 'no'}")
        if not ok:
            fallos.append(nombre)

    # Corolario: con el canal roto, DOS ciclos seguidos avisan (no hay silencio).
    cfg = os.path.join(BASE, "config_notify.env")
    with open(cfg, "w") as f:
        f.write(base_cfg + '\nNTFY_TOPIC="selftest"\nNTFY_URL="http://127.0.0.1:1"\n')
    shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
    avisos = 0
    for _ in range(2):
        r = subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                           env={**os.environ, "SKIP_FETCH": "1", "CONFIG": cfg},
                           capture_output=True, text=True, timeout=1800)
        salida = r.stdout + r.stderr
        if "NOTIFY" in salida and "suprimida por histéresis" not in salida:
            avisos += 1
    ok = avisos == 2
    print(f"  {'ok  ' if ok else 'FALLA'} {'canal roto reintenta':26s} "
          f"{avisos}/2 ciclos avisaron (esperado 2/2: sin entrega no hay silencio)")
    if not ok:
        fallos.append("canal roto no reintenta")

    srv.shutdown()
    print(f"\n[selftest notify-contract] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


HDF5_FAMILIA = ("xarray", "h5py", "h5netcdf", "netCDF4", "cfgrib")


def exit_contract_selftest():
    """R5-H1: el segfault de teardown que descartaba ciclos EXITOSOS (12-ago-2026).

    Un script que abre HDF5 y además usa rasterio termina con DOS copias de
    HDF5 en el proceso; al finalizar el intérprete cada una desarma la suya y
    el proceso muere con SIGSEGV DESPUÉS de que todo el trabajo quedó en disco.
    monitor.sh y forecast_all.sh solo miran el exit code, así que descartaban
    un ciclo bueno: medido en atacama con status.json fresco, 24 gránulos y 0
    fallos de conversión, el ciclo igual se anunció como "FETCH FALLÓ — El
    monitor NO corrió".

    Este test NO reproduce el segfault (haría falta bajar HDF5 real, o sea red).
    Guarda el INVARIANTE que impide la reincidencia, que es lo que falló acá: el
    patrón ya estaba curado en forecast_gfs/fetch_icon/fetch_ecmwf desde antes y
    nadie lo propagó a los fetchers del monitor. Por eso el chequeo se hace
    sobre la FAMILIA y no sobre los archivos nombrados: si mañana aparece un
    script nuevo que mezcle los dos stacks, este test falla hasta que lo blinden.
    """
    import subprocess

    mdir = os.path.join(REPO, "monitor")
    fallos = []

    # (1) invariante estructural: toda hermana que mezcle los stacks sale duro.
    hermanas = []
    for f in sorted(os.listdir(mdir)):
        if not f.endswith(".py") or f == "selftest.py":
            continue
        src = open(os.path.join(mdir, f)).read()
        tiene_hdf5 = any(f"import {m}" in src or f"from {m}" in src for m in HDF5_FAMILIA)
        if tiene_hdf5 and ("import rasterio" in src or "from rasterio" in src):
            hermanas.append(f)
    print(f"  hermanas HDF5+GDAL detectadas: {len(hermanas)}")
    for f in hermanas:
        src = open(os.path.join(mdir, f)).read()
        ok = "os._exit(" in src
        print(f"  {'ok  ' if ok else 'FALLA'} {f:24s} salida dura {'presente' if ok else 'AUSENTE'}")
        if not ok:
            fallos.append(f"{f}: sin os._exit")

    # (2) contrato dinámico, sin red: éxito → 0, config incompleta → != 0.
    #     Solo los dos fetchers del monitor admiten SKIP_FETCH; flood_salado
    #     necesita CR2MET en disco, así que queda cubierto solo por (1).
    work = os.path.join(BASE, "exitcontract")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)
    write_steps(os.path.join(work, "steps"), now, 96)
    base = {**os.environ, "SKIP_FETCH": "1", "WORK_DIR": work,
            "BBOX": "-70.80,-34.30,-69.70,-33.20"}
    casos = [
        ("fetch_imerg_early.py", "éxito sin red", base, 0),
        ("fetch_imerg_early.py", "falta BBOX", {k: v for k, v in base.items() if k != "BBOX"}, 1),
        ("fetch_goes_qpe.py", "falta BBOX", {k: v for k, v in base.items() if k != "BBOX"}, 1),
    ]
    for script, nombre, env_, esperado in casos:
        r = subprocess.run([MONITOR_PY, os.path.join(mdir, script)],
                           env=env_, capture_output=True, text=True, timeout=600)
        ok = (r.returncode == 0) if esperado == 0 else (r.returncode != 0)
        print(f"  {'ok  ' if ok else 'FALLA'} {script:24s} {nombre:16s} "
              f"exit={r.returncode} (esperado {'0' if esperado == 0 else '!=0'})")
        if not ok:
            fallos.append(f"{script} [{nombre}]: exit {r.returncode}")

    print(f"\n[selftest exit-contract] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main(fusion=False):
    shutil.rmtree(os.path.join(BASE, "state"), ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "out"), ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 30)

    if not fusion:
        # Modo base: un solo feed con la ráfaga al final.
        write_steps(os.path.join(BASE, "steps"), now, 96, burst_last=6)
        write_status(os.path.join(BASE, "status.json"), now, 96)
        shutil.rmtree(os.path.join(BASE, "feeds"), ignore_errors=True)
        write_config(fusion=False)
        print(f"selftest listo: 96 pasos sintéticos (ráfaga al final) en {BASE}/steps")
    else:
        # Fase A: IMERG rezagado 5 h con pura llovizna; la ráfaga vive SOLO en
        # GOES, en las horas que IMERG aún no publica. DGA sin token.
        imerg_end = now - timedelta(hours=5)
        write_steps(os.path.join(BASE, "steps"), imerg_end, 96, burst_last=0)
        write_status(os.path.join(BASE, "status.json"), imerg_end, 96)
        goes_dir = os.path.join(BASE, "feeds", "goes")
        write_steps(os.path.join(goes_dir, "steps"), now, 20, burst_last=4)
        write_status(os.path.join(goes_dir, "status.json"), now, 20,
                     {"feed": "goes-qpe:selftest"})
        dga_dir = os.path.join(BASE, "feeds", "dga")
        os.makedirs(dga_dir, exist_ok=True)
        with open(os.path.join(dga_dir, "status.json"), "w") as f:
            json.dump({"feed": "dga-dmc", "disabled": True, "reason": "selftest"}, f)
        write_config(fusion=True)
        # El selftest del parser DGA corre aquí mismo (sin red).
        import subprocess

        subprocess.run(
            [sys.executable, os.path.join(REPO, "monitor", "fetch_dga.py"), "--selftest"],
            check=True, cwd=os.path.join(REPO, "monitor"),
        )
        print(f"selftest fusión listo: IMERG hasta {imerg_end:%H:%M}Z (llovizna), "
              f"GOES hasta {now:%H:%M}Z (ráfaga solo-GOES), DGA deshabilitado")
        print("debe ALERTAR por fusión; con GOES_ENABLED=0 en el config debe quedar quiet")
    print(f"correr: SKIP_FETCH=1 CONFIG={BASE}/config.env {REPO}/monitor/monitor.sh")


def h0_selftest():
    """H0 en sombra de punta a punta (2026-09-25): el ciclo base con ráfaga debe
    ALERTAR igual con H0 encendido, con H0 viejo y sin H0, y el peligro debe ser
    idéntico en los tres — H0 informa el mecanismo, nunca mueve el veredicto.

    · serie fresca → la alerta lleva "H0 (sombra):" y last_run.json trae h0;
    · serie vieja  → WARN "H0 (sombra) apagado", la alerta sale sin H0.
    """
    import subprocess

    fallos = []
    now = datetime.now(timezone.utc).date()
    serie = os.path.join(BASE, "h0.csv")

    def ciclo(h0_csv, dias_atras):
        main(fusion=False)
        with open(serie, "w") as f:
            f.write("fecha,h0_m,tormenta\n")
            for d in range(90, dias_atras - 1, -1):
                f.write(f"{now - timedelta(days=d)},{3000 + 10 * (d % 7)},{int(d % 3 == 0)}\n")
        with open(os.path.join(BASE, "config.env"), "a") as f:
            f.write(f'H0_CSV="{h0_csv}"\nH0_STALE_DAYS=3\nH0_ABS_M=""\n')
        log = os.path.join(BASE, "monitor.log")
        if os.path.exists(log):
            os.remove(log)
        r = subprocess.run([os.path.join(REPO, "monitor", "monitor.sh")],
                           env={**os.environ, "SKIP_FETCH": "1",
                                "CONFIG": os.path.join(BASE, "config.env")},
                           capture_output=True, text=True, timeout=600)
        run = json.load(open(os.path.join(BASE, "out", "last_run.json")))
        return r.returncode, open(log).read(), run

    casos = {}
    for nombre, csv_, atras in [("fresca", serie, 0), ("vieja", serie, 10), ("sin H0", "", 0)]:
        rc, log, run = ciclo(csv_, atras)
        casos[nombre] = run
        alerta = "NOTIFY[urgent] nowcast-monitor: ALERTA I-D" in log
        con_h0 = "H0 (sombra):" in log
        apagado = "H0 (sombra) apagado" in log
        esperado = {"fresca": (True, True, False), "vieja": (True, False, True),
                    "sin H0": (True, False, False)}[nombre]
        ok = rc == 0 and (alerta, con_h0, apagado) == esperado
        print(f"  {'ok  ' if ok else 'FALLA'} serie {nombre:7s} rc={rc} alerta={alerta} "
              f"H0 en aviso={con_h0} apagado={apagado}")
        if not ok:
            fallos.append(nombre)
    tiene = "h0" in casos["fresca"] and all("h0" in s for s in casos["fresca"]["steps"])
    print(f"  {'ok  ' if tiene else 'FALLA'} last_run.json trae h0 en cabecera y en cada paso")
    if not tiene:
        fallos.append("json sin h0")
    quita = lambda r: [(s["step"], s["max_probability"], s["alert"]) for s in r["steps"]]
    igual = quita(casos["fresca"]) == quita(casos["vieja"]) == quita(casos["sin H0"])
    print(f"  {'ok  ' if igual else 'FALLA'} peligro idéntico con H0, con H0 viejo y sin H0")
    if not igual:
        fallos.append("H0 movió el peligro")
    print(f"\n[selftest h0] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def watchdog_selftest():
    """El vigía de servicios detenidos (2026-09-26), sin systemd ni red.

    Motivo: 23 días con monitor y pronóstico caídos sin que se notara. Casos:
    fresco → calla; viejo → URGENTE entregado; segunda pasada → histéresis;
    sin last_ok pero visto hace mucho → también DETENIDO; vuelve → RECUPERADO
    una sola vez. Canal local, nunca el ntfy real.
    """
    import http.server
    import subprocess
    import threading
    import time

    recibidos = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            recibidos.append((self.headers.get("Priority", ""), self.headers.get("Title", "")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wd = os.path.join(BASE, "watchdog")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd)
    ok_mon = os.path.join(wd, "mon_last_ok")
    ok_fc = os.path.join(wd, "fc_last_ok")   # nunca se crea: caso "sin last_ok"
    ahora = int(time.time())

    def correr():
        antes = len(recibidos)
        env = {**os.environ, "LOCAL_CONFIG": "/nonexistent", "WATCHDOG_DIR": wd,
               "NTFY_TOPIC": "selftest", "NTFY_URL": f"http://127.0.0.1:{srv.server_address[1]}",
               "WATCHDOG_TARGETS": f"monitor@x|{ok_mon}|3;forecast|{ok_fc}|14"}
        # config.env deja NTFY_TOPIC vacío: se inyecta DESPUÉS vía un config propio
        cfg = os.path.join(wd, "cfg.env")
        with open(cfg, "w") as f:
            f.write(f'NTFY_TOPIC="selftest"\nNTFY_URL="{env["NTFY_URL"]}"\n')
        env["CONFIG"] = cfg
        subprocess.run([os.path.join(REPO, "monitor", "watchdog.sh")], env=env,
                       capture_output=True, text=True, timeout=120, check=True)
        return recibidos[antes:]

    fallos = []

    def caso(nombre, got, esperado):
        ok = got == esperado
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:44s} {got}")
        if not ok:
            fallos.append(nombre)

    open(ok_mon, "w").write(str(ahora - 600))          # hace 10 min
    caso("fresco → silencio (y registra first_seen)", correr(), [])
    open(ok_mon, "w").write(str(ahora - 5 * 3600))     # hace 5 h > 3 h
    caso("monitor viejo → URGENTE", correr(), [("urgent", "nowcast: monitor@x DETENIDO")])
    caso("segunda pasada → histéresis", correr(), [])
    # forecast sin last_ok: se envejece su first_seen más allá de 14 h
    open(os.path.join(wd, "first_seen_forecast"), "w").write(str(ahora - 15 * 3600))
    caso("sin last_ok, visto hace 15 h → URGENTE", correr(),
         [("urgent", "nowcast: forecast DETENIDO")])
    open(ok_mon, "w").write(str(ahora))
    caso("monitor vuelve → RECUPERADO una vez", correr(),
         [("default", "nowcast: monitor@x RECUPERADO")])
    caso("…y no lo repite", [r for r in correr() if "monitor" in r[1]], [])
    srv.shutdown()
    print(f"\n[selftest watchdog] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


if __name__ == "__main__":
    if "--susc" in sys.argv:
        sys.exit(susc_selftest())
    if "--rescue" in sys.argv:
        sys.exit(rescue_selftest())
    if "--exit-contract" in sys.argv:
        sys.exit(exit_contract_selftest())
    if "--notify-contract" in sys.argv:
        sys.exit(notify_contract_selftest())
    if "--flood" in sys.argv:
        sys.exit(flood_selftest())
    if "--h0" in sys.argv:
        sys.exit(h0_selftest())
    if "--watchdog" in sys.argv:
        sys.exit(watchdog_selftest())
    main(fusion="--fusion" in sys.argv)
