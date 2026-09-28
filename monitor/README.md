# nowcast-monitor — monitor operacional de eventos (multi-feed)

Monitor local de geohazards gatillados por lluvia: cada 30 minutos ingiere
hasta tres forzantes — IMERG **Early** NRT (30 min / 0.1°, ~4-5 h de
latencia), **GOES-East QPE** (ABI-L2-RRQPEF, 10 min, latencia de MINUTOS,
bucket AWS público sin credenciales) y **telemetría DGA** (pluviómetros
in-situ vía API DMC, ~1 h) — las fusiona por **noisy-OR** con
`nowcast run --fuse-rasters`, corre el motor I-D sobre una ventana rodante de
48 h y notifica cruces de umbral vía ntfy. Pensado para un nodo liviano del
home cluster (p. ej. `sentinel`).

## Qué ES y qué NO es (honestidad operacional)

- **ES** un monitor de *eventos en curso*: la fase A (GOES + DGA fusionados)
  baja la latencia efectiva de ~5 h a **~15-60 minutos** — detecta que una
  tormenta cruzó el umbral I-D mientras aún está ocurriendo.
- **Sigue sin ser** alerta temprana en sentido estricto. La fase B está
  construida (`qpf_ensemble.py`, pySTEPS sobre GOES) y tiene +22% de skill
  sobre persistencia, pero NO da lead time todavía: GOES QPE solo nunca cruza
  el umbral I-D en lluvia orográfica templada (medido en el evento de Coelemu),
  y un QPF por extrapolación hereda ese sesgo. Falta ajuste de QPE contra
  IMERG/pluviómetros antes de extrapolar. Ver "Fase B" abajo.
- La fusión corre sobre la **unión** de ventanas: donde un feed no tiene dato
  (la cola rezagada de IMERG, el arranque en frío de DGA) entra como cero y
  no aporta al noisy-OR; los feeds rápidos cubren esas horas. Todo relleno
  queda contado y logueado (`prepare_fusion.py`). Un feed secundario caído
  degrada la fusión y se avisa; nunca detiene el ciclo.
- Corre con **susceptibilidad real** de terreno (`make_susc_grid.py`), así que
  discrimina *dónde*, no solo *cuándo*. `SUSC_TIF` y `ALERT_LEVEL` van ATADOS:
  cambiar uno sin re-derivar el otro pierde eventos marginales en silencio.
  Con `SUSC_TIF` vacío vuelve a ser monitor de **timing** (uniforme 1.0) y hay
  que restaurar `ALERT_LEVEL=0.5`.
- Antes de creerle a una sola notificación: **una temporada en modo sombra**,
  comparando el log contra reportes SERNAGEOMIN. Un monitor que nadie ha
  visto fallar es una demo.

## Arquitectura (sin estado, a propósito)

```
systemd timer (30 min)
  └─ monitor.sh
       ├─ fetch_imerg_early.py   # feed PRIMARIO → steps/step_*.tif (KB c/u)
       │    · baja SOLO gránulos nuevos, recorta bbox, borra el HDF5
       │    · huecos viejos → raster de 0 mm MARCADO (gapmark_*) + conteo
       │    · rotación de la ventana + status.json
       ├─ fetch_goes_qpe.py      # feed GOES → feeds/goes/steps/ (mismo contrato)
       │    · RRQPE full disk 10 min desde AWS anónimo (~1.5 MB/gránulo)
       │    · reproyección geoestacionaria→0.1° (fórmulas GOES-R PUG, sin
       │      pyproj), 3 gránulos = 1 paso semihorario, MISMA grilla que IMERG
       ├─ fetch_dga.py           # feed DGA → feeds/dga/steps/ (requiere token DMC)
       │    · API Servicios Climáticos DMC (grupo EstacionesDGA, ~12 h, minuto)
       │    · parser defensivo (acumulado/intervalo, reset de pluviómetro),
       │      IDW a la grilla común; sin token se deshabilita solo
       ├─ dead-man switch        # por feed: primario aborta, secundarios avisan
       ├─ prepare_fusion.py      # unión de ventanas + cero-relleno CONTADO
       ├─ nowcast run --rain-rasters ... --fuse-rasters ... --combine noisy-or
       └─ notificación con histéresis (cruces, no estados; re-aviso c/6 h)
```

