# Tasa de cruces fuera de evento en los núcleos de tormenta: protocolo previo

Fecha: 2026-11-21. Se versiona ANTES de calcular. Responde al issue crítico 1 del
review blind b1 de EMS: el timing sub-diario se mostró en tres eventos elegidos
sabiendo dónde pasaron, sin medir cuántas veces cruza el mismo gatillo en días
sin evento. Si una regla cambia después de ver datos, se anota abajo con su
motivo y se reporta también el resultado con la regla original.

## Datos
- IMERG Final V07 semihorario en un punto, vía la API de Giovanni
  (`giovanni.py`; verificado idéntico a las series del paper en los tres
  núcleos el 2026-11-21).
- Núcleos (celda de mayor total del evento en su caja, definición del paper):
  Atacama −27.15 / −70.45; Cajón del Maipo −33.45 / −70.55; Villa Santa Lucía
  −43.45 / −72.15.
- Registro: años completos 1998–2024 (27 años). Sensibilidad: solo era GPM,
  2014–2024.

## Gatillo (el mismo del paper, sin cambios)
- Curva primaria a* = 5.5 mm/h, b = 0.39; secundaria Caine a = 14.82.
- Semihorario: ventanas de 0.5 a 24 h (como la serie C del experimento de un
  solo factor). Diario: la misma serie IMERG sumada por día UTC, ventanas de 1 a 3
  días (como la serie B).
- Un día UTC «cruza» si algún paso de ese día tiene E ≥ 1.

## Métricas, por núcleo y resolución
1. Días con cruce fuera de evento por año (media sobre años completos) y episodios
   por año (días con cruce consecutivos cuentan como uno).
2. Si el gatillo cruza en la ventana del evento (día documentado ± 2 días).
3. Rango del evento: percentil del E máximo de la ventana del evento entre los E
   máximos diarios de todo el registro.
4. Pasos sin dato: se cuentan; un día con más del 10 % de pasos sin dato se
   excluye del conteo.

## Lo que no puede decir
- Es una tasa en el punto del núcleo, no en el dominio: no mide falsas alarmas
  espaciales (eso lo mide la huella del §5.5).
- Un cruce fuera de evento no es necesariamente una falsa alarma: el inventario no
  registra todos los flujos. Los conteos son una cota superior de la tasa de
  falsas alarmas.
