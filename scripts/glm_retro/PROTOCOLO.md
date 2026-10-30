# GLM como aviso de eventos: protocolo declarado ANTES de ver los resultados

Fecha: 2026-10-30. Este archivo se versiona antes de correr el cruce; si una regla
cambia después de ver los datos, el cambio se anota abajo con su motivo y el
resultado con la regla original se reporta igual.

## Pregunta
De las celdas eléctricas que el GLM ve sobre Chile entre Atacama y La Araucanía,
¿cuántas terminan en un evento (quebrada, aluvión, crecida, anegamiento, remoción
o granizada con daño)? Y de los eventos, ¿cuántos tuvieron rayos antes y con
cuánto adelanto?

## Datos
- Rayos: GLM-L2-LCFA de GOES-19, 1 al 31 de octubre de 2026, binados a 0.1° × 10
  min (`descargar.py`). Los días con archivos faltantes quedan en `cobertura.csv`.
- Eventos: `data/catalogo_eventos_2026-10.csv`, armado desde prensa, Senapred y
  Red Geocientífica SIN mirar los rayos.
- Tierra chilena: comunas del Censo 2024. Las celdas fuera de Chile (Argentina,
  mar) no cuentan como aviso: nadie las vería en la cadena.

## Definiciones
1. **Celda en aviso**: celda 0.1° cuyo vecindario 3×3 (absorbe el error de
   geolocalización de pocos km) acumula ≥ N descargas en los últimos 60 min.
   N primario = 25. Se reporta también N = 10 y N = 50 como sensibilidad, sin
   elegir el "mejor".
2. **Episodio**: componente conexa de celdas en aviso en espacio (8 vecinos) y
   tiempo (bloques de 10 min contiguos o con hueco ≤ 30 min). Es la unidad de
   aviso: un episodio = un mensaje.
3. **Acierto**: un evento cae a ≤ 15 km de alguna celda del episodio y su hora
   está entre el inicio del episodio − 1 h y su fin + 3 h. Si el evento solo
   tiene fecha, basta que el episodio toque esa fecha en hora de Chile (UTC−3).
4. **Adelanto**: hora del evento − primer bloque en aviso a ≤ 15 km. Solo para
   eventos con hora exacta o aproximada.
5. **Falsa alarma**: episodio sin evento que cumpla (3).

## Métricas
- POD de eventos, separado por mecanismo: convectivo (tormenta, granizo) versus
  frontal o sin dato. Se espera POD bajo en frontales: no es un defecto del
  aviso, es su alcance.
- Fracción de episodios con evento (1 − FAR). Es una COTA INFERIOR de la
  precisión: el catálogo de prensa es incompleto y un episodio sin nota no
  prueba que no pasó nada. Se reporta también restringida a episodios que tocan
  comunas con población urbana.
- Adelanto: mediana y rango; n pequeño, se listan todos.

## Lo que este diseño no puede decir
- Con un mes y un catálogo de prensa no se calibra un umbral operacional.
- Un evento sin rayos puede ser frontal o puede ser hueco del GLM; se separan
  por mecanismo informado, no por inferencia.

## Anotaciones antes de correr el cruce (2026-10-30, sin haber visto resultados)
- Catálogo congelado: `catalogo_eventos_2026-10.csv` sha256 97d791ac8f846907…, 104 filas, 93
  con coordenadas; `catalogo_noeventos_2026-10.csv` sha256 530f761e05912024…. Está en
  `.gitignore` (`/data/*.csv`), por eso el hash va aquí.
- Las 11 filas sin coordenadas (agregadas de varias comunas) quedan fuera del
  cruce. Las coordenadas son en su mayoría del centro de la comuna: el radio de
  15 km no se agranda para compensar.
- `hora_confianza` trae, además de exacta/aprox, «tarde», «noche», «mañana» y
  «día». Solo exacta y aprox llevan hora; el resto se cruza por fecha (regla 3).
  Varias horas «aprox» son de publicación de la nota, no del evento: el adelanto
  que salga de ellas es una COTA SUPERIOR. Caso conocido: Morrillos figura a
  las 16:25 (hora de la actualización), pero la lluvia fue de 15:09 a 16:09.
- Mecanismo «convectivo» = el texto de mecanismo o tipo menciona convección,
  tormenta, granizo o eléctrica; todo lo demás va a «frontal/sin dato».