Cada ciclo **re-corre la ventana rodante completa** con el motor batch. Esto
es legítimo porque batch ≡ streaming es bit-idéntico (testeado a nivel de
bits): no hay estado del motor que persistir, un reinicio del nodo no pierde
nada, y el costo (96 pasos × ~100 celdas) son milisegundos.

Principios heredados de las auditorías:
- **El silencio del feed ≠ calma meteorológica**: dead-man switch con aviso
  `urgent` propio (la lección del `--alert-level NaN` a escala de sistema).
- **Nada se imputa en silencio**: los huecos se rellenan con 0 mm porque el
  eje temporal debe ser contiguo, pero quedan marcados, contados, reportados,
  y sobre `MAX_GAP_FRACTION` la corrida se declara DEGRADADA.
- El log local es la fuente de verdad; ntfy es best-effort.

## Despliegue en un nodo del cluster

```bash
# 0. En el nodo destino (Ubuntu 24.04+ es PEP 668: usar venv):
#      python3 -m venv ~/nowcast-monitor/venv
#      ~/nowcast-monitor/venv/bin/pip install --no-cache-dir \
#          -r monitor/requirements-monitor.txt
#    (versiones FIJAS, las mismas del orquestador; ~320 MB. cftime NO es
#    opcional: sin él xarray no decodifica el calendario juliano de IMERG.
#    Hasta 2026-09-27 aquí se instalaba una lista suelta sin versiones con
#    h5py/h5netcdf; el archivo usa netCDF4 como motor — el que de verdad abre
#    IMERG y GOES en producción — y deja fuera la segunda copia de HDF5.)
#    monitor.sh antepone ~/nowcast-monitor/venv/bin al PATH solo (MONITOR_VENV
#    para otra ruta) y ABORTA con "CONFIG INVÁLIDA" si el venv no existe: ya
#    no hace falta prependerlo en config.local.env.
#    y ~/.netrc con las credenciales Earthdata (app "NASA GESDISC DATA ARCHIVE"
#    autorizada — la misma cuenta ya usada por scripts/extract_event_imerg.py).

# 1. Compilar el binario (en el orquestador; el nodo no necesita toolchain):
cargo build --release -p nowcast-cli
#    En el propio orquestador, INSTALARLO fuera de target/ — el monitor y el
#    pronóstico lo leen de ~/nowcast-monitor/bin/nowcast, nunca de target/:
install -Dm755 target/release/nowcast ~/nowcast-monitor/bin/nowcast
#    Lección (2026-09-25): con NOWCAST_BIN apuntando a target/release, un borrado
#    de target/ (liberando disco) dejó monitor@atacama Y nowcast-forecast caídos
#    23 días (3-sep → 25-sep). Es una COPIA, no un symlink: sobrevive a cargo
#    clean. Contrapartida: tras recompilar hay que volver a instalar, o el
#    monitor sigue con el binario viejo (y un flag nuevo en config → error de
#    uso, exit 1, aviso "ERROR del motor", no una alerta falsa).
#    La cadena de PRONÓSTICO (forecast_all.sh + analysis_all.sh) corre en su
#    propio venv con versiones fijas — distinto del venv del monitor del paso 0:
python3 -m venv ~/nowcast-monitor/venv-forecast
~/nowcast-monitor/venv-forecast/bin/pip install -r monitor/requirements-forecast.txt
#    Lección (2026-09-26): el wheel de geostat-rs subió numpy a 2.5.3 en
#    ~/.local el 7-sep y el geopandas 0.14 del sistema abortó comuna_impact.
#    Sin el venv, los dos scripts ABORTAN en vez de caer al python3 del sistema.

# 1b. Generar la susceptibilidad de terreno EN EL ORQUESTADOR — no en el nodo:
#     lee los modelos por cuenca de 30 m del disco KINGSTON, que el nodo no
#     tiene montado. Es terreno: se genera UNA vez, no por ciclo. Imprime el
#     ALERT_LEVEL coherente con el ráster que acaba de escribir; hay que
#     copiarlo a config.env (ver paso 3) o el umbral y la susceptibilidad
#     quedan descoordinados y se pierden los eventos marginales.
python3 monitor/make_susc_grid.py --out /tmp/susc_0p1.tif   # BBOX del config.env

# 2. Copiar al nodo (ej: sentinel):
ssh sentinel "mkdir -p ~/nowcast-monitor/bin ~/nowcast-monitor/monitor"
scp target/release/nowcast sentinel:~/nowcast-monitor/bin/
scp /tmp/susc_0p1.tif sentinel:~/nowcast-monitor/          # $WORK_DIR/susc_0p1.tif
scp monitor/{config.env,fetch_imerg_early.py,fetch_goes_qpe.py,fetch_dga.py,prepare_fusion.py,monitor.sh,lib_notify.sh,h0_guard.py,watchdog.sh} \
    sentinel:~/nowcast-monitor/monitor/
ssh sentinel "chmod +x ~/nowcast-monitor/monitor/monitor.sh && ls -la ~/nowcast-monitor/bin"  # verificar bytes

# 3. Configurar: editar ~/nowcast-monitor/monitor/config.env en el nodo
#    (BBOX, NTFY_TOPIC — crear un topic privado en ntfy.sh, NOWCAST_BIN, y el
#    ALERT_LEVEL que imprimió el paso 1b si cambiaste BBOX o el ráster).
#    monitor.sh ABORTA si SUSC_TIF apunta a un ráster que no está: no cae a
#    uniforme 1.0 en silencio, porque el ALERT_LEVEL del config está derivado
#    PARA la susceptibilidad y sin ella alertaría de más.
#    Fase A: GOES no necesita credenciales (bucket público). Para el feed
#    in-situ, registrarse GRATIS en
#    https://climatologia.meteochile.gob.cl/application/usuario/registroUsuario
#    y poner DMC_USUARIO + DMC_TOKEN en monitor/config.local.env (NO en
#    config.env: ese archivo se versiona; config.local.env está gitignored y
#    monitor.sh lo sourcea encima). Copiarlo al nodo aparte:
#      scp monitor/config.local.env sentinel:~/nowcast-monitor/monitor/
#    Sin token la fusión corre con IMERG+GOES y el feed queda deshabilitado.

# 4. Primer ciclo a mano (arranque en frío: baja ~la ventana completa, ~1 GB
#    de tránsito que se reduce a KB en disco; los ciclos siguientes bajan 2-8
#    gránulos):
ssh sentinel "cd ~/nowcast-monitor/monitor && ./monitor.sh"

# 5. Programar (systemd de usuario):
scp monitor/systemd/nowcast-monitor.{service,timer} sentinel:~/.config/systemd/user/
ssh sentinel "systemctl --user daemon-reload && systemctl --user enable --now nowcast-monitor.timer && loginctl enable-linger $USER"

# 6. Vigía de servicios detenidos (OBLIGATORIO, no opcional): avisa URGENTE si
#    un monitor/pronóstico con timer habilitado no completa un ciclo en
#    WATCHDOG_MONITOR_HOURS (3) / WATCHDOG_FORECAST_HOURS (14), y "RECUPERADO"
#    al volver. monitor.sh y lib_notify.sh van juntos: sin lib_notify.sh el
#    ciclo aborta al arrancar (y el vigía es quien lo delata).
scp monitor/systemd/nowcast-watchdog.{service,timer} sentinel:~/.config/systemd/user/
ssh sentinel "systemctl --user daemon-reload && systemctl --user enable --now nowcast-watchdog.timer"
```

