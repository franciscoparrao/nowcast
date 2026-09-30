"""Variante de umbral I-D por modelo: UNA sola fuente para todos los productos.

La regla (2026-07-16, razonada en el docstring de susc_overlay.py):
  · GFS y ECMWF llegan a 0.1° por bilineal, que diluye los picos de intensidad →
    `regional-a5.5` (intercepto calibrado para forzante gruesa).
  · ICON llega por vecino más cercano desde ~13 km nativo, sin diluir → con el
    mismo intercepto permisivo se satura (272/275 comunas en ALTO) → `caine-global`.
Ninguna variante está "más validada" que la otra para ICON: es consistencia de
resolución, no una recalibración; queda pendiente contrastarla con inventario.

Por qué este módulo (2026-09-30): susc_overlay y comuna_impact aplicaban la regla,
cada uno con su copia; el índice, el registro (tabla por dominio y resumen) y el
paquete web usaban `regional-a5.5` también para ICON. Resultado visible en el
visor: en Linares una celda de ICON marcaba 0.83 mientras el pico comunal de
ICON era 0.17 (Caine). Todos importan de aquí.
"""

VARIANTE_POR_MODELO = {
    "gfs": "regional-a5.5",
    "ecmwf": "regional-a5.5",
    "icon": "caine-global",
}

# Nombre del ráster de máximo de la ventana de cada variante (convención de los
# fetchers: la variante caine-global es el `hazard_max.tif` "base").
_ARCHIVO_MAX = {"regional-a5.5": "hazard_max_regional-a5.5.tif", "caine-global": "hazard_max.tif"}


def variante(modelo):
    return VARIANTE_POR_MODELO[modelo]


def archivo_max(modelo):
    """`hazard_max_*.tif` que corresponde al modelo."""
    return _ARCHIVO_MAX[VARIANTE_POR_MODELO[modelo]]


def carpeta_pasos(modelo):
    """Carpeta de pasos de peligro del modelo (`hazard_<variante>/`)."""
    return f"hazard_{VARIANTE_POR_MODELO[modelo]}"


def pico(meta, modelo):
    """peak_prob de la variante del modelo en su json (forecast/ecmwf/icon)."""
    try:
        return float(meta["variants"][VARIANTE_POR_MODELO[modelo]]["peak_prob"])
    except (TypeError, KeyError, ValueError):
        return None


def rotulo(modelo):
    """Texto corto del umbral, para tablas y leyendas."""
    return "Caine" if VARIANTE_POR_MODELO[modelo] == "caine-global" else "a=5.5"
