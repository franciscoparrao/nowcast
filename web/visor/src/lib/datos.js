// Lectura del paquete web (contrato: monitor/web_package.py, esquema 1).
import { fromArrayBuffer } from "geotiff";

export const MODELOS = [
  { id: "gfs", nombre: "GFS" },
  { id: "ecmwf", nombre: "ECMWF" },
  { id: "icon", nombre: "ICON" },
];

const cacheTiff = new Map(); // url → Promise<GeoTIFFImage>
const cacheBanda = new Map(); // url#banda → Promise<Float32Array>

// Un paquete de ciclo se REGENERA (p.ej. al corregir la variante de ICON, 30-sep):
// "un ciclo es inmutable" no es cierto. El manifest y la lista van siempre
// frescos; cada archivo del ciclo lleva `?g=<generado>` del manifest, así que un
// paquete regenerado nunca se confunde con uno viejo en la caché del navegador.
let version = "";
export const fijarVersion = (manifest) => { version = manifest?.generado ?? ""; };
const conVersion = (url) => (version ? `${url}?g=${encodeURIComponent(version)}` : url);

async function json(url, fresco = false) {
  const r = await fetch(url, fresco ? { cache: "no-store" } : undefined);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

export const cargarCiclos = () => json("/data/ciclos.json", true);
export const cargarManifest = (ciclo) => json(`/data/${ciclo}/manifest.json`, true);
export const cargarVector = (ciclo, nombre) =>
  json(conVersion(`/data/${ciclo}/${nombre}.geojson`)).catch(() => null);

function imagen(urlBase) {
  const url = conVersion(urlBase);
  // Los archivos pesan decenas de KB: se bajan enteros una vez (sin byte-range)
  // y cada paso se decodifica bajo demanda.
  if (!cacheTiff.has(url)) {
    cacheTiff.set(url, fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
        return r.arrayBuffer();
      })
      .then(fromArrayBuffer)
      .then((t) => t.getImage()));
  }
  return cacheTiff.get(url);
}

/** Una banda decodificada a Float32Array (NaN = sin dato), según la escala del manifest. */
export function banda(url, indice, escala) {
  const k = `${conVersion(url)}#${indice}`;
  if (!cacheBanda.has(k)) {
    cacheBanda.set(k, imagen(url).then(async (img) => {
      const [crudo] = await img.readRasters({ samples: [indice] });
      const out = new Float32Array(crudo.length);
      if (escala?.dtype === "uint8") {
        for (let i = 0; i < crudo.length; i++) {
          out[i] = crudo[i] === escala.nodata ? NaN : crudo[i] / escala.divisor;
        }
      } else {
        out.set(crudo);
      }
      return out;
    }));
  }
  return cacheBanda.get(k);
}

/** Línea de tiempo común: las horas del modelo más denso del dominio. */
export function lineaDeTiempo(dominio) {
  let mejor = [];
  for (const m of Object.values(dominio.modelos)) {
    if (m.pasos.length > mejor.length) mejor = m.pasos;
  }
  return mejor;
}

/**
 * Índice de banda de un modelo para la hora `t`: la última banda con hora ≤ t
 * (ECMWF va cada 3 h: entre sus pasos se muestra el último disponible, y el
 * visor lo rotula). -1 si el modelo aún no tiene paso en esa hora.
 */
export function bandaParaHora(modelo, t) {
  let idx = -1;
  for (let i = 0; i < modelo.pasos.length; i++) {
    if (modelo.pasos[i] <= t) idx = i;
    else break;
  }
  return idx;
}

/**
 * Campo a mostrar. `variable`: "peligro" | "lluvia"; `capa`: gfs|ecmwf|icon|peor|
 * desacuerdo; `hora` null = máximo de la ventana (la lluvia es siempre el total
 * de la ventana: el paquete no trae lluvia por paso). Devuelve { valores,
 * porModelo }: porModelo alimenta la comparación de modelos en el tooltip.
 */
export async function campo(ciclo, dom, dominio, variable, capa, hora, escala) {
  const base = `/data/${ciclo}/${dom}`;
  const presentes = MODELOS.filter((m) => dominio.modelos[m.id]);
  const porModelo = {};
  await Promise.all(presentes.map(async (m) => {
    const info = dominio.modelos[m.id];
    if (variable === "lluvia") {
      porModelo[m.id] = info.lluvia ? await banda(`${base}/lluvia_${m.id}.tif`, 0, escala.lluvia) : null;
    } else if (hora === null) {
      porModelo[m.id] = await banda(`${base}/hazard_max_${m.id}.tif`, 0, escala.hazard);
    } else {
      const i = bandaParaHora(info, hora);
      porModelo[m.id] = i < 0 ? null : await banda(`${base}/hazard_${m.id}.tif`, i, escala.hazard);
    }
  }));
  if (capa in porModelo || MODELOS.some((m) => m.id === capa)) {
    return { valores: porModelo[capa] ?? null, porModelo };
  }
  const pilas = Object.values(porModelo).filter(Boolean);
  if (!pilas.length || (capa === "desacuerdo" && pilas.length < 2)) {
    return { valores: null, porModelo };
  }
  const valores = new Float32Array(pilas[0].length).fill(NaN);
  for (let i = 0; i < valores.length; i++) {
    let mx = -Infinity, mn = Infinity, n = 0;
    for (const p of pilas) {
      const v = p[i];
      if (Number.isFinite(v)) { mx = Math.max(mx, v); mn = Math.min(mn, v); n++; }
    }
    if (capa === "peor" && n) valores[i] = mx;
    if (capa === "desacuerdo" && n >= 2) valores[i] = mx - mn;
  }
  return { valores, porModelo };
}