Cadencia: el timer corre a los minutos :12 y :42 (IMERG Early publica cada
media hora con ~4 h de retraso; el desfase evita pedir justo al filo).

## Prueba local sin red

```bash
# Modo base — un solo feed con tormenta al final:
python3 monitor/selftest.py         # crea /tmp/nowcast-monitor-selftest
SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh
# Debe terminar en NOTIFY[urgent] ALERTA con el detalle del cruce.

# Vigía de servicios detenidos (canal ntfy local, sin systemd):
python3 monitor/selftest.py --watchdog

# Modo fusión (fase A) — IMERG rezagado 5 h con llovizna, ráfaga SOLO en GOES:
python3 monitor/selftest.py --fusion
SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh
# Debe ALERTAR vía fusión (la ráfaga vive en horas que IMERG aún no publica);
# con GOES_ENABLED=0 el mismo ciclo queda quiet — esa diferencia ES la
# latencia que la fase A recupera. Incluye fetch_dga.py --selftest (parser).

# Enriquecedor de clase fan-apex (etapa 5b de analysis_all.sh) — entradas
# hostiles, sin red ni KINGSTON: verifica que ninguna comuna que falle tumbe
# el lote ni escriba un bloque que afirme una clase que no calculó.
python3 monitor/fanapex_localidades.py --selftest

# Acople SUSC_TIF <-> ALERT_LEVEL sobre un evento MARGINAL (12 mm/h, apenas
# sobre Caine), que es donde vive la diferencia: uniforme+0.5 alerta, susc
# real+0.5 PIERDE el evento, susc real+0.272 lo recupera. Un evento extremo
# no distingue los tres casos — satura el gatillo y alerta igual.
python3 monitor/selftest.py --susc
```

