<script>
  // Mapa en canvas propio (sin biblioteca ni tiles, como el visor de SurtGIS):
  // proyección equirectangular con corrección cos(lat), zoom con rueda, arrastre,
  // contornos comunales como mapa base y el ráster 0.1° encima.
  import { pintar } from "../lib/colores.js";

  let {
    grilla, valores, escala, comunas, localidades,
    seleccion = null, onhover = () => {}, onselect = () => {},
  } = $props();

  let lienzo = $state();
  let caja = $state();
  let ancho = $state(0), alto = $state(0);
  // vista: centro (lon, lat) y px por grado de longitud "proyectada"
  let vista = $state({ lon: -71.5, lat: -35, esc: 60 });
  let arrastre = null;
  let caminos = [];      // [{ path: Path2D, props }] en coords de mundo
  let tierra = null;     // unión de todas las comunas: máscara del ráster
  let dominioVisto = null;

  const K = Math.cos((35 * Math.PI) / 180); // Chile central: 1° lon ≈ 0.82° lat
  const mundo = (lon, lat) => [lon * K, -lat];
  const aPantalla = (lon, lat) => {
    const [x, y] = mundo(lon, lat), [cx, cy] = mundo(vista.lon, vista.lat);
    return [(x - cx) * vista.esc + ancho / 2, (y - cy) * vista.esc + alto / 2];
  };
  const aLonLat = (px, py) => {
    const [cx, cy] = mundo(vista.lon, vista.lat);
    const x = (px - ancho / 2) / vista.esc + cx, y = (py - alto / 2) / vista.esc + cy;
    return [x / K, -y];
  };

  // Path2D de cada comuna, construido UNA vez en coordenadas de mundo; el
  // zoom/pan se aplica con setTransform, así que no se reconstruye al moverse.
  $effect(() => {
    caminos = [];
    tierra = comunas?.features?.length ? new Path2D() : null;
    for (const f of comunas?.features ?? []) {
      const p = new Path2D();
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const poly of polys) {
        for (const anillo of poly) {
          anillo.forEach(([lon, lat], i) => {
            const [x, y] = mundo(lon, lat);
            i ? p.lineTo(x, y) : p.moveTo(x, y);
          });
          p.closePath();
        }
      }
      caminos.push({ path: p, props: f.properties });
      tierra.addPath(p);
    }
    dibujar();
  });

  // Encuadra el dominio al cambiarlo (no al cambiar de capa u hora).
  $effect(() => {
    if (!grilla || !ancho) return;
    const clave = grilla.bbox.join(",");
    if (clave === dominioVisto) return;
    dominioVisto = clave;
    const [x0, y0, x1, y1] = grilla.bbox;
    vista = {
      lon: (x0 + x1) / 2, lat: (y0 + y1) / 2,
      esc: 0.92 * Math.min(ancho / ((x1 - x0) * K), alto / (y1 - y0)),
    };
  });

  // Ráster a un canvas fuera de pantalla del tamaño de la grilla.
  let imagen = null;
  $effect(() => {
    if (!grilla || !valores) { imagen = null; dibujar(); return; }
    const c = document.createElement("canvas");
    c.width = grilla.ancho; c.height = grilla.alto;
    c.getContext("2d").putImageData(new ImageData(pintar(valores, escala), grilla.ancho, grilla.alto), 0, 0);
    imagen = c;
    dibujar();
  });

  $effect(() => { vista; seleccion; localidades; dibujar(); });

  function token(nombre) {
    return getComputedStyle(document.documentElement).getPropertyValue(nombre).trim();
  }

  function dibujar() {
    if (!lienzo || !ancho) return;
    const dpr = window.devicePixelRatio || 1;
    lienzo.width = ancho * dpr; lienzo.height = alto * dpr;
    const ctx = lienzo.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = token("--mar"); ctx.fillRect(0, 0, ancho, alto);

    const [cx, cy] = mundo(vista.lon, vista.lat);
    const enMundo = () => ctx.setTransform(dpr * vista.esc, 0, 0, dpr * vista.esc,
      dpr * (ancho / 2 - cx * vista.esc), dpr * (alto / 2 - cy * vista.esc));

    enMundo();
    ctx.fillStyle = token("--tierra");
    for (const c of caminos) ctx.fill(c.path, "evenodd");

    if (imagen && grilla) {
      const [x0, y0, x1, y1] = grilla.bbox;
      const [ax, ay] = mundo(x0, y1), [bx, by] = mundo(x1, y0);
      ctx.imageSmoothingEnabled = false; // celdas nítidas: es un producto de 0.1°
      // Recorte a la silueta comunal: la grilla del modelo cubre también mar y
      // Argentina, donde el índice no significa nada para este producto.
      ctx.save();
      if (tierra) ctx.clip(tierra, "nonzero");
      ctx.drawImage(imagen, ax, ay, bx - ax, by - ay);
      ctx.restore();
      ctx.lineWidth = 1 / vista.esc;
      ctx.strokeStyle = token("--rule-strong");
      ctx.setLineDash([4 / vista.esc, 3 / vista.esc]);
      ctx.strokeRect(ax, ay, bx - ax, by - ay);
      ctx.setLineDash([]);
    }

    ctx.lineWidth = 0.6 / vista.esc;
    ctx.strokeStyle = token("--borde");
    for (const c of caminos) ctx.stroke(c.path);
    if (seleccion) {
      const c = caminos.find((k) => k.props.CUT === seleccion.CUT);
      if (c) { ctx.lineWidth = 2.2 / vista.esc; ctx.strokeStyle = token("--ink"); ctx.stroke(c.path); }
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const f of localidades?.features ?? []) {
      const [x, y] = aPantalla(...f.geometry.coordinates);
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI);
      ctx.fillStyle = token("--paper"); ctx.fill();
      ctx.lineWidth = 1.6; ctx.strokeStyle = token("--ink"); ctx.stroke();
      if (vista.esc > 90) {
        ctx.font = `600 11px ${token("--f-etiqueta")}`;
        ctx.fillStyle = token("--ink");
        ctx.fillText(f.properties.label ?? f.properties.name, x + 7, y + 4);
      }
    }
  }

  function celdaEn(lon, lat) {
    if (!grilla || !valores) return null;
    const [x0, y0, x1, y1] = grilla.bbox;
    if (lon < x0 || lon >= x1 || lat <= y0 || lat > y1) return null;
    const col = Math.floor(((lon - x0) / (x1 - x0)) * grilla.ancho);
    const fila = Math.floor(((y1 - lat) / (y1 - y0)) * grilla.alto);
    return fila * grilla.ancho + col;
  }

  function comunaEn(px, py) {
    const ctx = lienzo.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const [cx, cy] = mundo(vista.lon, vista.lat);
    ctx.setTransform(dpr * vista.esc, 0, 0, dpr * vista.esc,
      dpr * (ancho / 2 - cx * vista.esc), dpr * (alto / 2 - cy * vista.esc));
    const c = caminos.find((k) => ctx.isPointInPath(k.path, px * dpr, py * dpr, "evenodd"));
    return c?.props ?? null;
  }

  function mover(ev) {
    const r = lienzo.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    if (arrastre) {
      const dx = (px - arrastre.px) / vista.esc, dy = (py - arrastre.py) / vista.esc;
      vista = { ...vista, lon: arrastre.lon - dx / K, lat: arrastre.lat + dy };
      return;
    }
    const [lon, lat] = aLonLat(px, py);
    onhover({ x: ev.clientX, y: ev.clientY, lon, lat, celda: celdaEn(lon, lat), comuna: comunaEn(px, py) });
  }

  function rueda(ev) {
    ev.preventDefault();
    const r = lienzo.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const [lon, lat] = aLonLat(px, py);
    const esc = Math.min(4000, Math.max(8, vista.esc * Math.exp(-ev.deltaY * 0.0015)));
    // zoom anclado al cursor: el punto bajo el mouse queda donde estaba
    const x = (px - ancho / 2) / esc, y = (py - alto / 2) / esc;
    vista = { lon: lon - x / K, lat: lat + y, esc };
  }

  function bajar(ev) {
    const r = lienzo.getBoundingClientRect();
    arrastre = { px: ev.clientX - r.left, py: ev.clientY - r.top, lon: vista.lon, lat: vista.lat, movio: false };
  }

  function soltar(ev) {
    const r = lienzo.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const clic = arrastre && Math.hypot(px - arrastre.px, py - arrastre.py) < 4;
    arrastre = null;
    if (clic) onselect(comunaEn(px, py));
  }

  $effect(() => {
    const ro = new ResizeObserver(([e]) => {
      ancho = e.contentRect.width; alto = e.contentRect.height; dibujar();
    });
    ro.observe(caja);
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const redib = () => dibujar();
    mq.addEventListener("change", redib);
    const mo = new MutationObserver(redib);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => { ro.disconnect(); mq.removeEventListener("change", redib); mo.disconnect(); };
  });
</script>

<div class="mapa" bind:this={caja}>
  <canvas
    bind:this={lienzo}
    onmousemove={mover}
    onmouseleave={() => { arrastre = null; onhover(null); }}
    onmousedown={bajar}
    onmouseup={soltar}
    onwheel={rueda}
    aria-label="Mapa del pronóstico: rueda para acercar, arrastrar para mover, clic en una comuna para ver su ficha"
  ></canvas>
</div>

<style>
  .mapa { position: absolute; inset: 0; }
  canvas { width: 100%; height: 100%; display: block; cursor: grab; }
  canvas:active { cursor: grabbing; }
</style>
