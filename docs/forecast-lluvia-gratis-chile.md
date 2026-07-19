# Pronostico de lluvia gratuito de mayor resolucion para Chile

Fecha: 2026-07-15

## Contexto

El sistema experimental de nowcasting de geopeligros usa actualmente GFS 0.25 deg
via NOMADS y ECMWF IFS Open Data 0.25 deg. Ambos son fuentes gratuitas y
globales, pero su grilla de alrededor de 25 km no resuelve bien efectos
orograficos de pocos kilometros.

El problema operativo que disparo esta revision fue la discrepancia con los
montos DMC para el evento frontal/rio atmosferico de mediados de julio de 2026
en Atacama. DMC dio valores altos por localidad en el valle del Huasco
(Vallenar 93.2 mm, Freirina 112.6 mm, Huasco 100.7 mm en 24-48 h), mientras
GFS/ECMWF 0.25 deg dieron valores muy inferiores en esos puntos. La hipotesis
principal es que el valle del rio Huasco canaliza/amplifica precipitacion a una
escala que una grilla global de 25 km no representa. El caso opuesto tambien
aparecio: en Caldera/Copiapo, el maximo de la celda gruesa quedo a decenas de
kilometros del pueblo real.

## Resultado ejecutivo

La mejora gratuita incremental mas clara es agregar DWD ICON global. ICON es
global, abierto, corre cuatro veces al dia y tiene malla nativa icosaedrica de
13 km. No reemplaza un modelo regional de 3 km, pero reduce a la mitad el
tamano efectivo de celda frente a GFS/ECMWF Open Data 0.25 deg y es viable en
el cluster domestico porque solo se descarga y reproyecta el producto, no se
corre el modelo.

ECMWF no parece estar siendo degradado por error en el script actual:
`monitor/fetch_ecmwf.py` consume `ifs/0p25/oper`, y la documentacion Open Data
de ECMWF indica que los productos IFS/AIFS publicos estan a 0.25 deg salvo
excepcion. Las versiones de mayor resolucion existen en el catalogo/servicio de
diseminacion, pero requieren acuerdo de servicio.

## Alternativas evaluadas

| Alternativa | Sirve para Chile | Resolucion / horizonte | Costo/licencia | Esfuerzo | Prioridad |
|---|---:|---|---|---|---|
| DWD ICON global | Si | 13 km nativo; 00/12 UTC hasta 180 h, 06/18 UTC hasta 120 h | Open Data DWD, CC BY 4.0 | 1-2 dias para ingesta inicial | P0 |
| ECMWF IFS Open Data | Si | 0.25 deg en producto abierto; pasos 3 h | CC BY 4.0 + terminos ECMWF | Ya implementado | P0, revisar sesgo |
| GFS | Si | 0.25 deg, horario | NOAA/NCEP publico | Ya implementado | Base |
| UK Met Office global | Si, pero con limite | Modelo global de alta resolucion, pero DataHub gratuito limitado | Plan gratis con cupo bajo; no ideal para operacion | 2-4 dias + control cuotas | P2 |
| JMA GSM | Global | Hay producto global, pero el acceso operacional abierto a GRIB no quedo tan limpio como DWD/ECMWF | Verificar endpoint/licencia antes de invertir | 2-5 dias de investigacion | P3 |
| ARPEGE global | Tecnicamente global | Malla estirada: mayor resolucion cerca de Francia/Europa, peor lejos | Open data Météo-France, pero poco atractivo para Chile | Bajo retorno | Baja |
| AROME, ICON-EU, ICON-D2, JMA MSM/LFM, HRRR | No | Alta resolucion regional fuera de Chile | No aplica a Sudamerica | Descartar | Descartar |

## Downscaling estadistico e IA

CorrDiff/Earth-2 y trabajos recientes de difusion/super-resolucion son
prometedores, pero no son una solucion gratuita plug-and-play para Atacama.
Los modelos publicos o demostrados suelen estar entrenados para regiones
especificas, por ejemplo Taiwan, CONUS o Alaska, y requieren un "truth" local
de alta resolucion. Para Chile eso implica entrenar o ajustar con estaciones,
CR2MET historico, IMERG/GOES y/o un corpus WRF propio.

Recomendacion incremental antes de entrenar IA generativa: MOS/quantile mapping
por localidad/cuenca usando predictores de GFS+ECMWF+ICON:

