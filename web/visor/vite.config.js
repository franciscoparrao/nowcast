// Visor de nowcast: sirve la app y, en /data/, el paquete web que deja
// monitor/web_package.py (~/nowcast-forecast/web, o NOWCAST_WEB). Solo escucha
// en localhost: el visor es PRIVADO por decisión (2026-09-29).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

const DATA = path.resolve(process.env.NOWCAST_WEB || path.join(os.homedir(), "nowcast-forecast/web"));
const FONTS = path.resolve(__dirname, "../../monitor/report/fonts");
const TIPOS = { ".json": "application/json", ".geojson": "application/geo+json", ".tif": "image/tiff" };

function datos() {
  const servir = (req, res, next) => {
    if (!req.url.startsWith("/data/")) return next();
    const rel = decodeURIComponent(req.url.slice(6).split("?")[0]);
    const f = path.resolve(DATA, rel);
    // nada fuera del directorio de datos (ni con ../ ni con rutas absolutas)
    if (f !== DATA && !f.startsWith(DATA + path.sep)) {
      res.statusCode = 403;
      return res.end();
    }
    fs.stat(f, (err, st) => {
      if (err || !st.isFile()) {
        res.statusCode = 404;
        return res.end();
      }
      res.setHeader("Content-Type", TIPOS[path.extname(f)] || "application/octet-stream");
      // Un ciclo puede regenerarse: el visor versiona cada archivo con
      // ?g=<generado> del manifest; lo que no trae versión se revalida siempre.
      res.setHeader("Cache-Control", /[?&]g=/.test(req.url) ? "max-age=3600" : "no-cache");
      fs.createReadStream(f).pipe(res);
    });
  };
  return {
    name: "nowcast-datos",
    // cuerpo con llaves a propósito: si el hook DEVUELVE una función, Vite la
    // registra como hook posterior y la invoca sin argumentos
    configureServer(s) { s.middlewares.use(servir); },
    configurePreviewServer(s) { s.middlewares.use(servir); },
  };
}

export default defineConfig({
  plugins: [svelte(), datos()],
  resolve: { alias: { "@fonts": FONTS } },
  server: { host: "127.0.0.1", port: 5188, fs: { allow: [".", FONTS] } },
  preview: { host: "127.0.0.1", port: 5188 },
});
