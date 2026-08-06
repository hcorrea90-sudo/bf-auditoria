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
            // Version: primer span que no sea km, combustible, transmision ni precio
            // Chips de estado que NO son version
            const skipSpan = /^(Gasolina|Bencina|Di.sel|H.brido|El.ctrico|Autom.tica|Mec.nica|Manual|GNC|GLP|.nico\s*due.o|Pocos\s*kil.metros|Pocos\s*KM|SALE|Winter\s*Sale|Vendido|Nuevo\s*Ingreso|Destacado)$/i;
            for (const el of t.querySelectorAll('span')) {
                const text = el.innerText.trim();
                if (text.length < 4) continue;
                if (/km$/i.test(text)) continue;
                if (/^\$/.test(text)) continue;
                if (/bono|incluye|vendido/i.test(text)) continue;
                if (skipSpan.test(text)) continue;
                if (text === titulo) continue;
                if (/\d/.test(text)) { version = text; break; }
            }

            // Precio: p o span que empiece con $ y tenga digitos largos
            let precio = '';
            for (const el of t.querySelectorAll('p, span')) {
                const text = el.innerText.trim();
                if (/^\$[\d\.\s]{6,}/.test(text)) { precio = text; break; }
            }

            // URL del auto: buscar enlace con href que contenga /autos-usados/
            let url_auto = '';
            for (const a of t.querySelectorAll('a')) {
                const href = a.getAttribute('href') || '';
                if (href.includes('/autos-usados/') || href.includes('/product/')) {
                    url_auto = href.startsWith('http') ? href : 'https://www.brunofritsch.cl' + href;
                    break;
                }
            }
            // Fallback: enlace del id del producto si hay link general
            if (!url_auto) {
                const linkParent = t.closest('a') || t.querySelector('a');
                if (linkParent) {
                    const href = linkParent.getAttribute('href') || '';
                    url_auto = href.startsWith('http') ? href : 'https://www.brunofritsch.cl' + href;
                }
            }

            resultado.push({
                titulo, version, precio_txt: precio,
                combustible, km_txt: km, transmision,
                url_auto,
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

    # Chips de estado que NO son parte de la versión del auto
    CHIPS_ESTADO = re.compile(
        r"\b(Pocos\s*Kil[oó]metros|[uú]nico\s*Due[nñ]o|SALE|WINTER\s*SALE|"
        r"Pocos\s*KM|Nuevo\s*Ingreso|Destacado|Winter\s*Sale)\b",
        re.I
    )

    vehiculos = []
    for d in datos_js:
        precio = parsear_precio(d["precio_txt"] or d["txt_full"])
        km     = parsear_km(d["km_txt"]) or parsear_km(d["txt_full"])

        # Limpiar titulo de chips de estado
        titulo = CHIPS_ESTADO.sub("", d["titulo"]).strip()
        titulo = re.sub(r"\s+", " ", titulo).strip()

        # Agregar version si no es un chip de estado
        version_limpia = CHIPS_ESTADO.sub("", d["version"] or "").strip()
        if version_limpia and version_limpia not in titulo:
            titulo = f"{titulo} {version_limpia}".strip()

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
            "url": d.get("url_auto", ""),
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
        # Solo alertar si el combustible actual NO es ya hibrido/electrico
        if any(x in c for x in ("hibrido", "híbrido", "electrico", "eléctrico")):
            continue
        # Verificar si el titulo sugiere que deberia ser hibrido
        tl = " " + v["titulo"].lower() + " "
        for kw in MHEV_KEYWORDS:
            if kw in tl:
                out.append({
                    "vehiculo":    v["titulo"],
                    "km":          v["km"],
                    "precio":      v["precio"],
                    "url":         v.get("url",""),
                    "comb_actual": v["combustible"] or "No especificado",
                    "deberia":     "Hibrido",
                    "detalle":     f'"{kw.upper().strip()}" indica tecnologia mild-hybrid o hibrida. Revisar catalogacion.',
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
            seq.append({"km": item["km"], "precio": item["precio"], "sube": sube, "url": item.get("url","")})
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
                "precio_ant": r1["precio"], "url_ant": r1.get("url",""),
                "ano_nuevo": a2, "km_nuevo": r2["km"],
                "precio_nuevo": r2["precio"], "url_nuevo": r2.get("url",""),
                "diff_precio": dp, "diff_km": dk, "sev": sev,
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

def extraer_modelo_base(titulo):
    """Extrae las primeras palabras del modelo para comparar (ej: TIGGO 7 PRO, RAV4, X-TRAIL)."""
    # Quitar marca (primera palabra) y año
    t = re.sub(r"\b(19|20)\d{2}\b", "", titulo)
    partes = t.split()
    # Tomar palabras 2-4 como modelo base (ej: TIGGO 7 PRO, RAV4, X-TRAIL)
    return " ".join(partes[1:4]).upper() if len(partes) > 1 else ""

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
            # Verificar que sean el mismo modelo base (evitar mezcla Tiggo 7 vs Tiggo 3)
            modelo_inf = extraer_modelo_base(mi["titulo"])
            modelo_sup = extraer_modelo_base(ms["titulo"])
            if modelo_inf and modelo_sup and modelo_inf != modelo_sup:
                continue
            kd = f"{mi['titulo']}|{ms['titulo']}"
            if kd in seen: continue
            seen.add(kd)
            diff = mi["precio"] - ms["precio"]
            dk   = abs((mi["km"] or 0) - (ms["km"] or 0))
            out.append({
                "modelo":    f"{clave.split('|')[0]} {clave.split('|')[1]}",
                "desc":      desc,
                "ver_inf":   mi["titulo"], "km_inf":  mi["km"], "precio_inf": mi["precio"], "url_inf": mi.get("url",""),
                "ver_sup":   ms["titulo"], "km_sup":  ms["km"], "precio_sup": ms["precio"], "url_sup": ms.get("url",""),
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

# Nueva función generar_html con diseño jerárquico profesional

def fp(n): return f"${n:,.0f}".replace(",", ".") if n else "—"
def fk(n): return f"{n:,.0f} km".replace(",", ".") if n else "—"

def badge_sev(s):
    cfg = {
        "ALTO":  ("#dc2626", "#fef2f2"),
        "MEDIO": ("#d97706", "#fffbeb"),
        "BAJO":  ("#16a34a", "#f0fdf4"),
    }.get(s, ("#6b7280", "#f8fafc"))
    return f'<span style="display:inline-block;background:{cfg[0]};color:#fff;padding:2px 9px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px">{s}</span>'

def generar_html(veh, comb_err, km_p, ano_p, ver_p, stats, fecha_gen, hora_gen):
    import json as _json
    from collections import defaultdict as _dd

    pp = stats["con_precio"] * 100 // stats["total"] if stats["total"] else 0
    nca = sum(1 for h in comb_err if h["sev"] == "ALTO")
    nka = sum(1 for h in km_p     if h["sev"] == "ALTO")
    nva = sum(1 for h in ver_p    if h["sev"] == "ALTO")
    total_alertas = len(comb_err) + len(km_p) + len(ano_p) + len(ver_p)

    # ── Agrupar km_p por MARCA > MODELO ──────────────────────────────────────
    def marca_modelo(titulo):
        partes = titulo.split()
        marca  = partes[0] if partes else "OTRA"
        # modelo = siguientes palabras hasta el año
        import re
        modelo_partes = []
        for p in partes[1:]:
            if re.match(r"^(19|20)\d{2}$", p):
                break
            modelo_partes.append(p)
        return marca.upper(), " ".join(modelo_partes).upper()

    # Agrupar km_p
    km_por_marca = _dd(lambda: _dd(list))
    for h in km_p:
        m, mo = marca_modelo(h["vehiculo"])
        km_por_marca[m][mo].append(h)

    # Agrupar ano_p
    ano_por_marca = _dd(lambda: _dd(list))
    for h in ano_p:
        m, mo = marca_modelo(h["modelo"])
        ano_por_marca[m][mo].append(h)

    # ── Bloque KM vs PRECIO jerárquico ────────────────────────────────────────
    def btn_link(url, texto="Ver en sitio →"):
        if url:
            return f'<a href="{url}" target="_blank" class="btn-link">{texto}</a>'
        return ""

    def render_km_card(h, idx):
        secuencia = ""
        for i, p in enumerate(h["secuencia"]):
            url_btn = btn_link(p.get("url",""))
            if p["sube"]:
                secuencia += f'''<div class="seq-item sube">
                    <div class="seq-km">{fk(p["km"])}</div>
                    <div class="seq-precio">{fp(p["precio"])}</div>
                    <div class="seq-tag">▲ SUBE</div>
                    {url_btn}
                </div>'''
            else:
                secuencia += f'''<div class="seq-item">
                    <div class="seq-km">{fk(p["km"])}</div>
                    <div class="seq-precio">{fp(p["precio"])}</div>
                    {url_btn}
                </div>'''
        diffs = [h["secuencia"][i]["precio"] - h["secuencia"][i-1]["precio"]
                 for i in range(1, len(h["secuencia"]))
                 if h["secuencia"][i]["sube"]]
        diff_max = max(diffs) if diffs else 0
        return f'''<div class="inc-card sev-{h["sev"].lower()}">
            <div class="inc-card-head">
                <div>
                    <span class="inc-version">{h["vehiculo"]}</span>
                    <span class="inc-diff">Sobreprecio máximo: <strong>{fp(diff_max)}</strong></span>
                </div>
                {badge_sev(h["sev"])}
            </div>
            <div class="seq-row">{secuencia}</div>
        </div>'''

    km_html = ""
    for marca in sorted(km_por_marca.keys()):
        modelos = km_por_marca[marca]
        n_marca = sum(len(v) for v in modelos.values())
        marca_id = f"km_marca_{marca.replace(' ','_')}"
        modelos_html = ""
        for modelo in sorted(modelos.keys()):
            casos = modelos[modelo]
            modelo_id = f"km_{marca}_{modelo}".replace(" ","_").replace("-","_")
            cards = "".join(render_km_card(h, i) for i, h in enumerate(casos))
            n_alto = sum(1 for h in casos if h["sev"] == "ALTO")
            tag = f'<span class="marca-alto">{n_alto} ALTO</span>' if n_alto else ""
            modelos_html += f'''<div class="modelo-group">
                <button class="modelo-btn" onclick="toggle('{modelo_id}')">
                    <span class="modelo-name">{modelo}</span>
                    <span class="modelo-meta">{len(casos)} caso{"s" if len(casos)!=1 else ""} {tag}</span>
                    <span class="chevron" id="ch_{modelo_id}">▶</span>
                </button>
                <div class="modelo-body" id="{modelo_id}">{cards}</div>
            </div>'''
        km_html += f'''<div class="marca-group">
            <button class="marca-btn" onclick="toggle('{marca_id}')">
                <span class="marca-name">{marca}</span>
                <span class="marca-count">{n_marca} inconsistencia{"s" if n_marca!=1 else ""}</span>
                <span class="chevron" id="ch_{marca_id}">▶</span>
            </button>
            <div class="marca-body" id="{marca_id}">{modelos_html}</div>
        </div>'''

    if not km_html:
        km_html = '<div class="empty-state">Sin inconsistencias detectadas ✓</div>'

    # ── Bloque AÑO vs PRECIO jerárquico ──────────────────────────────────────
    def render_ano_card(h):
        diff_km_txt = f"+{fk(h['diff_km'])}" if h["diff_km"] >= 0 else fk(h["diff_km"])
        link_ant  = btn_link(h.get("url_ant",""),  f"Ver {h['ano_ant']} →")
        link_nuevo = btn_link(h.get("url_nuevo",""), f"Ver {h['ano_nuevo']} →")
        return f'''<div class="inc-card sev-{h["sev"].lower()}">
            <div class="inc-card-head">
                <div>
                    <span class="inc-version">{h["modelo"]}</span>
                </div>
                {badge_sev(h["sev"])}
            </div>
            <div class="ano-compare">
                <div class="ano-box ano-old">
                    <div class="ano-label">AÑO {h["ano_ant"]}</div>
                    <div class="ano-km">{fk(h["km_ant"])}</div>
                    <div class="ano-precio">{fp(h["precio_ant"])}</div>
                    {link_ant}
                </div>
                <div class="ano-arrow">
                    <div class="ano-diff-precio">−{fp(h["diff_precio"])}</div>
                    <div class="ano-diff-km">{diff_km_txt} km</div>
                    <div>→</div>
                </div>
                <div class="ano-box ano-new">
                    <div class="ano-label">AÑO {h["ano_nuevo"]} <span style="color:#dc2626;font-size:10px">MÁS BARATO</span></div>
                    <div class="ano-km">{fk(h["km_nuevo"])}</div>
                    <div class="ano-precio">{fp(h["precio_nuevo"])}</div>
                    {link_nuevo}
                </div>
            </div>
        </div>'''

    ano_html = ""
    for marca in sorted(ano_por_marca.keys()):
        modelos = ano_por_marca[marca]
        n_marca = sum(len(v) for v in modelos.values())
        marca_id = f"ano_marca_{marca.replace(' ','_')}"
        modelos_html = ""
        for modelo in sorted(modelos.keys()):
            casos = modelos[modelo]
            modelo_id = f"ano_{marca}_{modelo}".replace(" ","_").replace("-","_")
            cards = "".join(render_ano_card(h) for h in casos)
            modelos_html += f'''<div class="modelo-group">
                <button class="modelo-btn" onclick="toggle('{modelo_id}')">
                    <span class="modelo-name">{modelo}</span>
                    <span class="modelo-meta">{len(casos)} caso{"s" if len(casos)!=1 else ""}</span>
                    <span class="chevron" id="ch_{modelo_id}">▶</span>
                </button>
                <div class="modelo-body" id="{modelo_id}">{cards}</div>
            </div>'''
        ano_html += f'''<div class="marca-group">
            <button class="marca-btn" onclick="toggle('{marca_id}')">
                <span class="marca-name">{marca}</span>
                <span class="marca-count">{n_marca} caso{"s" if n_marca!=1 else ""}</span>
                <span class="chevron" id="ch_{marca_id}">▶</span>
            </button>
            <div class="marca-body" id="{marca_id}">{modelos_html}</div>
        </div>'''

    if not ano_html:
        ano_html = '<div class="empty-state">Sin inconsistencias detectadas ✓</div>'

    # ── Bloque VERSION vs PRECIO ──────────────────────────────────────────────
    ver_html = ""
    ver_por_marca = _dd(list)
    for h in ver_p:
        marca = h["modelo"].split()[0]
        ver_por_marca[marca].append(h)

    for marca in sorted(ver_por_marca.keys()):
        casos = ver_por_marca[marca]
        marca_id = f"ver_marca_{marca.replace(' ','_')}"
        cards = ""
        for h in casos:
            diff = h["diff"]
            cards += f'''<div class="inc-card sev-{h["sev"].lower()}">
                <div class="inc-card-head">
                    <div>
                        <span class="inc-version">{h["modelo"]}</span>
                        <span class="inc-diff">{h["desc"]}</span>
                    </div>
                    {badge_sev(h["sev"])}
                </div>
                <div class="ver-compare">
                    <div class="ver-box ver-inf">
                        <div class="ver-tag-inf">VERSIÓN INFERIOR — MÁS CARA</div>
                        <div class="ver-name">{h["ver_inf"]}</div>
                        <div class="ver-detail">{fk(h["km_inf"])} · <strong>{fp(h["precio_inf"])}</strong></div>
                        {btn_link(h.get("url_inf",""), "Ver en sitio →")}
                    </div>
                    <div class="ver-diff">
                        <div style="color:#dc2626;font-weight:700;font-size:13px">+{fp(diff)}</div>
                        <div style="font-size:10px;color:#6b7280">de diferencia</div>
                    </div>
                    <div class="ver-box ver-sup">
                        <div class="ver-tag-sup">VERSIÓN SUPERIOR — MÁS BARATA</div>
                        <div class="ver-name">{h["ver_sup"]}</div>
                        <div class="ver-detail">{fk(h["km_sup"])} · <strong>{fp(h["precio_sup"])}</strong></div>
                        {btn_link(h.get("url_sup",""), "Ver en sitio →")}
                    </div>
                </div>
            </div>'''
        ver_html += f'''<div class="marca-group">
            <button class="marca-btn" onclick="toggle('{marca_id}')">
                <span class="marca-name">{marca}</span>
                <span class="marca-count">{len(casos)} caso{"s" if len(casos)!=1 else ""}</span>
                <span class="chevron" id="ch_{marca_id}">▶</span>
            </button>
            <div class="marca-body" id="{marca_id}">{cards}</div>
        </div>'''

    if not ver_html:
        ver_html = '<div class="empty-state">Sin inconsistencias detectadas ✓</div>'

    # ── Combustible ───────────────────────────────────────────────────────────
    comb_html = ""
    for h in comb_err:
        comb_html += f'''<div class="inc-card sev-alto">
            <div class="inc-card-head">
                <div>
                    <span class="inc-version">{h["vehiculo"]}</span>
                    <span class="inc-diff">{fk(h["km"])} · {fp(h["precio"])}</span>
                </div>
                <div style="display:flex;align-items:center;gap:8px">
                    {btn_link(h.get("url",""), "Ver en sitio →")}
                    {badge_sev(h["sev"])}
                </div>
            </div>
            <div style="display:flex;gap:12px;align-items:center;margin-top:8px;font-size:12px;flex-wrap:wrap">
                <span style="background:#fee2e2;color:#991b1b;padding:3px 10px;border-radius:4px">Actual: {h["comb_actual"]}</span>
                <span style="color:#6b7280">→</span>
                <span style="background:#dcfce7;color:#166534;padding:3px 10px;border-radius:4px">Debería ser: {h["deberia"]}</span>
                <span style="color:#6b7280;font-size:11px">{h["detalle"]}</span>
            </div>
        </div>'''
    if not comb_html:
        comb_html = '<div class="empty-state">Sin inconsistencias detectadas ✓</div>'

    # ── Stats ─────────────────────────────────────────────────────────────────
    marcas_rows = "".join(
        f'<tr><td>{m}</td><td style="text-align:right"><strong>{c}</strong></td></tr>'
        for m, c in stats["top_marcas"]
    )
    tc = sum(stats["combustible"].values()) or 1
    comb_rows = "".join(
        f'<tr><td>{c}</td><td style="text-align:right">{n} <span style="color:#94a3b8">({n*100//tc}%)</span></td></tr>'
        for c, n in list(stats["combustible"].items())[:6]
    )
    anos_rows = "".join(
        f'<tr><td>{a}</td><td style="text-align:right"><strong>{n}</strong></td></tr>'
        for a, n in list(stats["anos"].items())
    )
    tt = sum(stats["transmision"].values()) or 1
    trans_rows = "".join(
        f'<tr><td>{t}</td><td style="text-align:right">{n} <span style="color:#94a3b8">({n*100//tt}%)</span></td></tr>'
        for t, n in stats["transmision"].items()
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auditoría Pricing BF — {fecha_gen}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f1f5f9;--card:#fff;--border:#e2e8f0;
  --text:#0f172a;--muted:#64748b;--accent:#1d4ed8;
  --alto:#dc2626;--medio:#d97706;--bajo:#16a34a;
  --alto-bg:#fef2f2;--medio-bg:#fffbeb;--bajo-bg:#f0fdf4;
}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:var(--bg);color:var(--text);font-size:13px;line-height:1.5}}

/* HEADER */
.hdr{{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);color:#fff;padding:24px 40px 20px}}
.hdr-top{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px}}
.hdr h1{{font-size:18px;font-weight:800;letter-spacing:-.3px}}
.hdr .sub{{color:#93c5fd;font-size:11px;margin-top:2px}}
.hdr-meta{{text-align:right;font-size:11px;color:#cbd5e1;line-height:1.8}}
.hdr-meta strong{{color:#fff}}
.ubadge{{display:inline-flex;align-items:center;gap:5px;background:rgba(29,78,216,.3);
         border:1px solid rgba(99,179,237,.4);color:#93c5fd;
         border-radius:20px;padding:3px 10px;font-size:10px;margin-top:10px}}

/* SEVERITY BAR */
.sev-bar{{background:#1e293b;padding:8px 40px;display:flex;gap:20px;flex-wrap:wrap;font-size:10px;align-items:center}}
.sev-bar .label{{color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.5px}}
.sev-item{{display:flex;align-items:center;gap:6px;color:#94a3b8}}

/* DASHBOARD SUPERIOR */
.dashboard{{background:#fff;border-bottom:2px solid var(--border);padding:0 40px}}

/* Fila 1: KPIs inventario */
.kpis-row{{display:flex;align-items:stretch;border-bottom:1px solid var(--border);overflow-x:auto}}
.kpi-group{{display:flex;flex:1;min-width:0}}
.kpi-group + .kpi-group{{border-left:1px solid var(--border)}}
.kpi{{flex:1;padding:14px 16px;text-align:center;min-width:90px}}
.kpi .num{{font-size:18px;font-weight:800;color:var(--accent);white-space:nowrap}}
.kpi .lbl{{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}}
.kpi-divider{{width:1px;background:var(--border);margin:8px 0}}

/* Fila 2: Alertas ejecutivas */
.alerts-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:0}}
.alert-box{{padding:14px 20px;border-right:1px solid var(--border);position:relative}}
.alert-box:last-child{{border-right:none}}
.alert-box .n{{font-size:28px;font-weight:900;line-height:1}}
.alert-box .desc{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:4px}}
.alert-box .sub{{font-size:9px;color:var(--muted);margin-top:2px}}
.alert-box.alto{{border-top:3px solid var(--alto)}}
.alert-box.medio{{border-top:3px solid var(--medio)}}
.alert-box.bajo{{border-top:3px solid var(--bajo)}}
.alert-box.neutro{{border-top:3px solid #94a3b8}}
.alert-box.alto .n{{color:var(--alto)}}
.alert-box.medio .n{{color:var(--medio)}}
.alert-box.bajo .n{{color:var(--bajo)}}
.alert-box.neutro .n{{color:#475569}}

/* CONTENT */
.content{{padding:24px 40px;max-width:1400px;margin:0 auto}}

/* SECTION */
.section{{margin-bottom:32px}}
.sec-hdr{{display:flex;align-items:center;gap:10px;margin-bottom:12px;
          padding-bottom:10px;border-bottom:2px solid var(--border)}}
.sec-hdr h2{{font-size:14px;font-weight:700;color:var(--text)}}
.sec-hdr .cnt{{margin-left:auto;background:#e2e8f0;color:#475569;
               border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600}}
.sec-hdr .hint{{font-size:10px;color:var(--muted);font-weight:400}}

/* MARCA ACCORDION */
.marca-group{{margin-bottom:6px;border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.marca-btn{{width:100%;display:flex;align-items:center;gap:12px;padding:10px 16px;
            background:#f8fafc;border:none;cursor:pointer;text-align:left;
            transition:background .15s}}
.marca-btn:hover{{background:#f1f5f9}}
.marca-name{{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.2px}}
.marca-count{{font-size:11px;color:var(--muted);margin-left:4px}}
.marca-alto{{background:var(--alto);color:#fff;font-size:9px;font-weight:700;
             padding:1px 6px;border-radius:3px;margin-left:6px}}
.chevron{{margin-left:auto;font-size:10px;color:var(--muted);transition:transform .2s}}
.chevron.open{{transform:rotate(90deg)}}
.marca-body{{display:none;padding:8px 12px;background:#fff}}
.marca-body.open{{display:block}}

/* MODELO ACCORDION */
.modelo-group{{margin-bottom:4px}}
.modelo-btn{{width:100%;display:flex;align-items:center;gap:8px;padding:8px 12px;
             background:#f8fafc;border:1px solid var(--border);border-radius:6px;
             cursor:pointer;text-align:left;transition:background .15s}}
.modelo-btn:hover{{background:#eff6ff}}
.modelo-name{{font-size:12px;font-weight:600;color:#1e40af}}
.modelo-meta{{font-size:10px;color:var(--muted);margin-left:4px}}
.modelo-body{{display:none;padding:8px 0;}}
.modelo-body.open{{display:block}}

/* INCIDENT CARDS */
.inc-card{{border:1px solid var(--border);border-radius:6px;padding:12px;
           margin:6px 0;background:var(--card)}}
.inc-card.sev-alto{{border-left:3px solid var(--alto)}}
.inc-card.sev-medio{{border-left:3px solid var(--medio)}}
.inc-card.sev-bajo{{border-left:3px solid var(--bajo)}}
.inc-card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:10px}}
.inc-version{{font-size:12px;font-weight:600;color:var(--text);display:block}}
.inc-diff{{font-size:10px;color:var(--muted);display:block;margin-top:2px}}

/* SEQUENCE (km/precio) */
.seq-row{{display:flex;flex-wrap:wrap;gap:6px}}
.seq-item{{background:#f8fafc;border:1px solid var(--border);border-radius:5px;
           padding:6px 10px;min-width:120px}}
.seq-item.sube{{background:var(--alto-bg);border-color:#fca5a5}}
.seq-km{{font-size:10px;color:var(--muted)}}
.seq-precio{{font-size:13px;font-weight:700;color:var(--text)}}
.seq-tag{{font-size:9px;color:var(--alto);font-weight:700;margin-top:2px}}

/* AÑO COMPARE */
.ano-compare{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:4px}}
.ano-box{{background:#f8fafc;border:1px solid var(--border);border-radius:6px;padding:8px 14px;min-width:130px}}
.ano-box.ano-new{{background:var(--alto-bg);border-color:#fca5a5}}
.ano-label{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:4px}}
.ano-km{{font-size:10px;color:var(--muted)}}
.ano-precio{{font-size:14px;font-weight:800;color:var(--text)}}
.ano-arrow{{text-align:center;padding:0 4px;color:var(--muted)}}
.ano-diff-precio{{font-size:11px;font-weight:700;color:var(--alto)}}
.ano-diff-km{{font-size:10px;color:var(--muted)}}

/* VERSION COMPARE */
.ver-compare{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:6px}}
.ver-box{{border-radius:6px;padding:10px 14px;min-width:180px;flex:1}}
.ver-box.ver-inf{{background:var(--alto-bg);border:1px solid #fca5a5}}
.ver-box.ver-sup{{background:var(--bajo-bg);border:1px solid #86efac}}
.ver-tag-inf{{font-size:9px;font-weight:700;color:var(--alto);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}}
.ver-tag-sup{{font-size:9px;font-weight:700;color:var(--bajo);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}}
.ver-name{{font-size:11px;font-weight:600;color:var(--text);margin-bottom:4px}}
.ver-detail{{font-size:11px;color:var(--muted)}}
.ver-diff{{text-align:center;padding:0 8px}}

/* STATS */
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}}
.stat-card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}}
.stat-card h4{{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:8px;font-weight:700}}
.stat-card table{{width:100%}}
.stat-card td{{padding:3px 0;font-size:11px;border:none}}

.empty-state{{text-align:center;color:var(--muted);padding:24px;font-size:12px;
              background:#f8fafc;border-radius:6px;border:1px dashed var(--border)}}
.btn-link{{display:inline-block;margin-top:6px;padding:3px 10px;background:#1d4ed8;
           color:#fff;border-radius:4px;font-size:10px;font-weight:600;
           text-decoration:none;white-space:nowrap}}
.btn-link:hover{{background:#1e40af}}

footer{{text-align:center;padding:20px;color:var(--muted);font-size:10px;
        border-top:1px solid var(--border);margin-top:20px}}

@media(max-width:700px){{
  .hdr,.sev-bar,.kpis,.alert-summary,.content{{padding-left:16px;padding-right:16px}}
  .ano-compare,.ver-compare{{flex-direction:column}}
}}
</style>
</head>
<body>

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-top">
    <div>
      <h1>AUDITORÍA PRICING — AUTOS USADOS BRUNO FRITSCH</h1>
      <div class="sub">Revisión de consistencia: precios, kilometraje y equipamiento declarado</div>
      <div class="ubadge">🔄 Se actualiza automáticamente cada lunes</div>
    </div>
    <div class="hdr-meta">
      <div>Generado: <strong>{fecha_gen} {hora_gen}</strong></div>
      <div>Fuente: <strong>brunofritsch.cl/autos-usados</strong></div>
      <div>Inventario: <strong>{stats['total']} vehículos</strong></div>
    </div>
  </div>
</div>

<!-- SEVERITY BAR -->
<div class="sev-bar">
  <span class="label">Criterio de severidad</span>
  <span class="sev-item"><span style="background:#dc2626;color:#fff;padding:1px 7px;border-radius:3px;font-weight:700;font-size:9px">ALTO</span> Diferencia &gt; $1.500.000 o error crítico</span>
  <span class="sev-item"><span style="background:#d97706;color:#fff;padding:1px 7px;border-radius:3px;font-weight:700;font-size:9px">MEDIO</span> Diferencia $600K–$1.500K, requiere revisión</span>
  <span class="sev-item"><span style="background:#16a34a;color:#fff;padding:1px 7px;border-radius:3px;font-weight:700;font-size:9px">BAJO</span> Diferencia &lt; $600.000, puede tener justificación</span>
</div>

<!-- DASHBOARD SUPERIOR -->
<div class="dashboard">
  <!-- Fila 1: KPIs inventario + precios -->
  <div class="kpis-row">
    <div class="kpi-group">
      <div class="kpi"><div class="num">{stats['total']}</div><div class="lbl">Inventario total</div></div>
      <div class="kpi"><div class="num">{stats['con_precio']}</div><div class="lbl">Con precio ({pp}%)</div></div>
      <div class="kpi"><div class="num">{fk(stats['km_prom'])}</div><div class="lbl">Km promedio</div></div>
    </div>
    <div class="kpi-group">
      <div class="kpi"><div class="num">{fp(stats['precio_min'])}</div><div class="lbl">Precio mínimo</div></div>
      <div class="kpi"><div class="num">{fp(stats['precio_prom'])}</div><div class="lbl">Precio promedio</div></div>
      <div class="kpi"><div class="num">{fp(stats['precio_max'])}</div><div class="lbl">Precio máximo</div></div>
    </div>
    <div class="kpi-group">
      <div class="kpi" style="background:#fef2f2"><div class="num" style="color:#dc2626">{total_alertas}</div><div class="lbl">Total alertas</div></div>
      <div class="kpi"><div class="num">{len(comb_err)}</div><div class="lbl">Combustible</div></div>
      <div class="kpi"><div class="num">{len(km_p)}</div><div class="lbl">Km vs precio</div></div>
      <div class="kpi"><div class="num">{len(ano_p)}</div><div class="lbl">Año vs precio</div></div>
      <div class="kpi"><div class="num">{len(ver_p)}</div><div class="lbl">Versión vs precio</div></div>
    </div>
  </div>
  <!-- Fila 2: Resumen ejecutivo de alertas -->
  <div class="alerts-row">
    <div class="alert-box alto">
      <div class="n">{nca + nka + nva}</div>
      <div class="desc">🔴 Prioridad Alta</div>
      <div class="sub">Acción inmediata requerida</div>
    </div>
    <div class="alert-box medio">
      <div class="n">{len(km_p)-nka + len(ano_p)}</div>
      <div class="desc">🟠 Prioridad Media</div>
      <div class="sub">Revisar esta semana</div>
    </div>
    <div class="alert-box bajo">
      <div class="n">{sum(1 for h in km_p if h["sev"]=="BAJO")}</div>
      <div class="desc">🟢 Prioridad Baja</div>
      <div class="sub">Monitorear — puede tener justificación</div>
    </div>
    <div class="alert-box neutro">
      <div class="n">{len(set(h['vehiculo'].split()[0] for h in km_p))}</div>
      <div class="desc">📦 Marcas afectadas</div>
      <div class="sub">Con al menos una inconsistencia</div>
    </div>
  </div>
</div>

<div class="content">

<!-- SECCIÓN 1: COMBUSTIBLE -->
<div class="section">
  <div class="sec-hdr">
    <h2>⚠️ Combustible mal catalogado</h2>
    <span class="hint">Vehículos con tecnología híbrida catalogados incorrectamente</span>
    <span class="cnt">{len(comb_err)} casos</span>
  </div>
  {comb_html}
</div>

<!-- SECCIÓN 2: KM vs PRECIO -->
<div class="section">
  <div class="sec-hdr">
    <h2>📈 Mismo modelo/versión/año — Más km, precio mayor</h2>
    <span class="hint">El precio debería bajar al subir los km en la misma versión</span>
    <span class="cnt">{len(km_p)} grupos</span>
  </div>
  {km_html}
</div>

<!-- SECCIÓN 3: AÑO vs PRECIO -->
<div class="section">
  <div class="sec-hdr">
    <h2>🗓️ Año más nuevo con precio menor (misma versión)</h2>
    <span class="hint">Un modelo más nuevo debería costar igual o más que el anterior en igualdad de condiciones</span>
    <span class="cnt">{len(ano_p)} casos</span>
  </div>
  {ano_html}
</div>

<!-- SECCIÓN 4: VERSIÓN vs PRECIO -->
<div class="section">
  <div class="sec-hdr">
    <h2>🏆 Versión inferior más cara que versión superior</h2>
    <span class="hint">Versión de menor equipamiento/potencia aparece más cara sin justificación en km</span>
    <span class="cnt">{len(ver_p)} casos</span>
  </div>
  {ver_html}
</div>

<!-- ESTADÍSTICAS -->
<div class="section">
  <div class="sec-hdr"><h2>📊 Estadísticas del inventario</h2></div>
  <div class="stats-grid">
    <div class="stat-card"><h4>Top marcas en stock</h4><table><tbody>{marcas_rows}</tbody></table></div>
    <div class="stat-card"><h4>Distribución combustible</h4><table><tbody>{comb_rows}</tbody></table></div>
    <div class="stat-card"><h4>Transmisión</h4><table><tbody>{trans_rows}</tbody></table></div>
    <div class="stat-card"><h4>Por año de fabricación</h4><table><tbody>{anos_rows}</tbody></table></div>
  </div>
</div>

</div>

<footer>
  Informe generado automáticamente · Bruno Fritsch Autos Usados · brunofritsch.cl · {fecha_gen} {hora_gen} · {stats['total']} vehículos analizados
</footer>

<script>
function toggle(id) {{
  const body = document.getElementById(id);
  const ch   = document.getElementById('ch_' + id);
  if (!body) return;
  const open = body.classList.toggle('open');
  if (ch) ch.classList.toggle('open', open);
}}
// Abrir primer grupo de cada sección automáticamente
document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('.marca-body').forEach((b, i) => {{
    if (i === 0 || i === 1) {{
      b.classList.add('open');
      const ch = document.getElementById('ch_' + b.id);
      if (ch) ch.classList.add('open');
    }}
  }});
}});
</script>
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