- precipitacion nearest-cell, maximo 3x3 y maximo 5x5;
- viento 850/700 hPa, IVT si se agrega;
- elevacion, pendiente, orientacion del valle y distancia a costa;
- correccion por localidad/cuenca contra historico de estaciones y CR2MET.

Esto es mucho mas rapido de implementar y ataca directamente el error
observado: el punto real no debe depender solo de la celda mas cercana.

## WRF/ICON regional propio

WRF-ARW anidado es la via correcta para resolver orografia a 3-4 km. Para un
dominio Atacama-Coquimbo de unos 300x400 km, una configuracion razonable es
padre 9 km + nido 3 km, forzado por GFS o ECMWF Open Data. En un nodo domestico
de 16 cores y 64 GB RAM, un forecast de 5 dias probablemente toma varias horas
o media jornada; dos corridas diarias son posibles pero apretadas. Con 32-64
cores se vuelve mas razonable. Un nido de 1 km solo conviene para una ventana
chica, por ejemplo valle del Huasco.

No conviene partir por WRF para el evento inmediato si el objetivo es una
mejora incremental y gratuita. Primero ICON global + correccion espacial
simple; luego un piloto WRF 9/3 km.

## DMC, U. Chile, CR2 y salidas chilenas

No hay confirmada una salida abierta tipo GRIB/NetCDF/API de modelo regional
DMC/U. Chile/CR2 comparable a GFS/ICON para descarga operacional. La API DMC
de estaciones y CR2MET son utiles, pero el primero es observacion y el segundo
es historico/reanalisis, no pronostico. Por ahora los montos DMC por localidad
deben tratarse como referencia externa/autoritativa para reconciliacion, no
como feed directo de modelo.

## Plan operativo

1. Agregar ICON global DWD como tercera fuente deterministica y verificar que
   ECMWF Open Data no tenga una resolucion gratuita mejor que `0p25`.
2. Comparar GFS/ECMWF/ICON en localidades criticas usando nearest-cell,
   maximo 3x3 y maximo 5x5, no solo el pixel mas cercano.
3. Implementar correccion estadistica simple por localidad/cuenca.
4. Hacer piloto WRF 9/3 km para Atacama-Coquimbo si el evento o la temporada lo
   justifican.
5. Mas adelante, entrenar un downscaler tipo CorrDiff/WxFlow con WRF +
   estaciones + CR2MET/IMERG como verdad local aproximada.

## Estado del punto 1 en el repo

- `monitor/fetch_ecmwf.py` ya usa el producto ECMWF Open Data `ifs/0p25/oper`.
  Esto coincide con la resolucion documentada para Open Data; no hay evidencia
  de una degradacion accidental dentro del script.
- Se agrega `monitor/fetch_icon.py` como ingesta inicial de DWD ICON global:
  descarga campos de lluvia de `opendata.dwd.de`, lee la malla icosaedrica,
  reproyecta por vecino mas cercano a la grilla interna de 0.1 deg y escribe
  resultados bajo `<workdir>/latest/<dominio>/icon/`.
- La primera version usa lluvia liquida directa de `rain_gsp + rain_con`
  cuando esta disponible. Si esos campos fallan, el fallback planificado es
  `tot_prec` con particion por isoterma/orografia, pero no conviene activar ese
  fallback sin validacion de metadatos GRIB.

## Fuentes clave

- DWD, Globalmodell ICON: 13 km, 120 capas, 2,949,120 celdas, ciclos 00/12
  hasta 180 h y 06/18 hasta 120 h.
  https://www.dwd.de/DE/forschung/wettervorhersage/num_modellierung/01_num_vorhersagemodelle/icon_beschreibung.html
- DWD Open Data ICON GRIB:
  https://opendata.dwd.de/weather/nwp/icon/grib/
- DWD condiciones legales: contenidos/geodatos bajo CC BY 4.0 con atribucion.
  https://www.dwd.de/DE/service/rechtliche_hinweise/rechtliche_hinweise_node.html
- ECMWF Open Data: subconjunto IFS/AIFS gratuito, CC BY 4.0, productos a
  0.25 deg salvo excepcion, mayor resolucion via servicio/acuerdo.
  https://www.ecmwf.int/en/forecasts/datasets/open-data
- Earth2Studio/CorrDiff como referencia de downscaling regional, no solucion
  andina lista para produccion:
  https://github.com/NVIDIA/earth2studio

