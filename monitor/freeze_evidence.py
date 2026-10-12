#!/usr/bin/env python3
"""Congela la evidencia de una alerta I-D del monitor (2026-10-12).

POR QUÉ. La primera alerta de monitor@rm (10-oct, 22:30Z, solo GOES) quedó
SIN verificar: cuando se fue a mirar, la ventana de ~12 h de la API DMC ya
empezaba 2 h después del cruce, los pasos de la ventana rodante ya habían
rotado y los rásters de peligro se habían reescrito con otros índices. La
evidencia de un cruce se pierde en horas si nadie la guarda EN EL MOMENTO.
Este script la deja congelada en el ciclo de la alerta, para que un informe
trazable (docs/informes/) pueda citarla después con su hash.

QUÉ GUARDA. En `EVID_DIR/<EP_FIRST>/` (una carpeta por RACHA, nombrada por
la clave de tiempo de su primer paso en alerta; mientras la racha persista
cada ciclo AÑADE lo nuevo y NUNCA sobreescribe lo ya congelado):
  feeds/<feed>/step_<clave>.tif   pasos de cada feed de fusion.json en la
                                  ventana [EP_FIRST − PRE_H, EP_LAST + POST_H]
                                  (+ gapmark_<clave> si el feed marcó hueco;
                                  los zerofill del primario se registran en
                                  el manifiesto, no se copian)
  hazard/hazard_<clave>.tif       peligro por paso, renombrado por CLAVE DE
                                  TIEMPO (el índice hazard_%04d cambia con
                                  cada rotación de la ventana)
  glm/steps/step_<clave>.tif, glm/counts.json, glm/status_<ciclo>.json
  ema/<code>.jsonl                registros CRUDOS de archive_ema en la ventana
  ema/resumen.json                lluvia horaria por estación (mm)
  runs/run_<ciclo>.json, runs/fusion_<ciclo>.json
  config.env                      snapshot de la config (credenciales tachadas)
  ciclos.jsonl                    una línea por ciclo: racha, veredicto de
                                  corroboración, título, entrega, GLM
  manifest.json                   ventana, commit del monitor, sha256 de cada
                                  archivo congelado
Guarda TAMBIÉN las alertas "NO corroboradas": justamente son las que hay que
poder revisar (la falsa alarma del 10-oct es el caso de uso).

Salida (stdout, una línea): `<dir> <archivos_nuevos> <archivos_total>` o
`skip <motivo>`. Nunca falla el ciclo: cualquier error interno se reporta en
stderr y sale 0 con `skip`. `--selftest` sin red ni motor.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KEY_FMT = "%Y%m%dT%H%M"
STEP_MIN = 30
SECRET_RE = re.compile(r"^\s*(\w*(TOKEN|USUARIO|USER|PASS|SECRET|KEY)\w*)\s*=", re.IGNORECASE)


def parse_key(k):
    try:
        return datetime.strptime(k, KEY_FMT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_key(t):
    return t.strftime(KEY_FMT)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def ventana(ep_first, ep_last, pre_h, post_h):
    """(t_ini, t_fin) aware UTC; None si las claves no son de tiempo."""
    a, b = parse_key(ep_first), parse_key(ep_last)
    if a is None or b is None:
        return None
    return a - timedelta(hours=pre_h), b + timedelta(hours=post_h)


def claves(fusion, n):
    """Clave de tiempo del paso i = span[0] + 30 min × i (como monitor.sh)."""
    t0 = parse_key((fusion.get("span") or [""])[0])
    if t0 is None:
        return [f"step{i:04d}" for i in range(n)]
    return [fmt_key(t0 + timedelta(minutes=STEP_MIN * i)) for i in range(n)]


def en_ventana(key, win, i=None, idx_win=None):
    t = parse_key(key)
    if t is not None and win is not None:
        return win[0] <= t <= win[1]
    if idx_win is not None and i is not None:
        return idx_win[0] <= i <= idx_win[1]
    return False


class Congelador:
    def __init__(self, dest, manifest):
        self.dest = dest
        self.files = manifest.setdefault("files", {})
        self.nuevos = 0

    def copiar(self, src, rel, note=None):
        """Copia src a dest/rel solo si rel no existe aún (nunca pisa)."""
        if rel in self.files:
            return False
        dst = os.path.join(self.dest, rel)
        if os.path.exists(dst):
            self.files[rel] = {"sha256": sha256(dst)}
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        entry = {"sha256": sha256(dst)}
        if note:
            entry["src"] = note
        self.files[rel] = entry
        self.nuevos += 1
        return True

    def escribir(self, rel, data, sobreescribir=False):
        """Escribe JSON/texto; por defecto no pisa (ciclos.jsonl y counts se manejan aparte)."""
        dst = os.path.join(self.dest, rel)
        if os.path.exists(dst) and not sobreescribir:
            return False
        os.makedirs(os.path.dirname(dst) or self.dest, exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "w") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, dst)
        self.files[rel] = {"sha256": sha256(dst)}
        self.nuevos += 1
        return True


def congelar_feeds(c, fusion, win, idx_win, manifest):
    resumen = {}
    for name, info in (fusion.get("feeds") or {}).items():
        lst = (info.get("list") or "").split(",")
        keys = claves(fusion, len(lst))
        copiados, zerofill, gapmarks = 0, [], []
        steps_dir = None
        for i, p in enumerate(lst):
            if not p or not en_ventana(keys[i], win, i, idx_win):
                continue
            base = os.path.basename(p)
            if base.startswith("step_"):
                steps_dir = steps_dir or os.path.dirname(p)
                if os.path.exists(p) and c.copiar(p, f"feeds/{name}/step_{keys[i]}.tif", base):
                    copiados += 1
            else:
                zerofill.append(keys[i])
        if steps_dir:
            for g in glob.glob(os.path.join(steps_dir, "gapmark_*")):
                k = os.path.basename(g)[len("gapmark_"):]
                if en_ventana(k, win):
                    c.copiar(g, f"feeds/{name}/gapmark_{k}")
                    gapmarks.append(k)
        prev = manifest.get("feeds", {}).get(name, {})
        resumen[name] = {
            "pasos_congelados": prev.get("pasos_congelados", 0) + copiados,
            "zerofill_en_ventana": sorted(set(prev.get("zerofill_en_ventana", [])) | set(zerofill)),
            "gapmarks_en_ventana": sorted(set(prev.get("gapmarks_en_ventana", [])) | set(gapmarks)),
            "newest_real": info.get("newest_real"),
        }
    manifest["feeds"] = resumen


def congelar_hazard(c, hazard_dir, fusion, run, win, idx_win):
    if not hazard_dir or not os.path.isdir(hazard_dir):
        return 0
    n = len(run.get("steps") or [])
    keys = claves(fusion, n)
    copiados = 0
    for i in range(n):
        if not en_ventana(keys[i], win, i, idx_win):
            continue
        p = os.path.join(hazard_dir, f"hazard_{i:04d}.tif")
        if os.path.exists(p) and c.copiar(p, f"hazard/hazard_{keys[i]}.tif", os.path.basename(p)):
            copiados += 1
    return copiados


def congelar_glm(c, glm_dir, win, ciclo):
    if not glm_dir or not os.path.isdir(glm_dir) or win is None:
        return None
    for p in glob.glob(os.path.join(glm_dir, "steps", "step_*.tif")):
        k = os.path.basename(p)[5:-4]
        if en_ventana(k, win):
            c.copiar(p, f"glm/steps/step_{k}.tif")
    counts_p = os.path.join(glm_dir, "counts.json")
    merged = {}
    dst = os.path.join(c.dest, "glm", "counts.json")
    if os.path.exists(dst):
        try:
            merged = json.load(open(dst))
        except Exception:
            merged = {}
    try:
        counts = json.load(open(counts_p)) if os.path.exists(counts_p) else {}
    except Exception:
        counts = {}
    antes = len(merged)
    for k, v in counts.items():
        if en_ventana(k, win) and k not in merged:
            merged[k] = v
    if len(merged) != antes or not os.path.exists(dst):
        c.escribir("glm/counts.json", dict(sorted(merged.items())), sobreescribir=True)
    st = os.path.join(glm_dir, "status.json")
    recent = None
    if os.path.exists(st):
        c.copiar(st, f"glm/status_{ciclo}.json")
        try:
            recent = json.load(open(st)).get("recent")
        except Exception:
            recent = None
    return recent


def mm_de(texto):
    try:
        return float(str(texto).split()[0])
    except Exception:
        return None


def congelar_ema(c, ema_dir, stations, win):
    """Filtra el archivo crudo de archive_ema (jsonl por estación y mes) a la
    ventana; une con lo ya congelado por `momento` (idempotente)."""
    if not ema_dir or not os.path.isdir(ema_dir) or win is None or not stations:
        return {}
    codes = [s.split(":")[0].strip() for s in stations.split(",") if s.strip()]
    nombres = {s.split(":")[0].strip(): (s.split(":") + [""] * 4)[3].strip() for s in stations.split(",") if s.strip()}
    meses = set()
    t = win[0].replace(day=1, hour=0, minute=0)
    while t <= win[1]:
        meses.add(t.strftime("%Y-%m"))
        t = (t + timedelta(days=32)).replace(day=1)
    resumen = {}
    for code in codes:
        dst = os.path.join(c.dest, "ema", f"{code}.jsonl")
        regs = {}
        if os.path.exists(dst):
            for line in open(dst):
                try:
                    r = json.loads(line)
                    regs[r.get("momento")] = r
                except Exception:
                    pass
        antes = len(regs)
        for mes in sorted(meses):
            p = os.path.join(ema_dir, f"{code}-{mes}.jsonl")
            if not os.path.exists(p):
                continue
            for line in open(p):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                m = r.get("momento")
                try:
                    tm = datetime.strptime(m[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if win[0] <= tm <= win[1] and m not in regs:
                    regs[m] = r
        if not regs:
            resumen[code] = {"nombre": nombres.get(code, ""), "registros": 0, "horario_mm": {}}
            continue
        if len(regs) != antes:
            texto = "".join(json.dumps(regs[k], ensure_ascii=False) + "\n" for k in sorted(regs))
            c.escribir(f"ema/{code}.jsonl", texto, sobreescribir=True)
        horario = {}
        for m, r in regs.items():
            v = mm_de(r.get("aguaCaidaDelMinuto"))
            if v is None:
                continue
            h = m[:13].replace(" ", "T") + "Z"
            horario[h] = round(horario.get(h, 0.0) + v, 2)
        resumen[code] = {"nombre": nombres.get(code, ""), "registros": len(regs),
                         "desde": min(regs), "hasta": max(regs),
                         "total_mm": round(sum(horario.values()), 2),
                         "horario_mm": dict(sorted(horario.items()))}
    c.escribir("ema/resumen.json", resumen, sobreescribir=True)
    return resumen


def snapshot_config(c, config_path):
    if not config_path or not os.path.exists(config_path):
        return
    lines = []
    for line in open(config_path, errors="replace"):
        if SECRET_RE.match(line):
            lines.append(SECRET_RE.match(line).group(1) + '="<tachado>"\n')
        else:
            lines.append(line)
    c.escribir("config.env", "".join(lines))


def git_commit(repo):
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def congelar(a):
    run = json.load(open(a.run_json))
    fusion = json.load(open(a.fusion_json))
    win = ventana(a.ep_first, a.ep_last, a.pre_h, a.post_h)
    idx_win = None
    if win is None:   # claves por índice (fusion.json sin span)
        try:
            i0, i1 = int(a.ep_first[-4:]), int(a.ep_last[-4:])
            idx_win = (i0 - int(a.pre_h * 60 / STEP_MIN), i1 + int(a.post_h * 60 / STEP_MIN))
        except Exception:
            return f"skip claves ilegibles ({a.ep_first} {a.ep_last})"
    dest = os.path.join(a.evid_dir, a.ep_first)
    free_mb = shutil.disk_usage(os.path.dirname(a.evid_dir.rstrip("/")) or "/").free / 2**20
    if free_mb < a.min_free_mb:
        return f"skip disco con {free_mb:.0f} MB libres (< {a.min_free_mb})"
    os.makedirs(dest, exist_ok=True)
    ciclo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")   # con segundos: dos ciclos en el mismo minuto no colisionan
    mp = os.path.join(dest, "manifest.json")
    manifest = {}
    if os.path.exists(mp):
        try:
            manifest = json.load(open(mp))
        except Exception:
            manifest = {}
    c = Congelador(dest, manifest)
    manifest.setdefault("racha", {"ep_first": a.ep_first})
    manifest["racha"]["ep_last"] = a.ep_last
    manifest.setdefault("creado_utc", ciclo)
    manifest["actualizado_utc"] = ciclo
    manifest["ventana"] = {"desde": fmt_key(win[0]), "hasta": fmt_key(win[1]),
                           "pre_h": a.pre_h, "post_h": a.post_h} if win else {"idx": idx_win}
    manifest.setdefault("monitor_commit", git_commit(a.repo))
    manifest.setdefault("work_dir", a.work_dir)

    congelar_feeds(c, fusion, win, idx_win, manifest)
    manifest["hazard_pasos"] = manifest.get("hazard_pasos", 0) + congelar_hazard(c, a.hazard_dir, fusion, run, win, idx_win)
    glm_recent = congelar_glm(c, a.glm_dir, win, ciclo)
    ema = congelar_ema(c, a.ema_dir, a.stations, win)
    manifest["ema"] = {k: {kk: vv for kk, vv in v.items() if kk != "horario_mm"} for k, v in ema.items()}
    c.copiar(a.run_json, f"runs/run_{ciclo}.json")
    c.copiar(a.fusion_json, f"runs/fusion_{ciclo}.json")
    snapshot_config(c, a.config)

    alert_steps = [s for s in run.get("steps", []) if s.get("alert")]
    linea = {
        "ciclo_utc": ciclo, "ep_first": a.ep_first, "ep_last": a.ep_last,
        "n_alert": len(alert_steps),
        "pico": max((s.get("max_probability", 0) for s in run.get("steps", [])), default=None),
        "alert_level": run.get("alert_level"),
        "corroborada": a.corrob, "sostienen": a.corrob_feeds, "detalle": a.corrob_det,
        "titulo": a.titulo, "entregado": a.delivered, "glm_recent": glm_recent,
        "ema_total_mm": {k: v.get("total_mm") for k, v in ema.items()},
        "run_json": f"runs/run_{ciclo}.json",
    }
    with open(os.path.join(dest, "ciclos.jsonl"), "a") as f:
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    manifest["n_ciclos"] = manifest.get("n_ciclos", 0) + 1
    manifest["ultimo"] = {k: linea[k] for k in ("ciclo_utc", "corroborada", "sostienen", "titulo", "entregado")}
    tmp = mp + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    os.replace(tmp, mp)
    return f"{dest} {c.nuevos} {len(manifest['files'])}"


def selftest():
    import tempfile
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    fallos = []

    def check(nombre, cond, extra=""):
        print(f"  {'ok  ' if cond else 'FALLA'} {nombre} {extra}")
        if not cond:
            fallos.append(nombre)

    with tempfile.TemporaryDirectory() as td:
        prof = dict(driver="GTiff", height=3, width=3, count=1, dtype="float32", crs="EPSG:4326",
                    transform=from_origin(-70.85, -33.05, 0.1, 0.1))
        t0 = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
        n = 60
        keys = [fmt_key(t0 + timedelta(minutes=30 * i)) for i in range(n)]

        def ras(p, v):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with rasterio.open(p, "w", **prof) as d:
                d.write(np.full((3, 3), v, "float32"), 1)
            return p

        work = os.path.join(td, "work")
        zero = ras(os.path.join(work, "zero.tif"), 0.0)
        feeds = {}
        for name, d in (("primary", os.path.join(work, "steps")), ("goes", os.path.join(work, "feeds/goes/steps")),
                        ("dga", os.path.join(work, "feeds/dga/steps"))):
            lst = []
            for i, k in enumerate(keys):
                if name == "primary" and i >= 44:
                    lst.append(zero)            # cola zerofill por latencia
                else:
                    lst.append(ras(os.path.join(d, f"step_{k}.tif"), float(i)))
            open(os.path.join(d, f"gapmark_{keys[30]}"), "w").close()
            feeds[name] = {"list": ",".join(lst), "newest_real": keys[43 if name == "primary" else 47]}
        fusion = {"span": [keys[0], keys[-1]], "n_steps": n, "feeds": feeds}
        fusion_p = os.path.join(work, "state", "fusion.json"); os.makedirs(os.path.dirname(fusion_p))
        json.dump(fusion, open(fusion_p, "w"))
        # racha: pasos 40..42 en alerta (20:00–21:00Z)
        run = {"alert_level": 0.5, "n_alerts": 3,
               "steps": [{"step": i, "max_probability": 0.9 if 40 <= i <= 42 else 0.1,
                          "alert": {"n_cells": 1, "fraction": 0.1} if 40 <= i <= 42 else None} for i in range(n)]}
        run_p = os.path.join(work, "out", "last_run.json"); os.makedirs(os.path.dirname(run_p))
        json.dump(run, open(run_p, "w"))
        hz = os.path.join(work, "out", "hazard")
        for i in range(n):
            ras(os.path.join(hz, f"hazard_{i:04d}.tif"), 0.9 if 40 <= i <= 42 else 0.1)
        glm = os.path.join(work, "feeds", "glm")
        for k in keys[36:48]:
            ras(os.path.join(glm, "steps", f"step_{k}.tif"), 2.0)
        json.dump({k: {"domain": 2, "max_cell": 2} for k in keys}, open(os.path.join(glm, "counts.json"), "w"))
        json.dump({"recent": {"domain": 7, "max_cell": 3}}, open(os.path.join(glm, "status.json"), "w"))
        ema = os.path.join(td, "ema"); os.makedirs(ema)
        with open(os.path.join(ema, "330019-2026-10.jsonl"), "w") as f:
            for m in range(0, 36 * 60, 10):     # 36 h de registros cada 10 min, 1 mm/registro de 19Z a 21Z
                t = t0 + timedelta(minutes=m)
                mm = 1.0 if datetime(2026, 10, 12, 19, tzinfo=timezone.utc) <= t < datetime(2026, 10, 12, 21, tzinfo=timezone.utc) else 0.0
                f.write(json.dumps({"momento": t.strftime("%Y-%m-%d %H:%M"), "aguaCaidaDelMinuto": f"{mm} mm"}) + "\n")
        cfg = os.path.join(td, "config.env")
        open(cfg, "w").write('BBOX="x"\nDMC_TOKEN="secreto123"\nDMC_USUARIO="yo@x"\nID_A=5.5\n')
        evid = os.path.join(work, "evidence")

        def correr(ep_first, ep_last, corrob=1, titulo="CRUCE NUEVO"):
            a = argparse.Namespace(run_json=run_p, fusion_json=fusion_p, work_dir=work, ep_first=ep_first,
                                   ep_last=ep_last, evid_dir=evid, hazard_dir=hz, glm_dir=glm, ema_dir=ema,
                                   stations="330019:-70.549:-33.455:Tobalaba Ad.", pre_h=6, post_h=3,
                                   corrob=corrob, corrob_feeds="dga", corrob_det="det", titulo=titulo,
                                   delivered=1, config=cfg, repo=os.path.dirname(os.path.abspath(__file__)),
                                   min_free_mb=1)
            return congelar(a)

        out = correr(keys[40], keys[42])
        dest = os.path.join(evid, keys[40])
        print("   →", out)
        man = json.load(open(os.path.join(dest, "manifest.json")))
        check("carpeta nombrada por la racha", os.path.isdir(dest))
        # ventana 14:00Z..00:00Z → pasos 28..48 (21 pasos); primario tiene real hasta 43 → 16 reales + 5 zerofill
        check("primario: 16 pasos reales + 5 zerofill registrados",
              man["feeds"]["primary"]["pasos_congelados"] == 16 and man["feeds"]["primary"]["zerofill_en_ventana"] == keys[44:49],
              str(man["feeds"]["primary"]))
        check("goes/dga: 21 pasos", man["feeds"]["goes"]["pasos_congelados"] == 21 and man["feeds"]["dga"]["pasos_congelados"] == 21)
        check("gapmark en ventana copiado", man["feeds"]["dga"]["gapmarks_en_ventana"] == [keys[30]]
              and os.path.exists(os.path.join(dest, "feeds/dga", f"gapmark_{keys[30]}")))
        check("peligro renombrado por clave (21)", man["hazard_pasos"] == 21
              and os.path.exists(os.path.join(dest, "hazard", f"hazard_{keys[40]}.tif")))
        with rasterio.open(os.path.join(dest, "hazard", f"hazard_{keys[40]}.tif")) as d:
            check("peligro del paso 40 vale 0.9 (no se confundió el índice)", abs(float(d.read(1)[0, 0]) - 0.9) < 1e-6)
        gl = json.load(open(os.path.join(dest, "glm", "counts.json")))
        check("GLM: pasos y counts solo en ventana", set(gl) == set(keys[28:49])
              and len(glob.glob(os.path.join(dest, "glm/steps/*.tif"))) == 12, str(len(gl)))
        res = json.load(open(os.path.join(dest, "ema", "resumen.json")))["330019"]
        check("EMA filtrada a la ventana con 12 mm en 19-21Z",
              res["total_mm"] == 12.0 and res["horario_mm"].get("2026-10-12T19Z") == 6.0
              and res["desde"] == "2026-10-12 14:00" and res["hasta"] == "2026-10-13 00:00", str({k: v for k, v in res.items() if k != "horario_mm"}))
        cf = open(os.path.join(dest, "config.env")).read()
        check("credenciales tachadas en el snapshot", "secreto123" not in cf and "yo@x" not in cf and "ID_A=5.5" in cf)
        check("sha256 por archivo", all("sha256" in v for v in man["files"].values()) and len(man["files"]) > 60)
        lineas = [json.loads(l) for l in open(os.path.join(dest, "ciclos.jsonl"))]
        check("ciclos.jsonl con el veredicto", len(lineas) == 1 and lineas[0]["corroborada"] == 1
              and lineas[0]["glm_recent"]["domain"] == 7 and lineas[0]["ema_total_mm"]["330019"] == 12.0)
        # 2º ciclo, misma racha extendida un paso: añade lo nuevo, no pisa lo viejo
        h40 = os.path.join(dest, "hazard", f"hazard_{keys[40]}.tif")
        sha_antes = sha256(h40)
        ras(os.path.join(hz, "hazard_0040.tif"), 0.3)    # el motor reescribió el ráster: NO debe pisarse
        run["steps"][43]["alert"] = {"n_cells": 2, "fraction": 0.2}; json.dump(run, open(run_p, "w"))
        out2 = correr(keys[40], keys[43], corrob=0, titulo="NO corroborada")
        print("   →", out2)
        man2 = json.load(open(os.path.join(dest, "manifest.json")))
        nuevos = int(out2.split()[1])
        check("2º ciclo: no pisa lo congelado", sha256(h40) == sha_antes)
        check("2º ciclo: añade 1 paso de peligro + 1 goes/dga, 0 primario (zerofill)",
              man2["hazard_pasos"] == 22 and man2["feeds"]["goes"]["pasos_congelados"] == 22
              and man2["feeds"]["primary"]["pasos_congelados"] == 16 and man2["feeds"]["primary"]["zerofill_en_ventana"] == keys[44:50],
              f"nuevos={nuevos} {man2['feeds']['primary']}")
        lineas = [json.loads(l) for l in open(os.path.join(dest, "ciclos.jsonl"))]
        check("2 líneas en ciclos.jsonl, la 2ª NO corroborada", len(lineas) == 2 and lineas[1]["corroborada"] == 0
              and man2["n_ciclos"] == 2 and man2["racha"]["ep_last"] == keys[43])
        check("snapshot de config no se duplica", sum(1 for k in man2["files"] if k.startswith("config")) == 1)
        # sin disco → skip, sin tocar nada
        a = argparse.Namespace(run_json=run_p, fusion_json=fusion_p, work_dir=work, ep_first=keys[40], ep_last=keys[43],
                               evid_dir=evid, hazard_dir=hz, glm_dir=glm, ema_dir=ema, stations="", pre_h=6, post_h=3,
                               corrob=1, corrob_feeds="", corrob_det="", titulo="", delivered=0, config=cfg, repo=td,
                               min_free_mb=10**9)
        check("sin disco libre → skip", congelar(a).startswith("skip disco"))
        # claves por índice (fusion sin span)
        fusion2 = dict(fusion); fusion2.pop("span"); json.dump(fusion2, open(fusion_p, "w"))
        a = argparse.Namespace(run_json=run_p, fusion_json=fusion_p, work_dir=work, ep_first="step0040", ep_last="step0042",
                               evid_dir=evid, hazard_dir=hz, glm_dir=None, ema_dir=None, stations="", pre_h=6, post_h=3,
                               corrob=1, corrob_feeds="", corrob_det="", titulo="", delivered=0, config=None, repo=td,
                               min_free_mb=1)
        out3 = congelar(a)
        m3 = json.load(open(os.path.join(evid, "step0040", "manifest.json")))
        check("fusion sin span: congela por índice", out3.startswith(os.path.join(evid, "step0040"))
              and m3["hazard_pasos"] == 21 and m3["ventana"] == {"idx": [28, 48]}, str(m3.get("ventana")))
    print(f"\n[selftest freeze_evidence] {'TODO OK' if not fallos else f'FALLAS: {fallos}'}")
    return 0 if not fallos else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_json"); ap.add_argument("fusion_json"); ap.add_argument("work_dir")
    ap.add_argument("ep_first"); ap.add_argument("ep_last"); ap.add_argument("evid_dir")
    ap.add_argument("--hazard-dir", default=None); ap.add_argument("--glm-dir", default=None)
    ap.add_argument("--ema-dir", default=None); ap.add_argument("--stations", default="")
    ap.add_argument("--pre-h", type=float, default=6.0); ap.add_argument("--post-h", type=float, default=3.0)
    ap.add_argument("--corrob", type=int, default=1); ap.add_argument("--corrob-feeds", default="")
    ap.add_argument("--corrob-det", default=""); ap.add_argument("--titulo", default="")
    ap.add_argument("--delivered", type=int, default=0); ap.add_argument("--config", default=None)
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--min-free-mb", type=float, default=500.0)
    a = ap.parse_args()
    for k in ("hazard_dir", "glm_dir", "ema_dir", "config"):
        if getattr(a, k) == "-":
            setattr(a, k, None)
    try:
        print(congelar(a))
    except Exception as e:
        print(f"skip error {e.__class__.__name__}: {e}", file=sys.stderr)
        print(f"skip error {e.__class__.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
