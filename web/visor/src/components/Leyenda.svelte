<script>
  let { escala, titulo } = $props();
  const stops = $derived(Array.from({ length: 11 }, (_, i) => {
    const [r, g, b] = escala.color(i / 10);
    return `rgb(${r} ${g} ${b}) ${i * 10}%`;
  }).join(", "));
  const pct = (v) => ((v - escala.min) / (escala.max - escala.min)) * 100;
</script>

<figure class="leyenda">
  <figcaption>{titulo}</figcaption>
  <div class="barra" style="background:linear-gradient(90deg, {stops})">
    <span class="piso" style="width:{pct(escala.piso)}%" title="bajo {escala.piso}{escala.unidad}: transparente en el mapa"></span>
    {#if escala.marca !== null}<span class="marca" style="left:{pct(escala.marca)}%"></span>{/if}
  </div>
  <div class="ejes">
    <span>{escala.min}{escala.unidad}</span>
    {#if escala.marca !== null}<span style="left:{pct(escala.marca)}%" class="m">{escala.marca} alerta</span>{/if}
    <span>{escala.max}{escala.unidad}{escala.unidad === "mm" ? "+" : ""}</span>
  </div>
</figure>

<style>
  .leyenda { margin: 18px 0 0; }
  figcaption { font: 700 11px/1.3 var(--f-etiqueta); letter-spacing: .12em; text-transform: uppercase;
    color: var(--ink-faint); margin-bottom: 8px; }
  .barra { position: relative; height: 12px; border-radius: 2px; }
  .piso { position: absolute; left: 0; top: 0; bottom: 0;
    background: repeating-linear-gradient(135deg, var(--paper) 0 3px, transparent 3px 6px); }
  .marca { position: absolute; top: -4px; bottom: -4px; width: 2px; background: var(--ink); }
  .ejes { position: relative; display: flex; justify-content: space-between; margin-top: 6px;
    font: 12px var(--f-mono); color: var(--ink-soft); }
  .ejes .m { position: absolute; transform: translateX(-50%); color: var(--ink); }
</style>
