"""
bf_scraper.py - Auditoria brunofritsch.cl/autos-usados
Usa Playwright para renderizar JS y capturar precios reales.
"""

import re
import sys
import time
import json
import logging
from datetime import datetime
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

LIST_URL    = "https://www.brunofritsch.cl/autos-usados"
PAGE_SIZE   = 100
MAX_PAGINAS = 15
OUTPUT_DIR  = Path(__file__).parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "index.html"
DATA_FILE   = OUTPUT_DIR / "data.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

MHEV_KEYWORDS = ["mhev", "mild hybrid", " b5 ", " b4 ", " b6 ", " b8 ", "48v", "e-tsi", "etsi", "phev"]

# ── Scraping con Playwright ───────────────────────────────────────────────────

def parsear_precio(txt):
    matches = re.findall(r"\$\s*([\d]{1,3}(?:[\.\s][\d]{3})+)", txt)
    for m in matches:
        limpio = re.sub(r"[^\d]", "", m)
        try:
            v = int(limpio)
            if 1_000_000 <= v <= 200_000_000:
                return v
        except:
            pass
    return None

def parsear_km(txt):
    m = re.search(r"([\d]{1,3}(?:\.[\d]{3})*)\s*km", txt, re.I)
    if m:
        try:
            v = int(m.group(1).replace(".", ""))
            if 0 < v < 999_999:
                return v
        except:
            pass
    return None

def extraer_ano(txt):
    m = re.search(r"\b(199\d|20[012]\d)\b", txt)
    return int(m.group(1)) if m else None

def extraer_combustible(txt):
    for p in ["Hibrido", "Electrico", "Gasolina", "Bencina", "Diesel", "GNC", "GLP"]:
        if re.search(rf"\b{p}\b", txt, re.I):
            return p.capitalize()
    return ""

def extraer_transmision(txt):
    if re.search(r"\bAutomatica\b|\bAutomática\b", txt, re.I): return "Automatica"
    if re.search(r"\bMecanica\b|\bMecánica\b|\bManual\b",  txt, re.I): return "Mecanica"
    return ""

def parsear_tarjeta(t):
    txt = t.get_text(" ", strip=True)
    precio = parsear_precio(txt)
    km     = parsear_km(txt)
    ano    = extraer_ano(txt)

    titulo = ""
    for tag in t.find_all("p"):
        cls = " ".join(tag.get("class", []))
        if "body1" in cls:
            candidato = tag.get_text(" ", strip=True)
            if len(candidato) > 6 and re.search(r"[A-Z]", candidato):
                titulo = candidato
                break
    if not titulo:
        a = t.find("a")
        titulo = a.get_text(" ", strip=True)[:120] if a else txt[:80]
    titulo = re.sub(r"\s+", " ", titulo).strip()
    marca  = titulo.split()[0].upper() if titulo else "DESCONOCIDA"

    return {
        "titulo":      titulo,
        "marca":       marca,
        "ano":         ano,
        "km":          km,
        "precio":      precio,
        "combustible": extraer_combustible(txt),
        "transmision": extraer_transmision(txt),
    }

