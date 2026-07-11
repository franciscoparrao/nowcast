# nowcast-monitor — monitor operacional de eventos (IMERG Early)

Monitor local de geohazards gatillados por lluvia: cada 30 minutos ingiere
IMERG **Early** NRT (30 min / 0.1°, ~4 h de latencia), corre el motor I-D
sobre una ventana rodante de 48 h y notifica cruces de umbral vía ntfy.
Pensado para un nodo liviano del home cluster (p. ej. `sentinel`).

## Qué ES y qué NO es (honestidad operacional)

- **ES** un monitor de *eventos en curso o muy recientes*: con ~4 h de
  latencia del feed, detecta que una tormenta cruzó el umbral I-D horas
  después de que empezó — útil para vigilancia, priorización y verificación.
- **NO es** alerta temprana: no hay lead time real sin QPF/telemetría de baja
  latencia (fase 2: telemetría DGA por noisy-OR; la maquinaria `multi` del
  motor ya lo soporta sin tocar el core).
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
       ├─ fetch_imerg_early.py   # ingesta incremental → steps/step_*.tif (KB c/u)
       │    · baja SOLO gránulos nuevos, recorta bbox, borra el HDF5
       │    · huecos viejos → raster de 0 mm MARCADO (gapmark_*) + conteo
       │    · rotación de la ventana + status.json
       ├─ dead-man switch        # feed sin datos > STALE_HOURS → NOTIFY urgent
       ├─ nowcast run --rain-rasters ... --format json   # ventana completa
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
scp monitor/{config.env,fetch_imerg_early.py,monitor.sh} sentinel:~/nowcast-monitor/monitor/
ssh sentinel "chmod +x ~/nowcast-monitor/monitor/monitor.sh && ls -la ~/nowcast-monitor/bin"  # verificar bytes

# 3. Configurar: editar ~/nowcast-monitor/monitor/config.env en el nodo
#    (BBOX, NTFY_TOPIC — crear un topic privado en ntfy.sh, NOWCAST_BIN).

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
# Genera una ventana sintética con una tormenta y corre un ciclo completo:
python3 monitor/selftest.py         # crea /tmp/nowcast-monitor-selftest
SKIP_FETCH=1 CONFIG=/tmp/nowcast-monitor-selftest/config.env monitor/monitor.sh
# Debe terminar en NOTIFY[urgent] ALERTA con el detalle del cruce.
```

## Limitaciones conocidas (léelas)

1. **Latencia ~4 h** del feed: monitor de eventos, no alerta temprana.
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