## Retención de ciclos (el disco NO se poda solo)

`forecast_all.sh` + `analysis_all.sh` corren 4×/día y dejan ~9 directorios de
ciclo diarios de ~1.1 GB. Nada los borraba: medido el 2026-08-01, en 19 días
`~/nowcast-forecast` llegó a 80 GB y el disco de la máquina a 100%.

```bash
python3 monitor/prune_forecast.py                    # informe, NO borra
python3 monitor/prune_forecast.py --apply            # retención 7 d / 10 d
python3 monitor/prune_forecast.py --keep 20260731 --apply   # protege un evento
```

Qué borra y por qué se puede: el 98% del volumen son los cruces
`hazard_x_susc_30m_*`, que son **caché, no dato** — `susc_overlay.py:319` los
calcula como `hazard_0.1° × susceptibilidad_KINGSTON`, y ambos insumos
sobreviven (el hazard 0.1° pesa 0.23 MB por dominio y se conserva; la
susceptibilidad vive en KINGSTON). Regenerar un ciclo: `python3
susc_overlay.py --forecast <ciclo>`, minutos y sin red. El crudo grib2/bz2 sí
es irrecuperable pasados ~10 días en NOMADS, pero ya produjo el hazard 0.1°
que se conserva; solo haría falta para re-derivar con otro umbral I-D.

NUNCA toca `informes/` (el registro de constancia con su hash), `runouts/`,
los `.json` ni los rásters de 0.1°. Marcha en seco por defecto: borrar exige
`--apply` explícito.

Está **cableado como etapa 7 de `analysis_all.sh`**, al final (no puede tocar
los productos del ciclo recién generado), y gobernado por `PRUNE_MODE`:

| `PRUNE_MODE` | efecto |
|---|---|
| `report` (default) | no borra; loguea cuánto liberaría — el problema queda visible cada ciclo |
| `apply` | poda de verdad (retención `PRUNE_DIAS_30M`=7 / `PRUNE_DIAS_CRUDO`=10) |
| `off` | ni siquiera reporta |

El default es `report` a propósito: borrar datos en automático es decisión del
operador, no algo que se herede de un fix. Ritmo medido: ~10 GB/día, así que en
modo `report` el log dirá cuánto se está acumulando hasta que decidas.

## Limitaciones conocidas (léelas)