def scrape_pagina_pw(page, pagina):
    url = f"{LIST_URL}?page={pagina}&pageSize={PAGE_SIZE}"
    page.goto(url, wait_until="networkidle", timeout=60000)
    try:
        page.wait_for_selector("#grid-mode-product-card", timeout=15000)
    except:
        pass
    time.sleep(2)

    # Extraer datos estructurados via JavaScript directamente desde el DOM
    datos_js = page.evaluate("""() => {
        const tarjetas = document.querySelectorAll('#grid-mode-product-card');
        const resultado = [];
        tarjetas.forEach(t => {
            const txt = t.innerText || '';

            // Combustible, km, transmision desde chips visuales
            let combustible = '', km = '', transmision = '';
            for (const el of t.querySelectorAll('p, span, div')) {
                const text = el.innerText.trim();
                if (/^(Gasolina|Bencina|Di[eé]sel|H[ií]brido|El[eé]ctrico|GNC|GLP)$/i.test(text)) combustible = text;
                if (/^\d{1,3}\.\d{3}\s*km$/i.test(text)) km = text;
                if (/^(Autom[aá]tica|Mec[aá]nica|Manual)$/i.test(text)) transmision = text;
            }

            // Titulo: primer p con mayusculas que NO sea bono ni precio
            let titulo = '', version = '';
            const parrafos = t.querySelectorAll('p');
            // Titulo: primer parrafo con texto del auto (no precio ni bono)
            for (const p of parrafos) {
                const text = p.innerText.trim();
                if (/bono|incluye|^\$/i.test(text)) continue;
                if (text.length < 3) continue;
                titulo = text;
                break;
            }
            // Version: buscar en spans (ej: "1.5T LIMITED 2WD CVT AT 5P")
            for (const el of t.querySelectorAll('span')) {
                const text = el.innerText.trim();
                // La version tiene patron: numero.letras seguido de traccion/transmision
                if (/\d+\.\d+.*\b(4[Xx][24]|2[Ww][Dd]|[Aa][Ww][Dd]|[Mm][Tt]|[Cc][Vv][Tt]|[Aa][Tt])\b/.test(text)) {
                    version = text;
                    break;
                }
            }

            // Precio: p o span que empiece con $ y tenga digitos largos
            let precio = '';
            for (const el of t.querySelectorAll('p, span')) {
                const text = el.innerText.trim();
                if (/^\$[\d\.\s]{6,}/.test(text)) { precio = text; break; }
            }

            resultado.push({
                titulo, version, precio_txt: precio,
                combustible, km_txt: km, transmision,
                txt_full: txt.substring(0, 300)
            });
        });
        return resultado;
    }""")

    html    = page.content()
    soup    = BeautifulSoup(html, "lxml")
    total_m = re.search(r"(\d+)\s*autos", soup.get_text())
    total   = int(total_m.group(1)) if total_m else 0
    hay_sig = (pagina * PAGE_SIZE) < total if total else len(datos_js) >= PAGE_SIZE * 0.7

    vehiculos = []
    for d in datos_js:
        precio = parsear_precio(d["precio_txt"] or d["txt_full"])
        km     = parsear_km(d["km_txt"]) or parsear_km(d["txt_full"])
        titulo = d["titulo"]
        if d["version"] and d["version"] not in titulo:
            titulo = f"{titulo} {d['version']}".strip()
        titulo = re.sub(r"\s+", " ", titulo).strip()
        if not titulo:
            titulo = d["txt_full"][:80].strip()
        ano   = extraer_ano(titulo) or extraer_ano(d["txt_full"])
        marca = titulo.split()[0].upper() if titulo else "DESCONOCIDA"
        comb  = d["combustible"] or extraer_combustible(d["txt_full"])
        trans = d["transmision"] or extraer_transmision(d["txt_full"])
        vehiculos.append({
            "titulo": titulo, "marca": marca, "ano": ano,
            "km": km, "precio": precio, "combustible": comb, "transmision": trans,
        })

    log.info(f"  Pagina {pagina:02d}: {len(vehiculos)} autos | precios: {sum(1 for v in vehiculos if v['precio'])} | combustible: {sum(1 for v in vehiculos if v['combustible'])} | total sitio: {total}")
    return vehiculos, hay_sig

def scrape_todo():
    todos = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            locale="es-CL",
        )
        page = ctx.new_page()
        log.info(f"Scraping {LIST_URL}")
        pagina = 1
        while pagina <= MAX_PAGINAS:
            items, hay_sig = scrape_pagina_pw(page, pagina)
            todos.extend(items)
            if not items or not hay_sig:
                break
            pagina += 1
        browser.close()
    log.info(f"Total extraidos: {len(todos)}")
    # Log primeros 5 titulos para diagnostico
    for v in todos[:5]:
        log.info(f"  MUESTRA titulo='{v['titulo']}' comb='{v['combustible']}' km={v['km']} precio={v['precio']}")
    return todos

# ── Análisis ──────────────────────────────────────────────────────────────────

def clave_version(v):
    t = v["titulo"].upper()
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = re.sub(r"[\d\.]+\s*KM", "", t, flags=re.I)
    t = re.sub(r"\$[\d\.]+", "", t)
    return re.sub(r"\s+", " ", t).strip()

