<script>
  import Mapa from "./components/Mapa.svelte";
  import Leyenda from "./components/Leyenda.svelte";
  import Ficha from "./components/Ficha.svelte";
  import {
    MODELOS, cargarCiclos, cargarManifest, cargarVector, campo, lineaDeTiempo, bandaParaHora, fijarVersion,
  } from "./lib/datos.js";
  import { escalaDe } from "./lib/colores.js";

  const fmtChile = new Intl.DateTimeFormat("es-CL", {
    timeZone: "America/Santiago", weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short",
  });
  const aChile = (iso) => fmtChile.format(new Date(iso.replace("Z", ":00Z")));

  let ciclos = $state.raw([]);
  let ciclo = $state(null);
  let manifest = $state.raw(null);
  let comunas = $state.raw(null);
  let localidades = $state.raw(null);
  let dom = $state("chile-centro-sur");
  let variable = $state("peligro");
  let capa = $state("peor");
  let porHora = $state(false);
  let iHora = $state(0);
  let reproduciendo = $state(false);
  let resultado = $state.raw(null);
  let hover = $state.raw(null);
  let seleccion = $state.raw(null);
  let error = $state(null);

  const dominio = $derived(manifest?.dominios?.[dom] ?? null);
  const horas = $derived(dominio ? lineaDeTiempo(dominio) : []);
  const hora = $derived(porHora && horas.length ? horas[Math.min(iHora, horas.length - 1)] : null);
  const escala = $derived(escalaDe(variable, capa));
  const nModelos = $derived(dominio ? Object.keys(dominio.modelos).length : 0);

  // carga inicial
  $effect(() => {
    cargarCiclos()
      .then((c) => { ciclos = c.ciclos; ciclo = c.ciclos[0]?.ciclo ?? null; })
      .catch((e) => { error = `No se pudo leer ciclos.json (${e.message}). ¿Corrió web_package.py?`; });
  });

  // cambio de ciclo: manifest y vectores
  $effect(() => {
    if (!ciclo) return;
    const c = ciclo;
    cargarManifest(c)
      .then((m) => { fijarVersion(m); return Promise.all([m, cargarVector(c, "comunas"), cargarVector(c, "localidades")]); })
      .then(([m, co, lo]) => {
        if (c !== ciclo) return;
        manifest = m; comunas = co; localidades = lo; seleccion = null;
        if (!m.dominios[dom]) dom = Object.keys(m.dominios)[0];
        iHora = 0;
      })
      .catch((e) => { error = `Ciclo ${c}: ${e.message}`; });
  });

  // capa pedida que no existe en este dominio → volver a "peor"
  $effect(() => {
    if (!dominio) return;
    const esModelo = MODELOS.some((m) => m.id === capa);
    if ((esModelo && !dominio.modelos[capa]) || (capa === "desacuerdo" && nModelos < 2)) capa = "peor";
  });

  // el campo a pintar: async, descartando respuestas de un pedido ya superado
  let pedido = 0;
  $effect(() => {
    if (!manifest || !dominio) return;
    const n = ++pedido;
    const args = [ciclo, dom, dominio, variable, capa, variable === "lluvia" ? null : hora, manifest.escala];
    campo(...args)
      .then((r) => { if (n === pedido) resultado = r; })
      .catch((e) => { if (n === pedido) error = e.message; });
  });

  // reproducción: avanza una hora cada 350 ms y se detiene al final
  $effect(() => {
    if (!reproduciendo) return;
    const id = setInterval(() => {
      if (iHora >= horas.length - 1) { reproduciendo = false; return; }
      iHora += 1;
    }, 350);
    return () => clearInterval(id);
  });

  function tema() {
    const root = document.documentElement;
    const oscuro = root.dataset.theme
      ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = oscuro ? "light" : "dark";
  }

  const fmt = (v) => (v === null || v === undefined || !Number.isFinite(v)) ? "—"
    : variable === "lluvia" ? `${v.toFixed(0)} mm` : v.toFixed(2);
  const etiquetaCapa = { gfs: "GFS", ecmwf: "ECMWF", icon: "ICON", peor: "Peor de los modelos", desacuerdo: "Desacuerdo" };
</script>

