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
- **Sigue sin ser** alerta temprana en sentido estricto: el lead time solo se
  vuelve positivo con forzante *pronosticada* (fase B: ensemble pySTEPS sobre
  GOES QPE → `ensemble_hazard`; la maquinaria ya existe en el core).
- La fusión corre sobre la **unión** de ventanas: donde un feed no tiene dato
  (la cola rezagada de IMERG, el arranque en frío de DGA) entra como cero y
  no aporta al noisy-OR; los feeds rápidos cubren esas horas. Todo relleno
  queda contado y logueado (`prepare_fusion.py`). Un feed secundario caído
  degrada la fusión y se avisa; nunca detiene el ciclo.
- Con `SUSC_TIF` vacío corre con susceptibilidad uniforme 1.0: es un monitor
  de **timing** (¿cuándo cruzó la lluvia el umbral?), no de **dónde** es más
  peligroso. El raster RF regenerado se enchufa por config cuando exista.
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
# 0. En el nodo destino: python3 + pip install earthaccess xarray rasterio numpy
#    y ~/.netrc con las credenciales Earthdata (app "NASA GESDISC DATA ARCHIVE"
#    autorizada — la misma cuenta ya usada por scripts/extract_event_imerg.py).

# 1. Compilar el binario (en el orquestador; el nodo no necesita toolchain):
cargo build --release -p nowcast-cli

# 2. Copiar al nodo (ej: sentinel):
ssh sentinel "mkdir -p ~/nowcast-monitor/bin ~/nowcast-monitor/monitor"
scp target/release/nowcast sentinel:~/nowcast-monitor/bin/
scp monitor/{config.env,fetch_imerg_early.py,fetch_goes_qpe.py,fetch_dga.py,prepare_fusion.py,monitor.sh} \
    sentinel:~/nowcast-monitor/monitor/
ssh sentinel "chmod +x ~/nowcast-monitor/monitor/monitor.sh && ls -la ~/nowcast-monitor/bin"  # verificar bytes

# 3. Configurar: editar ~/nowcast-monitor/monitor/config.env en el nodo
#    (BBOX, NTFY_TOPIC — crear un topic privado en ntfy.sh, NOWCAST_BIN).
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
```

Cadencia: el timer corre a los minutos :12 y :42 (IMERG Early publica cada
media hora con ~4 h de retraso; el desfase evita pedir justo al filo).

## Prueba local sin red

```bash
# Modo base — un solo feed con tormenta al final:
python3 monitor/selftest.py         # crea /tmp/nowcast-monitor-selftest
SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh
# Debe terminar en NOTIFY[urgent] ALERTA con el detalle del cruce.

# Modo fusión (fase A) — IMERG rezagado 5 h con llovizna, ráfaga SOLO en GOES:
python3 monitor/selftest.py --fusion
SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh
# Debe ALERTAR vía fusión (la ráfaga vive en horas que IMERG aún no publica);
# con GOES_ENABLED=0 el mismo ciclo queda quiet — esa diferencia ES la
# latencia que la fase A recupera. Incluye fetch_dga.py --selftest (parser).
```

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
