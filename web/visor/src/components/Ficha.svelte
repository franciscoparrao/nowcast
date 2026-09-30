<script>
  // Ficha de una comuna: picos de la ventana por modelo (del cruce comunal del
  // ciclo) y las localidades en vigilancia que caen en ella.
  let { comuna, localidades, onclose } = $props();

  const locs = $derived((localidades?.features ?? [])
    .map((f) => f.properties)
    .filter((p) => (p.comuna ?? "").toUpperCase() === (comuna.COMUNA ?? "").toUpperCase()));
  const f2 = (v) => (v === null || v === undefined || !Number.isFinite(v)) ? "—" : v.toFixed(2);
  const nivel = $derived((comuna.nivel ?? "").toString().toLowerCase());
</script>

<section class="ficha" aria-label="Ficha de la comuna {comuna.COMUNA}">
  <button class="cerrar" onclick={onclose} aria-label="Cerrar ficha">×</button>
  <div class="kicker">{comuna.REGION}</div>
  <h2>{comuna.COMUNA}</h2>
  {#if nivel}<span class="nivel {nivel}">nivel {nivel}</span>{/if}
  <dl>
    <dt>Población censal 2024</dt><dd>{comuna.poblacion?.toLocaleString("es-CL") ?? "—"}</dd>
    <dt>Terreno (susceptibilidad máx.)</dt><dd>{f2(comuna.peak_terreno)}</dd>
  </dl>
  <h3>Pico de la ventana (120 h)</h3>
  <table>
    <tbody>
      <tr><td><i class="sw" style="background:var(--m-gfs)"></i>GFS</td><td class="num">{f2(comuna.peak_gfs)}</td></tr>
      <tr><td><i class="sw" style="background:var(--m-ecmwf)"></i>ECMWF</td><td class="num">{f2(comuna.peak_ecmwf)}</td></tr>
      <tr><td><i class="sw" style="background:var(--m-icon)"></i>ICON</td><td class="num">{f2(comuna.peak_icon)}</td></tr>
      <tr class="peor"><td>Peor</td><td class="num">{f2(comuna.peak_worst)}</td></tr>
    </tbody>
  </table>
  <p class="nota">Máximo del peligro a 0.1° dentro del polígono: una comuna extensa con una esquina
    cordillerana puede marcar alto sin que el resto esté afectado.</p>
  {#if locs.length}
    <h3>Localidades en vigilancia</h3>
    <ul>
      {#each locs as l}
        <li><b>{l.label ?? l.name}</b>
          <span class="num">GFS {f2(l.peak_gfs)} · ECMWF {f2(l.peak_ecmwf)} · ICON {f2(l.peak_icon)}</span></li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .ficha { position: absolute; right: 16px; top: 16px; width: min(320px, calc(100% - 32px));
    max-height: calc(100% - 32px); overflow: auto; background: var(--paper-raised);
    border: 1px solid var(--rule-strong); box-shadow: var(--shadow); padding: 18px 20px 20px;
    z-index: 3; }
  .cerrar { position: absolute; right: 8px; top: 6px; border: 0; background: none; font-size: 22px;
    color: var(--ink-soft); cursor: pointer; }
  h2 { font: 700 22px/1.1 var(--f-titulo); margin: 4px 0 8px; }
  h3 { font: 700 11px/1.3 var(--f-etiqueta); letter-spacing: .12em; text-transform: uppercase;
    color: var(--ink-faint); margin: 18px 0 6px; }
  dl { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; margin: 12px 0 0; font-size: 14px; }
  dt { color: var(--ink-soft); } dd { margin: 0; font-family: var(--f-mono); font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  td { padding: 5px 0; border-bottom: 1px solid var(--rule); }
  tr.peor td { font-weight: 700; }
  ul { margin: 0; padding: 0; list-style: none; font-size: 14px; }
  li { padding: 6px 0; border-bottom: 1px solid var(--rule); display: grid; gap: 2px; }
  .nivel { display: inline-block; padding: 1px 8px; border-radius: 3px; font: 12px var(--f-mono); }
  .nivel.alto { background: var(--sev-high-bg); color: var(--sev-high-fg); }
  .nivel.medio { background: var(--sev-mid-bg); color: var(--sev-mid-fg); }
  .nivel.bajo { background: var(--sev-low-bg); color: var(--sev-low-fg); }
</style>