def analizar_combustible(veh):
    out = []
    for v in veh:
        c = (v["combustible"] or "").lower()
        if any(x in c for x in ("hibrido","electrico")):
            continue
        tl = " " + v["titulo"].lower() + " "
        for kw in MHEV_KEYWORDS:
            if kw in tl:
                out.append({
                    "vehiculo":    v["titulo"],
                    "km":          v["km"],
                    "precio":      v["precio"],
                    "comb_actual": v["combustible"] or "No especificado",
                    "deberia":     "Hibrido",
                    "detalle":     f'"{kw.upper().strip()}" indica tecnologia mild-hybrid o hibrida.',
                    "sev":         "ALTO",
                })
                break
    return out

def analizar_km_precio(veh):
    grupos = defaultdict(list)
    for v in veh:
        if None in (v["km"], v["precio"], v["ano"]) or v["km"] == 0:
            continue
        grupos[f"{clave_version(v)}|{v['ano']}"].append(v)
    out = []
    for _, items in grupos.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda x: x["km"])
        seq = []
        anomalia = False
        for i, item in enumerate(items):
            sube = i > 0 and item["precio"] > items[i-1]["precio"]
            if sube:
                anomalia = True
            seq.append({"km": item["km"], "precio": item["precio"], "sube": sube})
        if not anomalia:
            continue
        diff_max = max(
            items[i]["precio"] - items[i-1]["precio"]
            for i in range(1, len(items))
            if items[i]["precio"] > items[i-1]["precio"]
        )
        sev = "ALTO" if diff_max >= 1_500_000 else ("MEDIO" if diff_max >= 600_000 else "BAJO")
        out.append({"vehiculo": items[0]["titulo"], "secuencia": seq, "sev": sev})
    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out

