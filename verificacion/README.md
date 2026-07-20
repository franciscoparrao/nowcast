# Registro de verificación

Lo que el sistema **pronosticó**, lo que se **midió** y lo que **pasó** en
terreno, evento por evento. Lo escribe `monitor/verif_log.py`.

## Por qué está versionado

El resto de las salidas del sistema vive en `~/nowcast-forecast/` y no se
versiona: se regenera corriendo los scripts otra vez. `data/` está
gitignoreado por lo mismo — es derivado.

Este archivo es la excepción porque **es irreemplazable**. Son observaciones
de un momento que no vuelve: estaciones que reportaron durante el evento,
notas de prensa que se editan o desaparecen, decisiones de autoridad con su
hora. Ningún script lo reconstruye. Si se pierde, se perdió.

Son kilobytes. El costo de versionarlo es nulo y el de no hacerlo es total.

## Por qué existe

El 19-jul-2026, durante el evento del Huasco, el sistema falló de una manera
que ninguna auditoría de código habría detectado: la columna de acuerdo entre
modelos apuntaba al revés —Freirina, descontada porque ECMWF pronosticaba
25–32 mm, es donde se activaron las quebradas Maitencillo y Alarcón; Alto del
Carmen, descontado por lo mismo, es donde más llovió (66 mm en El Corral)— y
nos enteramos **por la prensa, no por el sistema**.

La corrección de fondo es ponderar por *skill* histórico en vez de por
consenso. Eso es imposible sin un registro: no se puede ponderar por un skill
que nunca se anotó.

## Reglas

**Append-only.** La historia no se reescribe. Un registro equivocado se
corrige anotando otro que lo corrija, no borrando el anterior — si esto va a
sostener un argumento en un paper, tiene que poder auditarse hacia atrás.

**Procedencia obligatoria.** Toda observación y todo evento llevan `--fuente`
y/o `--url`. El comando falla si faltan: un dato sin origen no sirve para
calibrar y contamina el registro con algo que después nadie puede verificar.

**Tres tipos separados**, porque acertar la lluvia no es acertar el flujo:

| tipo | qué guarda |
|---|---|
| `pronostico` | lo que dijo cada modelo, por ciclo y punto, con su ventana temporal |
| `observacion` | lo que se midió, con procedencia |
| `evento` | lo que pasó (evacuación, activación de quebrada, alerta oficial) |

## Estado

Primer evento cargado: **Huasco, 16–20 jul 2026**. Se verificó la lluvia
—GFS acertó, ECMWF falló por un orden de magnitud— y **no** el footprint del
ABM, que es lo que el sistema realmente predice. Quedan anotadas la alarma
DMC del 16-jul y la Alerta Roja de SENAPRED del 18-jul, ambas anteriores a
que el sistema pusiera Freirina en su watchlist: **no hubo anticipación
propia**, y eso también es parte del registro.

```bash
python3 monitor/verif_log.py resumen
```