<div class="app">
  <header>
    <div class="marca">
      <div class="kicker">nowcast · visor privado</div>
      <h1>Pronóstico de remociones en masa</h1>
    </div>
    <div class="ciclo">
      <label>Ciclo
        <select bind:value={ciclo}>
          {#each ciclos as c}<option value={c.ciclo}>{c.ciclo}{c.modelos?.length < 3 ? " · sin " + ["gfs", "ecmwf", "icon"].filter((m) => !c.modelos?.includes(m)).map((m) => m.toUpperCase()).join(", ") : ""}</option>{/each}
        </select>
      </label>
      {#if horas.length}
        <span class="ventana">120 h: {aChile(horas[0])} → {aChile(horas[horas.length - 1])}</span>
      {/if}
    </div>
    <button class="tema" type="button" onclick={tema}>claro / oscuro</button>
  </header>

  {#if manifest}<p class="aviso">{manifest.aviso}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}

  <aside class="controles">
    <label class="campo">Dominio
      <select bind:value={dom}>
        {#each Object.entries(manifest?.dominios ?? {}) as [id, d]}<option value={id}>{d.nombre}</option>{/each}
      </select>
    </label>

    <fieldset>
      <legend>Variable</legend>
      <div class="seg">
        <button class:on={variable === "peligro"} onclick={() => (variable = "peligro")}>Peligro</button>
        <button class:on={variable === "lluvia"} onclick={() => (variable = "lluvia")}>Lluvia</button>
      </div>
    </fieldset>

    <fieldset>
      <legend>Modelo</legend>
      <div class="seg col">
        {#each MODELOS as m}
          <button class:on={capa === m.id} disabled={!dominio?.modelos[m.id]}
            title={dominio?.modelos[m.id] ? `ciclo ${dominio.modelos[m.id].ciclo}` : `${m.nombre} no está en este ciclo`}
            onclick={() => (capa = m.id)}>
            <i class="sw" style="background:var(--m-{m.id})"></i>{m.nombre}
            {#if !dominio?.modelos[m.id]}<small>ausente</small>{/if}
          </button>
        {/each}
        <button class:on={capa === "peor"} onclick={() => (capa = "peor")}>Peor de los modelos</button>
        <button class:on={capa === "desacuerdo"} disabled={nModelos < 2} onclick={() => (capa = "desacuerdo")}>
          Desacuerdo <small>máx − mín</small>
        </button>
      </div>
    </fieldset>

    <fieldset disabled={variable === "lluvia"}>
      <legend>Tiempo</legend>
      <div class="seg">
        <button class:on={!porHora} onclick={() => { porHora = false; reproduciendo = false; }}>Máximo 120 h</button>
        <button class:on={porHora} onclick={() => (porHora = true)}>Hora a hora</button>
      </div>
      {#if porHora && horas.length}
        <input type="range" min="0" max={horas.length - 1} bind:value={iHora} aria-label="Hora del pronóstico" />
        <div class="hora">
          <button class="play" onclick={() => { if (iHora >= horas.length - 1) iHora = 0; reproduciendo = !reproduciendo; }}
            aria-label={reproduciendo ? "Pausar" : "Reproducir"}>{reproduciendo ? "❚❚" : "▶"}</button>
          <span>{aChile(hora)}<br><small>T+{iHora + 1} h · {hora}</small></span>
        </div>
        {#if capa === "ecmwf" && dominio?.modelos.ecmwf}
          <p class="nota">ECMWF viene cada 3 h: se muestra su último paso ≤ esta hora
            ({dominio.modelos.ecmwf.pasos[bandaParaHora(dominio.modelos.ecmwf, hora)] ?? "aún sin paso"}).</p>
        {/if}
      {/if}
      {#if variable === "lluvia"}<p class="nota">La lluvia es el total de la ventana: el paquete no la trae por hora.</p>{/if}
    </fieldset>

    <Leyenda {escala} titulo={`${escala.etiqueta} · ${etiquetaCapa[capa]}`} />
    {#if variable === "peligro" && manifest?.variante_por_modelo}
      <p class="nota">Umbral por modelo: {Object.entries(manifest.variante_por_modelo)
        .map(([m, v]) => `${m.toUpperCase()} ${v === "caine-global" ? "Caine global" : "regional a=5.5"}`).join(" · ")}.
        ICON no diluye los picos al llegar a 0.1°; con el umbral regional se satura.</p>
    {/if}
  </aside>

  <main class="lienzo">
    <Mapa
      grilla={dominio?.grilla}
      valores={resultado?.valores}
      {escala}
      {comunas}
      {localidades}
      {seleccion}
      onhover={(h) => (hover = h)}
      onselect={(c) => (seleccion = c)}
    />
    {#if resultado && !resultado.valores}
      <p class="vacio">{capa === "desacuerdo" ? "El desacuerdo necesita al menos dos modelos en este ciclo."
        : `${etiquetaCapa[capa]} no tiene datos para esta hora todavía.`}</p>
    {/if}
    {#if seleccion}
      <Ficha comuna={seleccion} {localidades} onclose={() => (seleccion = null)} />
    {/if}
  </main>

  {#if hover && (hover.comuna || hover.celda !== null)}
    <div class="tip" style="left:{hover.x + 16}px; top:{hover.y + 14}px">
      {#if hover.comuna}<b>{hover.comuna.COMUNA}</b><span class="reg">{hover.comuna.REGION}</span>{/if}
      {#if hover.celda !== null && resultado}
        <table><tbody>
          {#each MODELOS.filter((m) => resultado.porModelo[m.id] !== undefined) as m}
            <tr class:act={capa === m.id}>
              <td><i class="sw" style="background:var(--m-{m.id})"></i>{m.nombre}</td>
              <td class="num">{fmt(resultado.porModelo[m.id]?.[hover.celda])}</td>
            </tr>
          {/each}
          {#if capa === "peor" || capa === "desacuerdo"}
            <tr class="act"><td>{etiquetaCapa[capa]}</td><td class="num">{fmt(resultado.valores?.[hover.celda])}</td></tr>
          {/if}
        </tbody></table>
        <small>{hover.lat.toFixed(2)}°, {hover.lon.toFixed(2)}° · {porHora && variable !== "lluvia" ? "esta hora" : "máximo de la ventana"}</small>
      {/if}
    </div>
  {/if}

  <footer>
    {#if manifest}{Object.values(manifest.atribucion).join(" · ")}{/if}
  </footer>
</div>