def analizar_ano_precio(veh):
    grupos = defaultdict(list)
    for v in veh:
        if None in (v["km"], v["precio"], v["ano"]):
            continue
        grupos[clave_version(v)].append(v)
    out = []
    for _, items in grupos.items():
        anos = sorted(set(v["ano"] for v in items))
        if len(anos) < 2:
            continue
        for a1, a2 in combinations(anos, 2):
            g1 = sorted([v for v in items if v["ano"]==a1], key=lambda x: x["precio"])
            g2 = sorted([v for v in items if v["ano"]==a2], key=lambda x: x["precio"])
            r1 = g1[len(g1)//2]; r2 = g2[len(g2)//2]
            if r2["precio"] >= r1["precio"]:
                continue
            dp = r1["precio"] - r2["precio"]
            dk = r2["km"] - r1["km"]
            sev = "MEDIO" if dp >= 1_200_000 and dk < 90_000 else "BAJO"
            out.append({
                "modelo": r1["titulo"], "ano_ant": a1, "km_ant": r1["km"],
                "precio_ant": r1["precio"], "ano_nuevo": a2, "km_nuevo": r2["km"],
                "precio_nuevo": r2["precio"], "diff_precio": dp, "diff_km": dk, "sev": sev,
            })
    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out[:12]

JERARQUIAS = [
    (r"prado.*super\s*lujo",   r"prado.*vx-?l",         "Prado: SUPER LUJO < VX-L"),
    (r"landtrek.*active.*150", r"landtrek.*action.*180", "Landtrek: ACTIVE 150HP < ACTION 180HP"),
    (r"sportage.*ex.*2wd",     r"sportage.*ex.*awd",     "Sportage EX: 2WD < AWD"),
    (r"x-?trail.*\bsense\b",   r"x-?trail.*exclusive",   "X-Trail: SENSE < EXCLUSIVE"),
    (r"rav4.*\ble\b.*\bmt\b",  r"rav4.*\ble\b.*\bcvt\b", "RAV4: LE MT < LE CVT"),
    (r"rav4.*\ble\b",          r"rav4.*\bvx\b",          "RAV4: LE < VX"),
    (r"tucson.*\bgl\b",        r"tucson.*\bgls\b",       "Tucson: GL < GLS"),
    (r"\bmg\b.*\bstd\b",       r"\bmg\b.*\blux\b",       "MG: STD < LUX"),
    (r"tiggo.*\bgls\b",        r"tiggo.*\bglx\b",        "Tiggo: GLS < GLX"),
    (r"2wd",                   r"4wd|4x4|awd",           "Traccion: 2WD < 4WD/AWD"),
]

def analizar_version_precio(veh):
    grupos = defaultdict(list)
    for v in veh:
        if None in (v["precio"], v["ano"]):
            continue
        grupos[f"{v['marca']}|{v['ano']}"].append(v)
    out = []; seen = set()
    for clave, items in grupos.items():
        for pi, ps, desc in JERARQUIAS:
            inf = [v for v in items if re.search(pi,v["titulo"],re.I) and not re.search(ps,v["titulo"],re.I)]
            sup = [v for v in items if re.search(ps,v["titulo"],re.I)]
            if not inf or not sup: continue
            mi = max(inf, key=lambda x: x["precio"])
            ms = max(sup, key=lambda x: x["precio"])
            if mi["precio"] <= ms["precio"]: continue
            kd = f"{mi['titulo']}|{ms['titulo']}"
            if kd in seen: continue
            seen.add(kd)
            diff = mi["precio"] - ms["precio"]
            dk   = abs((mi["km"] or 0) - (ms["km"] or 0))
            out.append({
                "modelo":    f"{clave.split('|')[0]} {clave.split('|')[1]}",
                "desc":      desc,
                "ver_inf":   mi["titulo"], "km_inf":  mi["km"], "precio_inf": mi["precio"],
                "ver_sup":   ms["titulo"], "km_sup":  ms["km"], "precio_sup": ms["precio"],
                "diff": diff, "diff_km": dk,
                "sev": "ALTO" if diff >= 1_000_000 else "MEDIO",
            })
    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out

def estadisticas(veh):
    precios = [v["precio"] for v in veh if v["precio"]]
    kms     = [v["km"]     for v in veh if v["km"] and v["km"] > 0]
    marcas  = defaultdict(int); combs = defaultdict(int)
    anos    = defaultdict(int); trans = defaultdict(int)
    for v in veh:
        marcas[v["marca"]] += 1
        combs[v["combustible"] or "No especificado"] += 1
        if v["ano"]:         anos[v["ano"]] += 1
        if v["transmision"]: trans[v["transmision"]] += 1
    return {
        "total":       len(veh),
        "con_precio":  len(precios),
        "precio_min":  min(precios) if precios else 0,
        "precio_max":  max(precios) if precios else 0,
        "precio_prom": int(sum(precios)/len(precios)) if precios else 0,
        "km_prom":     int(sum(kms)/len(kms)) if kms else 0,
        "top_marcas":  sorted(marcas.items(), key=lambda x: -x[1])[:10],
        "combustible": dict(sorted(combs.items(),  key=lambda x: -x[1])),
        "anos":        dict(sorted(anos.items(),   key=lambda x: -x[0])[:14]),
        "transmision": dict(trans),
    }

# ── HTML ──────────────────────────────────────────────────────────────────────

def fp(n): return f"${n:,.0f}".replace(",", ".") if n else "-"
def fk(n): return f"{n:,.0f} km".replace(",", ".") if n else "-"
def badge(s):
    c = {"ALTO": "#dc2626", "MEDIO": "#d97706", "BAJO": "#16a34a"}.get(s, "#6b7280")
    return f'<span class="badge" style="background:{c}">{s}</span>'
def empty_row(cols):
    return f'<tr><td colspan="{cols}" class="empty">Sin inconsistencias detectadas</td></tr>'

def generar_html(veh, comb_err, km_p, ano_p, ver_p, stats, fecha_gen, hora_gen):
    fc = ""
    for h in comb_err:
        fc += (f"<tr><td><strong>{h['vehiculo']}</strong></td>"
               f"<td>{fk(h['km'])}</td><td>{fp(h['precio'])}</td>"
               f"<td>{h['comb_actual']}</td><td>Hibrido</td>"
               f"<td class='note'>{h['detalle']}</td><td>{badge(h['sev'])}</td></tr>")
    if not fc: fc = empty_row(7)

    fkp = ""
    for h in km_p[:15]:
        partes = []
        for p in h["secuencia"]:
            cls  = ' class="sube"' if p["sube"] else ""
            icon = " SUBE"         if p["sube"] else ""
            partes.append(f"<span{cls}>{fk(p['km'])} {fp(p['precio'])}{icon}</span>")
        fkp += f"<tr><td><strong>{h['vehiculo']}</strong></td><td class='seq'>{' '.join(partes)}</td><td>{badge(h['sev'])}</td></tr>"
    if not fkp: fkp = empty_row(3)

    fa = ""
    for h in ano_p:
        fa += (f"<tr><td>{h['modelo']}</td>"
               f"<td>{h['ano_ant']}<br><small>{fk(h['km_ant'])}<br>{fp(h['precio_ant'])}</small></td>"
               f"<td>{h['ano_nuevo']}<br><small>{fk(h['km_nuevo'])}<br>{fp(h['precio_nuevo'])}</small></td>"
               f"<td class='note'>{h['ano_nuevo']} mas barato en {fp(h['diff_precio'])} con {fk(h['diff_km'])} mas.</td>"
               f"<td>{badge(h['sev'])}</td></tr>")
    if not fa: fa = empty_row(5)

    fv = ""
    for h in ver_p:
        fv += (f"<tr><td>{h['modelo']}<br><small style='color:#6b7280'>{h['desc']}</small></td>"
               f"<td><span class='tag-inf'>INFERIOR</span><br><small>{h['ver_inf']}<br>{fk(h['km_inf'])} {fp(h['precio_inf'])}</small></td>"
               f"<td><span class='tag-sup'>SUPERIOR</span><br><small>{h['ver_sup']}<br>{fk(h['km_sup'])} {fp(h['precio_sup'])}</small></td>"
               f"<td class='note'>Version inferior {fp(h['diff'])} mas cara con {fk(h['diff_km'])} diferencia.</td>"
               f"<td>{badge(h['sev'])}</td></tr>")
    if not fv: fv = empty_row(5)

    mh  = "".join(f"<tr><td>{m}</td><td><strong>{c}</strong></td></tr>" for m,c in stats["top_marcas"])
    tc  = sum(stats["combustible"].values()) or 1
    ch  = "".join(f"<tr><td>{c}</td><td>{n} ({n*100//tc}%)</td></tr>" for c,n in list(stats["combustible"].items())[:7])
    ah  = "".join(f"<tr><td>{a}</td><td><strong>{n}</strong></td></tr>" for a,n in list(stats["anos"].items()))
    tt  = sum(stats["transmision"].values()) or 1
    th2 = "".join(f"<tr><td>{t}</td><td>{n} ({n*100//tt}%)</td></tr>" for t,n in stats["transmision"].items())

    nca = sum(1 for h in comb_err if h["sev"]=="ALTO")
    nka = sum(1 for h in km_p     if h["sev"]=="ALTO")
    nva = sum(1 for h in ver_p    if h["sev"]=="ALTO")
    pp  = stats["con_precio"]*100//stats["total"] if stats["total"] else 0

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auditoria BF Usados - {fecha_gen}</title>
<style>
:root{{--bg:#f8fafc;--card:#fff;--border:#e2e8f0;--text:#0f172a;--muted:#64748b;--accent:#2563eb;--alto:#dc2626;--medio:#d97706;--bajo:#16a34a}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}}
.hdr{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);color:#fff;padding:28px 40px 20px}}
.hdr h1{{font-size:20px;font-weight:800}}.hdr .sub{{color:#94a3b8;font-size:12px;margin-top:3px}}
.hdr .meta{{display:flex;gap:20px;margin-top:14px;flex-wrap:wrap;font-size:12px}}
.hdr .meta span{{color:#cbd5e1}}.hdr .meta strong{{color:#fff}}
.ubadge{{display:inline-flex;align-items:center;gap:6px;background:rgba(37,99,235,.25);border:1px solid rgba(37,99,235,.4);color:#93c5fd;border-radius:20px;padding:3px 10px;font-size:11px;margin-top:10px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;padding:20px 40px;background:#fff;border-bottom:1px solid var(--border)}}
.kpi{{background:#eff6ff;border-radius:8px;padding:12px;text-align:center}}
.kpi .num{{font-size:22px;font-weight:800;color:var(--accent)}}.kpi .lbl{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
.content{{padding:28px 40px;max-width:1400px;margin:0 auto}}.section{{margin-bottom:36px}}
.sec-title{{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;border-bottom:2px solid var(--border);padding-bottom:8px;margin-bottom:14px}}
.sec-title .cnt{{margin-left:auto;background:#e2e8f0;border-radius:20px;padding:1px 10px;font-size:11px;color:#475569;font-weight:600}}
.hint{{font-size:11px;color:var(--muted);margin-bottom:10px}}
table{{width:100%;border-collapse:collapse}}
th{{background:#f1f5f9;text-align:left;padding:7px 10px;font-size:11px;color:var(--muted);text-transform:uppercase;border-bottom:1px solid var(--border)}}
td{{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:top;font-size:12px}}
tr:last-child td{{border-bottom:none}}tr:hover td{{background:#fafbff}}
td.note{{font-size:11px;color:#475569;max-width:260px}}
td.seq span{{display:inline-block;margin-right:8px;margin-bottom:3px;font-size:11px;background:#f1f5f9;border-radius:4px;padding:2px 6px}}
td.seq span.sube{{background:#fef2f2;color:var(--alto);font-weight:600}}
td.empty{{text-align:center;color:var(--muted);padding:20px}}
.badge{{display:inline-block;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700}}
.tag-inf{{display:inline-block;background:#fef2f2;color:var(--alto);font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px}}
.tag-sup{{display:inline-block;background:#f0fdf4;color:var(--bajo);font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}}
.stat-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}}
.stat-card h4{{font-size:11px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}}
.stat-card td{{padding:3px 4px;border:none;font-size:12px}}
.res-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
.res-card{{border-radius:10px;padding:14px;border-left:4px solid}}
.res-card.r{{background:#fff5f5;border-color:var(--alto)}}.res-card.o{{background:#fffbeb;border-color:var(--medio)}}
.res-card.y{{background:#fefce8;border-color:#ca8a04}}.res-card.g{{background:#f0fdf4;border-color:var(--bajo)}}
.res-card h4{{font-size:11px;font-weight:700;text-transform:uppercase;margin-bottom:7px}}
.res-card ul{{padding-left:14px;font-size:11px;line-height:1.9}}
footer{{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border)}}
@media(max-width:680px){{.hdr,.kpis,.content{{padding-left:16px;padding-right:16px}}}}
</style></head><body>
<div class="hdr">
  <h1>INFORME ANALISIS - AUTOS USADOS BRUNO FRITSCH</h1>
  <div class="sub">Revision de consistencia: precio vs año/km y equipamiento declarado</div>
  <div class="meta"><span>Generado: <strong>{fecha_gen} {hora_gen}</strong></span><span>Fuente: <strong>brunofritsch.cl/autos-usados</strong></span><span>Total: <strong>{stats['total']} vehiculos</strong></span></div>
  <div class="ubadge">Se actualiza automaticamente cada lunes</div>
</div>
<div class="kpis">
  <div class="kpi"><div class="num">{stats['total']}</div><div class="lbl">Autos totales</div></div>
  <div class="kpi"><div class="num">{stats['con_precio']}</div><div class="lbl">Con precio ({pp}%)</div></div>
  <div class="kpi"><div class="num">{len(comb_err)}</div><div class="lbl">Combustible incorrecto</div></div>
  <div class="kpi"><div class="num">{len(km_p)}</div><div class="lbl">Km vs precio</div></div>
  <div class="kpi"><div class="num">{len(ano_p)}</div><div class="lbl">Año vs precio</div></div>
  <div class="kpi"><div class="num">{len(ver_p)}</div><div class="lbl">Version vs precio</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_min'])}</div><div class="lbl">Precio minimo</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_max'])}</div><div class="lbl">Precio maximo</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_prom'])}</div><div class="lbl">Precio promedio</div></div>
  <div class="kpi"><div class="num">{fk(stats['km_prom'])}</div><div class="lbl">Km promedio</div></div>
</div>
<div class="content">
<div class="section"><div class="sec-title">Combustible mal catalogado <span class="cnt">{len(comb_err)} casos</span></div>
<table><thead><tr><th>Vehiculo</th><th>Km</th><th>Precio</th><th>Actual</th><th>Deberia ser</th><th>Detalle</th><th>Sev.</th></tr></thead><tbody>{fc}</tbody></table></div>
<div class="section"><div class="sec-title">Mismo modelo/año/version - Mas km, precio mayor <span class="cnt">{len(km_p)} grupos</span></div>
<p class="hint">Criterio: misma version y año exactos, el precio deberia bajar al subir los km.</p>
<table><thead><tr><th>Vehiculo</th><th>Secuencia km - precio</th><th>Sev.</th></tr></thead><tbody>{fkp}</tbody></table></div>
<div class="section"><div class="sec-title">Año mas nuevo con precio menor <span class="cnt">{len(ano_p)} casos</span></div>
<table><thead><tr><th>Modelo</th><th>Año antiguo</th><th>Año nuevo</th><th>Nota</th><th>Sev.</th></tr></thead><tbody>{fa}</tbody></table></div>
<div class="section"><div class="sec-title">Version inferior mas cara que version superior <span class="cnt">{len(ver_p)} casos</span></div>
<p class="hint">Version de menor equipamiento aparece mas cara sin que los km justifiquen la brecha.</p>
<table><thead><tr><th>Modelo / Año</th><th>Version inferior</th><th>Version superior</th><th>Analisis</th><th>Sev.</th></tr></thead><tbody>{fv}</tbody></table></div>
<div class="section"><div class="sec-title">Estadisticas del inventario</div>
<div class="stats-grid">
  <div class="stat-card"><h4>Top 10 marcas</h4><table><tbody>{mh}</tbody></table></div>
  <div class="stat-card"><h4>Combustible</h4><table><tbody>{ch}</tbody></table></div>
  <div class="stat-card"><h4>Transmision</h4><table><tbody>{th2}</tbody></table></div>
  <div class="stat-card"><h4>Por año</h4><table><tbody>{ah}</tbody></table></div>
</div></div>
<div class="section"><div class="sec-title">Resumen y recomendaciones</div>
<div class="res-grid">
  <div class="res-card r"><h4>Prioridad Alta</h4><ul><li>Combustible incorrecto: <strong>{nca}</strong></li><li>Km vs precio (ALTO): <strong>{nka}</strong></li><li>Version inferior mas cara (ALTO): <strong>{nva}</strong></li></ul></div>
  <div class="res-card o"><h4>Prioridad Media</h4><ul><li>Km vs precio (MEDIO/BAJO): <strong>{len(km_p)-nka}</strong></li><li>Año vs precio: <strong>{len(ano_p)}</strong></li></ul></div>
  <div class="res-card y"><h4>Revisar</h4><ul><li>MHEV, B4-B8, E-TSI como Hibrido</li><li>AWD/4WD vs 2WD</li><li>Top trim mas barato que base</li></ul></div>
  <div class="res-card g"><h4>Cobertura</h4><ul><li><strong>{stats['total']}</strong> vehiculos</li><li><strong>{pp}%</strong> con precio</li><li>Actualizacion: lunes automatico</li></ul></div>
</div></div>
</div>
<footer>Informe generado automaticamente - Bruno Fritsch Autos Usados - brunofritsch.cl - {fecha_gen} {hora_gen} - {stats['total']} vehiculos</footer>
</body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.now()
    fecha_gen = now.strftime("%d/%m/%Y")
    hora_gen  = now.strftime("%H:%M")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=" * 50)
    log.info("  AUDITORIA BF USADOS (Playwright)")
    log.info(f"  {fecha_gen} {hora_gen}")
    log.info("=" * 50)
    veh = scrape_todo()
    if not veh:
        log.error("Sin vehiculos. Abortando.")
        sys.exit(1)
    comb_err = analizar_combustible(veh)
    km_p     = analizar_km_precio(veh)
    ano_p    = analizar_ano_precio(veh)
    ver_p    = analizar_version_precio(veh)
    stats    = estadisticas(veh)
    html     = generar_html(veh, comb_err, km_p, ano_p, ver_p, stats, fecha_gen, hora_gen)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    DATA_FILE.write_text(
        json.dumps({"generado": now.isoformat(), "total": len(veh),
                    "con_precio": stats["con_precio"], "vehiculos": veh[:50]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info(f"Total:{stats['total']} | Precio:{stats['con_precio']} | Km_prom:{stats['km_prom']} | "
             f"Combustible:{len(comb_err)} | KmPrecio:{len(km_p)} | AnoPrecio:{len(ano_p)} | Version:{len(ver_p)}")

if __name__ == "__main__":
    main()