1. **Latencia**: ~15-60 min con la fusión de fase A operando (GOES/DGA); ~5 h
   si solo queda IMERG. Monitor de eventos en curso; el lead positivo llega
   con la fase B (QPF por ensemble).
1b. **GOES QPE no es radar**: el hydroestimator geoestacionario sub/sobre-
   estima según el tipo de nube (convección fría bien, lluvia orográfica
   templada peor — relevante en la cordillera en invierno). El modo sombra
   mide su sesgo local antes de confiarle umbrales.
1c. **El feed in-situ usa las EMAs de la DMC, no los pluviómetros DGA de alta
   cordillera**: verificado contra la API real (2026-07-11) — las estaciones
   DGA republicadas (El Yeso 330149, Laguna Negra 330146, …) responden
   "Información no disponible": la DMC publica su *metadata*, no sus datos.
   Los defaults (El Colorado 2750 m, San José Guayacán, La Florida) son EMAs
   DMC con `aguaCaidaDelMinuto` minutario y ~20-60 min de retraso, dentro
   del dominio pero al poniente de las cabeceras (El Yeso/Laguna Negra
   quedan sin gauge). Mejora futura: scraping de HIDROlínea
   (snia.mop.gob.cl/sat) para los gauges DGA cordilleranos.
2. **Umbral Caine global por defecto**: validado en timing sobre 3 eventos
   chilenos con IMERG semihorario, pero SIN calibración de probabilidad
   operacional — `ALERT_LEVEL` es un índice, no una probabilidad, hasta que
   un `CALIBRATOR` ajustado sobre eventos reales entre en la config.
3. **Susceptibilidad uniforme** mientras el raster RF del Maipo no se
   regenere (R3-M11 del doc de auditoría).
4. FAR esperable alto en modo timing: toda tormenta fuerte del dominio
   cruza. El modo sombra existe para medir esto antes de confiar.
5. IMERG Early sub-captura convección corta bajo el radar de 0.1°/30 min;
   los tres eventos de validación sí lo resolvieron, pero es un límite físico
   del producto, no del motor.

## Fase B — QPF por ensemble (pySTEPS)

`qpf_ensemble.py` extrapola los últimos cuadros del feed rápido y produce N
miembros × K pasos futuros, que entran al motor por el mismo trait `Forcing`.
Es la pieza que daría **lead time positivo** (hoy el mejor caso es latencia
cero, porque toda la forzante es observada).

Necesita un venv propio — pysteps choca con el matplotlib del sistema:

```bash
python3 -m venv ~/.venvs/nowcast-qpf
~/.venvs/nowcast-qpf/bin/pip install "numpy<2" pysteps rasterio opencv-python-headless

~/.venvs/nowcast-qpf/bin/python monitor/qpf_ensemble.py \
    --steps ~/nowcast-monitor/feeds/goes/steps --out ~/nowcast-monitor/qpf \
    --members 12 --leadsteps 6
# --as-of YYYYmmddTHHMM corre como si fuera ese instante (replay honesto)
```

**Estado medido (evento Coelemu, 2026-08-08): la maquinaria funciona, el
insumo no alcanza.** El QPF tiene **+22% de skill sobre persistencia** a 3 h
(MAE 0.787 vs 1.007 mm/30min), con dispersión que crece con el horizonte, pero
subestima la intensidad (2.25 vs 3.68 mm observados en el primer paso) y decae
a cero demasiado rápido. El problema de fondo es otro: **GOES QPE solo NUNCA
cruza el umbral I-D** en ese evento — 0 pasos en alerta en todo el dominio,
pico global 0.214 < 0.315, y en la celda de Coelemu pico 0.035 contra 0.596 que
dio IMERG. Es el sesgo geoestacionario en lluvia orográfica templada, ya
documentado en las limitaciones, y un QPF lo hereda entero.

Consecuencia: Fase B sobre GOES crudo NO da lead time para frontales de
invierno en la zona centro-sur. El camino es corregir el sesgo de GOES contra
IMERG/pluviómetros donde se solapan (ajuste de QPE) ANTES de extrapolar.
