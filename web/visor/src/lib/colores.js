// Rampas secuenciales (un tono, claro → oscuro) para cada magnitud. El peligro
// usa la familia óxido del registro; el desacuerdo, violeta (no se confunde con
// peligro); la lluvia, azul. Bajo `piso` la celda es transparente: el mapa base
// (límites comunales) sigue legible donde no pasa nada.

function hex(h) {
  return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
}

function rampa(stops) {
  const s = stops.map(([t, c]) => [t, hex(c)]);
  return (t) => {
    if (t <= s[0][0]) return s[0][1];
    for (let i = 1; i < s.length; i++) {
      if (t <= s[i][0]) {
        const r = (t - s[i - 1][0]) / (s[i][0] - s[i - 1][0]);
        return s[i - 1][1].map((a, k) => Math.round(a + (s[i][1][k] - a) * r));
      }
    }
    return s[s.length - 1][1];
  };
}

export const ESCALAS = {
  peligro: {
    min: 0, max: 1, piso: 0.05, unidad: "", marca: 0.5, etiqueta: "Índice de excedencia I-D",
    color: rampa([[0, "#f3ecc6"], [0.3, "#e9c77a"], [0.5, "#d98a3d"], [0.75, "#a8461c"], [1, "#5e1a0e"]]),
  },
  desacuerdo: {
    min: 0, max: 1, piso: 0.05, unidad: "", marca: 0.5, etiqueta: "Desacuerdo (máx − mín entre modelos)",
    color: rampa([[0, "#ece3f3"], [0.5, "#a476cf"], [1, "#3e1f5c"]]),
  },
  lluvia: {
    min: 0, max: 150, piso: 1, unidad: "mm", marca: null, etiqueta: "Lluvia líquida acumulada (120 h)",
    color: rampa([[0, "#dcebf5"], [0.4, "#6aa9d4"], [1, "#0b3b5a"]]),
  },
};

export function escalaDe(variable, capa) {
  if (variable === "lluvia") return ESCALAS.lluvia;
  return capa === "desacuerdo" ? ESCALAS.desacuerdo : ESCALAS.peligro;
}

/** RGBA por celda para un ImageData de ancho×alto. */
export function pintar(valores, escala, alfa = 215) {
  const out = new Uint8ClampedArray(valores.length * 4);
  const span = escala.max - escala.min;
  for (let i = 0; i < valores.length; i++) {
    const v = valores[i];
    if (!Number.isFinite(v) || v < escala.piso) continue; // transparente
    const [r, g, b] = escala.color(Math.min(1, Math.max(0, (v - escala.min) / span)));
    out.set([r, g, b, alfa], i * 4);
  }
  return out;
}
