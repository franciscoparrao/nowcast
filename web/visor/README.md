# Visor del pronóstico nowcast (privado)

Visor web estático de las salidas del pronóstico: Svelte 5 + Vite + `geotiff.js`,
el mismo stack del visor de SurtGIS, con mapa en canvas propio (sin biblioteca de
mapas ni tiles de terceros: el mapa base son los límites comunales del paquete).

**Privado por decisión (2026-09-29):** el servidor escucha solo en `127.0.0.1`.
Publicarlo marcaría comunas en "ALTO" con un sistema experimental en modo sombra.

## Correr

```bash
cd web/visor
npm install            # una vez
npm run dev            # → http://127.0.0.1:5188
```

Lee el paquete web que deja `monitor/web_package.py` al final de cada ciclo
(`~/nowcast-forecast/web`, o `NOWCAST_WEB=/otra/ruta npm run dev`). Sin paquete,
el visor lo dice: generarlo con
`python3 monitor/web_package.py` (venv del pronóstico).

## Qué muestra

- **Variable:** peligro (índice de excedencia I-D) o lluvia líquida acumulada.
- **Modelo:** GFS, ECMWF, ICON, el peor de los tres o el **desacuerdo**
  (máximo − mínimo entre modelos). Un modelo ausente en el ciclo aparece
  deshabilitado y rotulado, no oculto.
- **Tiempo:** máximo de las 120 h u hora a hora con reproducción. ECMWF viene
  cada 3 h: entre sus pasos se muestra el último disponible, y el visor lo dice.
- **Tooltip:** los tres modelos en la celda bajo el cursor, para compararlos.
- **Clic en una comuna:** ficha con sus picos por modelo, población y las
  localidades en vigilancia.

Umbral por modelo (`monitor/variantes.py`): regional a=5.5 para GFS y ECMWF,
Caine global para ICON, igual que el cruce comunal y la susceptibilidad.

## Contrato de datos

El visor no calcula el pronóstico: lee `manifest.json` y los COG del ciclo
(esquema 2, ver docstring de `monitor/web_package.py`). Cada archivo del ciclo se
pide con `?g=<generado>` del manifest, porque un ciclo puede regenerarse.
