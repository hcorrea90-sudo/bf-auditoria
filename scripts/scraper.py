"""
bf_scraper.py — Auditoría completa de brunofritsch.cl/autos-usados
Recorre TODAS las páginas del inventario y genera docs/index.html
"""

import re
import sys
import time
import json
import logging
from datetime import date, datetime
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuración ─────────────────────────────────────────────────────────────
BASE_URL    = "https://www.brunofritsch.cl/autos-usados"
PAGE_SIZE   = 100
DELAY_SEG   = 1.5
MAX_PAGINAS = 50       # tope de seguridad (~5.000 autos)
OUTPUT_DIR  = Path(__file__).parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "index.html"
DATA_FILE   = OUTPUT_DIR / "data.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# Indicadores mild-hybrid / híbrido mal catalogado
MHEV_KEYWORDS = [
    "mhev", "mild hybrid", " b5 ", " b4 ", " b6 ", " b8 ",
    "48v", "e-tsi", "etsi", "phev", "hev",
]

# ── Parsers ───────────────────────────────────────────────────────────────────

def parsear_precio(texto: str) -> int | None:
    limpio = re.sub(r"[^\d]", "", str(texto or ""))
    v = int(limpio) if limpio else None
    # Sanity check: precios razonables entre $1M y $200M CLP
    return v if v and 1_000_000 <= v <= 200_000_000 else None

def parsear_km(texto: str) -> int | None:
    limpio = re.sub(r"[^\d]", "", str(texto or ""))
    v = int(limpio) if limpio else None
    return v if v and 0 <= v <= 999_999 else None

def extraer_ano(texto: str) -> int | None:
    m = re.search(r"\b(199\d|20[012]\d)\b", str(texto or ""))
    return int(m.group(1)) if m else None

# ── Scraping ──────────────────────────────────────────────────────────────────

def scrape_pagina(session: requests.Session, pagina: int) -> tuple[list[dict], bool]:
    url = f"{BASE_URL}?page={pagina}&pageSize={PAGE_SIZE}"
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Página {pagina}: {e}")
        return [], False

    soup = BeautifulSoup(resp.text, "lxml")

    # Selectores en orden de especificidad
    tarjetas = (
        soup.select("div.car-card")
        or soup.select("article.vehicle-card")
        or soup.select("div[class*='VehicleCard']")
        or soup.select("div[class*='car-item']")
        or soup.select("li[class*='vehicle']")
    )

    # Último recurso: divs que contengan precio CLP
    if not tarjetas:
        candidatos = soup.find_all("div", recursive=True)
        tarjetas = [
            d for d in candidatos
            if re.search(r"\$\s*[\d\.]{4,}", d.get_text())
            and re.search(r"\d{1,3}\.\d{3}\s*km", d.get_text(), re.I)
            and len(d.get_text()) < 800
        ]

    if not tarjetas:
        log.warning(f"  Página {pagina}: sin tarjetas.")
        return [], False

    vehiculos = []
    for t in tarjetas:
        txt = t.get_text(" ", strip=True)

        # Precio
        precio_m = re.search(r"\$\s*([\d\.]+)", txt)
        precio   = parsear_precio(precio_m.group(1)) if precio_m else None

        # Km
        km_m = re.search(r"([\d\.]+)\s*km", txt, re.I)
        km   = parsear_km(km_m.group(1).replace(".", "")) if km_m else None

        # Año
        ano = extraer_ano(txt)

        # Título
        titulo_tag = (
            t.find("h2") or t.find("h3") or t.find("h4")
            or t.find(class_=re.compile(r"title|name|model|heading", re.I))
        )
        titulo = titulo_tag.get_text(" ", strip=True) if titulo_tag else txt[:100]
        titulo = re.sub(r"\s+", " ", titulo).strip()

        # Si el título está vacío, construir desde texto
        if not titulo or len(titulo) < 5:
            titulo = txt[:80].strip()

        partes = titulo.split(None, 1)
        marca  = partes[0].upper() if partes else "DESCONOCIDA"

        # Combustible
        comb = ""
        for palabra in ["Híbrido", "Hibrido", "Eléctrico", "Electrico",
                        "Gasolina", "Bencina", "Diésel", "Diesel", "GNC", "GLP"]:
            if re.search(rf"\b{palabra}\b", txt, re.I):
                comb = palabra.capitalize()
                break
        # también buscar ícono/clase
        for cls in ["combustible", "fuel"]:
            tag = t.find(class_=re.compile(cls, re.I))
            if tag:
                comb = tag.get_text(" ", strip=True) or comb
                break

        # Transmisión
        trans = "Automática" if re.search(r"\b(at|cvt|dct|dsg|automátic|automatic)\b", titulo, re.I) \
                else ("Mecánica" if re.search(r"\b(mt|manual)\b", titulo, re.I) else "")

        vehiculos.append({
            "titulo":    titulo,
            "marca":     marca,
            "ano":       ano,
            "km":        km,
            "precio":    precio,
            "combustible": comb,
            "transmision": trans,
            "texto_raw": txt[:200],
        })

    # ¿Hay página siguiente?
    hay_sig = (
        bool(soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I)))
        or bool(soup.find(attrs={"aria-label": re.compile(r"next|siguiente", re.I)}))
        or len(tarjetas) >= PAGE_SIZE * 0.8   # si vino casi llena, intentar siguiente
    )

    log.info(f"  Página {pagina:02d}: {len(vehiculos)} vehículos")
    return vehiculos, hay_sig


def scrape_todo() -> list[dict]:
    session = requests.Session()
    todos   = []
    pagina  = 1
    log.info(f"▶ Scraping {BASE_URL}")
    while pagina <= MAX_PAGINAS:
        items, hay_sig = scrape_pagina(session, pagina)
        todos.extend(items)
        if not items or not hay_sig:
            break
        pagina += 1
        time.sleep(DELAY_SEG)
    log.info(f"✓ Total extraídos: {len(todos)}")
    return todos

# ── Análisis ──────────────────────────────────────────────────────────────────

def clave_version(v: dict) -> str:
    """Clave de agrupación: título normalizado (sin año, km, precio)."""
    t = v["titulo"].upper()
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = re.sub(r"[\d\.]+\s*KM", "", t, flags=re.I)
    t = re.sub(r"\$[\d\.]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def analizar_combustible(veh: list[dict]) -> list[dict]:
    out = []
    for v in veh:
        c = (v["combustible"] or "").lower()
        if any(x in c for x in ("híbrido", "hibrido", "eléctrico", "electrico")):
            continue
        titulo_l = (" " + v["titulo"].lower() + " ")
        for kw in MHEV_KEYWORDS:
            if kw in titulo_l:
                out.append({
                    "vehiculo":   v["titulo"],
                    "km":         v["km"],
                    "precio":     v["precio"],
                    "comb_actual": v["combustible"] or "No especificado",
                    "deberia":    "Híbrido",
                    "detalle":    f'"{kw.upper().strip()}" indica tecnología mild-hybrid o híbrida.',
                    "sev":        "ALTO",
                })
                break
    return out


def analizar_km_precio(veh: list[dict]) -> list[dict]:
    grupos: dict[str, list] = defaultdict(list)
    for v in veh:
        if None in (v["km"], v["precio"], v["ano"]):
            continue
        grupos[f"{clave_version(v)}|{v['ano']}"].append(v)

    out = []
    for clave, items in grupos.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda x: x["km"])
        secuencia = []
        hay_anomalia = False
        for i, item in enumerate(items):
            sube = i > 0 and item["precio"] > items[i-1]["precio"]
            if sube:
                hay_anomalia = True
            secuencia.append({"km": item["km"], "precio": item["precio"], "sube": sube})

        if not hay_anomalia:
            continue

        diff_max = max(
            items[i]["precio"] - items[i-1]["precio"]
            for i in range(1, len(items))
            if items[i]["precio"] > items[i-1]["precio"]
        )
        sev = "ALTO" if diff_max >= 1_500_000 else ("MEDIO" if diff_max >= 600_000 else "BAJO")
        out.append({
            "vehiculo":  items[0]["titulo"],
            "secuencia": secuencia,
            "sev":       sev,
        })

    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out


def analizar_ano_precio(veh: list[dict]) -> list[dict]:
    grupos: dict[str, list] = defaultdict(list)
    for v in veh:
        if None in (v["km"], v["precio"], v["ano"]):
            continue
        grupos[clave_version(v)].append(v)

    out = []
    for clave, items in grupos.items():
        anos = sorted(set(v["ano"] for v in items))
        if len(anos) < 2:
            continue
        for a1, a2 in combinations(anos, 2):
            g1 = sorted([v for v in items if v["ano"] == a1], key=lambda x: x["precio"])
            g2 = sorted([v for v in items if v["ano"] == a2], key=lambda x: x["precio"])
            r1 = g1[len(g1)//2]
            r2 = g2[len(g2)//2]
            if r2["precio"] >= r1["precio"]:
                continue
            diff_p  = r1["precio"] - r2["precio"]
            diff_km = r2["km"] - r1["km"]
            if diff_km > 90_000:
                sev = "BAJO"
            elif diff_p >= 1_200_000:
                sev = "MEDIO"
            else:
                sev = "BAJO"
            out.append({
                "modelo":       r1["titulo"],
                "ano_ant":      a1, "km_ant": r1["km"], "precio_ant": r1["precio"],
                "ano_nuevo":    a2, "km_nuevo": r2["km"], "precio_nuevo": r2["precio"],
                "diff_precio":  diff_p,
                "diff_km":      diff_km,
                "sev":          sev,
            })

    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out[:12]


JERARQUIAS = [
    (r"prado.*super\s*lujo",        r"prado.*vx-?l",             "Prado: SUPER LUJO < VX-L LIMITED"),
    (r"landtrek.*active.*150",       r"landtrek.*action.*180",    "Landtrek: ACTIVE 150HP < ACTION 180HP"),
    (r"sportage.*ex.*2wd",           r"sportage.*ex.*awd",        "Sportage EX: 2WD < AWD"),
    (r"x-?trail.*\bsense\b",         r"x-?trail.*exclusive",      "X-Trail: SENSE < EXCLUSIVE"),
    (r"x-?trail.*\bsense\b",         r"x-?trail.*advance",        "X-Trail: SENSE < ADVANCE"),
    (r"rav4.*\ble\b.*\bmt\b",        r"rav4.*\ble\b.*\bcvt\b",    "RAV4: MT < CVT"),
    (r"rav4.*\ble\b",                r"rav4.*\bvx\b",             "RAV4: LE < VX"),
    (r"tucson.*\bgl\b",              r"tucson.*\bgls\b",          "Tucson: GL < GLS"),
    (r"tucson.*\bgls\b",             r"tucson.*n-?line",          "Tucson: GLS < N-LINE"),
    (r"\bmg\b.*\bstd\b",             r"\bmg\b.*\blux\b",          "MG: STD < LUX"),
    (r"tiggo.*\bgls\b",              r"tiggo.*\bglx\b",           "Tiggo: GLS < GLX"),
    (r"chery.*\bgls\b",              r"chery.*\bglx\b",           "Chery: GLS < GLX"),
    (r"2wd",                         r"4wd|4x4|awd",              "Tracción: 2WD < 4WD/AWD"),
]

def analizar_version_precio(veh: list[dict]) -> list[dict]:
    grupos: dict[str, list] = defaultdict(list)
    for v in veh:
        if None in (v["precio"], v["ano"]):
            continue
        grupos[f"{v['marca']}|{v['ano']}"].append(v)

    out = []
    seen = set()
    for clave, items in grupos.items():
        for pat_inf, pat_sup, desc in JERARQUIAS:
            inferiores = [v for v in items
                          if re.search(pat_inf, v["titulo"], re.I)
                          and not re.search(pat_sup, v["titulo"], re.I)]
            superiores = [v for v in items
                          if re.search(pat_sup, v["titulo"], re.I)]
            if not inferiores or not superiores:
                continue
            mejor_inf = max(inferiores, key=lambda x: x["precio"])
            mejor_sup = max(superiores, key=lambda x: x["precio"])
            if mejor_inf["precio"] <= mejor_sup["precio"]:
                continue
            key_dedup = f"{mejor_inf['titulo']}|{mejor_sup['titulo']}"
            if key_dedup in seen:
                continue
            seen.add(key_dedup)
            diff = mejor_inf["precio"] - mejor_sup["precio"]
            diff_km = abs((mejor_inf["km"] or 0) - (mejor_sup["km"] or 0))
            out.append({
                "modelo":      f"{clave.split('|')[0]} {clave.split('|')[1]}",
                "desc":        desc,
                "ver_inf":     mejor_inf["titulo"],
                "km_inf":      mejor_inf["km"],
                "precio_inf":  mejor_inf["precio"],
                "ver_sup":     mejor_sup["titulo"],
                "km_sup":      mejor_sup["km"],
                "precio_sup":  mejor_sup["precio"],
                "diff":        diff,
                "diff_km":     diff_km,
                "sev":         "ALTO" if diff >= 1_000_000 else "MEDIO",
            })

    out.sort(key=lambda x: {"ALTO":0,"MEDIO":1,"BAJO":2}[x["sev"]])
    return out


def estadisticas(veh: list[dict]) -> dict:
    precios = [v["precio"] for v in veh if v["precio"]]
    kms     = [v["km"]     for v in veh if v["km"]]

    marcas: dict[str,int] = defaultdict(int)
    combs:  dict[str,int] = defaultdict(int)
    anos:   dict[int,int] = defaultdict(int)
    trans:  dict[str,int] = defaultdict(int)

    for v in veh:
        marcas[v["marca"]] += 1
        combs[v["combustible"] or "No especificado"] += 1
        if v["ano"]:
            anos[v["ano"]] += 1
        if v["transmision"]:
            trans[v["transmision"]] += 1

    return {
        "total":       len(veh),
        "con_precio":  len(precios),
        "precio_min":  min(precios) if precios else 0,
        "precio_max":  max(precios) if precios else 0,
        "precio_prom": int(sum(precios)/len(precios)) if precios else 0,
        "km_prom":     int(sum(kms)/len(kms)) if kms else 0,
        "top_marcas":  sorted(marcas.items(), key=lambda x:-x[1])[:10],
        "combustible": dict(sorted(combs.items(), key=lambda x:-x[1])),
        "anos":        dict(sorted(anos.items(), key=lambda x:-x[0])[:14]),
        "transmision": dict(trans),
    }

# ── Generación HTML ───────────────────────────────────────────────────────────

def fp(n): return f"${n:,.0f}".replace(",",".") if n else "—"
def fk(n): return f"{n:,.0f} km".replace(",",".") if n else "—"
def badge(s):
    c = {"ALTO":"#dc2626","MEDIO":"#d97706","BAJO":"#16a34a"}.get(s,"#6b7280")
    return f'<span class="badge" style="background:{c}">{s}</span>'


def generar_html(veh, comb_err, km_p, ano_p, ver_p, stats, fecha_gen, hora_gen) -> str:

    # ── Tabla combustible ──────────────────────────────────────────────────────
    filas_comb = ""
    for h in comb_err:
        filas_comb += f"""<tr>
          <td><strong>{h['vehiculo']}</strong></td>
          <td>{fk(h['km'])}</td><td>{fp(h['precio'])}</td>
          <td>{h['comb_actual']}</td><td>🔋 Híbrido</td>
          <td class="note">{h['detalle']}</td>
          <td>{badge(h['sev'])}</td></tr>"""
    if not filas_comb:
        filas_comb = '<tr><td colspan="7" class="empty">Sin inconsistencias detectadas ✓</td></tr>'

    # ── Tabla km/precio ────────────────────────────────────────────────────────
    filas_km = ""
    for h in km_p[:15]:
        seq = ""
        for p in h["secuencia"]:
            cls = ' class="sube"' if p["sube"] else ""
            icon = " ⚠ SUBE" if p["sube"] else ""
            seq += f'<span{cls}>{fk(p["km"])} {fp(p["precio"])}{icon}</span> '
        filas_km += f"""<tr>
          <td><strong>{h['vehiculo']}</strong></td>
          <td class="seq">{seq}</td>
          <td>{badge(h['sev'])}</td></tr>"""
    if not filas_km:
        filas_km = '<tr><td colspan="3" class="empty">Sin inconsistencias detectadas ✓</td></tr>'

    # ── Tabla año/precio ───────────────────────────────────────────────────────
    filas_ano = ""
    for h in ano_p:
        filas_ano += f"""<tr>
          <td>{h['modelo']}</td>
          <td>{h['ano_ant']}<br><small>{fk(h['km_ant'])}<br>{fp(h['precio_ant'])}</small></td>
          <td>{h['ano_nuevo']}<br><small>{fk(h['km_nuevo'])}<br>{fp(h['precio_nuevo'])}</small></td>
          <td class="note">{h['ano_nuevo']} más barato en {fp(h['diff_precio'])} con {fk(h['diff_km'])} más.</td>
          <td>{badge(h['sev'])}</td></tr>"""
    if not filas_ano:
        filas_ano = '<tr><td colspan="5" class="empty">Sin inconsistencias detectadas ✓</td></tr>'

    # ── Tabla versión/precio ───────────────────────────────────────────────────
    filas_ver = ""
    for h in ver_p:
        filas_ver += f"""<tr>
          <td>{h['modelo']}<br><small style="color:#6b7280">{h['desc']}</small></td>
          <td><span class="tag-inf">INFERIOR</span><br><small>{h['ver_inf']}<br>{fk(h['km_inf'])} · {fp(h['precio_inf'])}</small></td>
          <td><span class="tag-sup">SUPERIOR</span><br><small>{h['ver_sup']}<br>{fk(h['km_sup'])} · {fp(h['precio_sup'])}</small></td>
          <td class="note">Versión inferior {fp(h['diff'])} más cara con {fk(h['diff_km'])} de diferencia.</td>
          <td>{badge(h['sev'])}</td></tr>"""
    if not filas_ver:
        filas_ver = '<tr><td colspan="5" class="empty">Sin inconsistencias detectadas ✓</td></tr>'

    # ── Stats ──────────────────────────────────────────────────────────────────
    marcas_html = "".join(
        f"<tr><td>{m}</td><td><strong>{c}</strong></td></tr>"
        for m, c in stats["top_marcas"]
    )
    total_c = sum(stats["combustible"].values()) or 1
    comb_html = "".join(
        f"<tr><td>{c}</td><td>{n} <span class='pct'>({n*100//total_c}%)</span></td></tr>"
        for c, n in list(stats["combustible"].items())[:7]
    )
    anos_html = "".join(
        f"<tr><td>{a}</td><td><strong>{n}</strong></td></tr>"
        for a, n in list(stats["anos"].items())
    )
    trans_total = sum(stats["transmision"].values()) or 1
    trans_html = "".join(
        f"<tr><td>{t}</td><td>{n} <span class='pct'>({n*100//trans_total}%)</span></td></tr>"
        for t, n in stats["transmision"].items()
    )

    # Resumen
    n_comb_a = sum(1 for h in comb_err   if h["sev"]=="ALTO")
    n_km_a   = sum(1 for h in km_p       if h["sev"]=="ALTO")
    n_ver_a  = sum(1 for h in ver_p      if h["sev"]=="ALTO")
    pct_precio = stats['con_precio']*100//stats['total'] if stats['total'] else 0

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auditoría BF Usados — {fecha_gen}</title>
<style>
  :root{{
    --bg:#f8fafc;--card:#fff;--border:#e2e8f0;--text:#0f172a;
    --muted:#64748b;--accent:#2563eb;--alto:#dc2626;--medio:#d97706;--bajo:#16a34a;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}}
  /* Header */
  .hdr{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);
        color:#fff;padding:28px 40px 20px}}
  .hdr h1{{font-size:20px;font-weight:800;letter-spacing:-.3px}}
  .hdr .sub{{color:#94a3b8;font-size:12px;margin-top:3px}}
  .hdr .meta{{display:flex;gap:20px;margin-top:14px;flex-wrap:wrap;font-size:12px}}
  .hdr .meta span{{color:#cbd5e1}} .hdr .meta strong{{color:#fff}}
  .update-badge{{display:inline-flex;align-items:center;gap:6px;
    background:rgba(37,99,235,.25);border:1px solid rgba(37,99,235,.4);
    color:#93c5fd;border-radius:20px;padding:3px 10px;font-size:11px;margin-top:10px}}
  .update-badge::before{{content:"🔄";font-size:10px}}
  /* KPIs */
  .kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
         gap:10px;padding:20px 40px;background:#fff;border-bottom:1px solid var(--border)}}
  .kpi{{background:#eff6ff;border-radius:8px;padding:12px;text-align:center}}
  .kpi .num{{font-size:22px;font-weight:800;color:var(--accent)}}
  .kpi .lbl{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
  /* Layout */
  .content{{padding:28px 40px;max-width:1400px;margin:0 auto}}
  .section{{margin-bottom:36px}}
  .sec-title{{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;
              border-bottom:2px solid var(--border);padding-bottom:8px;margin-bottom:14px}}
  .sec-title .cnt{{margin-left:auto;background:#e2e8f0;border-radius:20px;
                   padding:1px 10px;font-size:11px;color:#475569;font-weight:600}}
  .hint{{font-size:11px;color:var(--muted);margin-bottom:10px}}
  /* Tables */
  table{{width:100%;border-collapse:collapse}}
  th{{background:#f1f5f9;text-align:left;padding:7px 10px;font-size:11px;
      color:var(--muted);text-transform:uppercase;letter-spacing:.4px;
      border-bottom:1px solid var(--border)}}
  td{{padding:9px 10px;border-bottom:1px solid var(--border);
      vertical-align:top;font-size:12px}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#fafbff}}
  td.note{{font-size:11px;color:#475569;max-width:260px}}
  td.seq span{{display:inline-block;margin-right:10px;margin-bottom:3px;
               font-size:11px;background:#f1f5f9;border-radius:4px;padding:2px 6px}}
  td.seq span.sube{{background:#fef2f2;color:var(--alto);font-weight:600}}
  td.empty{{text-align:center;color:var(--muted);padding:20px}}
  .badge{{display:inline-block;color:#fff;padding:2px 8px;border-radius:4px;
          font-size:10px;font-weight:700;white-space:nowrap}}
  .tag-inf{{display:inline-block;background:#fef2f2;color:var(--alto);
            font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px}}
  .tag-sup{{display:inline-block;background:#f0fdf4;color:var(--bajo);
            font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px}}
  /* Stats grid */
  .stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}}
  .stat-card{{background:var(--card);border:1px solid var(--border);
              border-radius:10px;padding:14px}}
  .stat-card h4{{font-size:11px;text-transform:uppercase;letter-spacing:.4px;
                 color:var(--muted);margin-bottom:8px}}
  .stat-card td{{padding:3px 4px;border:none;font-size:12px}}
  .pct{{color:var(--muted);font-size:11px}}
  /* Resumen */
  .res-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
  .res-card{{border-radius:10px;padding:14px;border-left:4px solid}}
  .res-card.r{{background:#fff5f5;border-color:var(--alto)}}
  .res-card.o{{background:#fffbeb;border-color:var(--medio)}}
  .res-card.y{{background:#fefce8;border-color:#ca8a04}}
  .res-card.g{{background:#f0fdf4;border-color:var(--bajo)}}
  .res-card h4{{font-size:11px;font-weight:700;text-transform:uppercase;margin-bottom:7px}}
  .res-card ul{{padding-left:14px;font-size:11px;line-height:1.9}}
  /* Footer */
  footer{{text-align:center;padding:20px;color:var(--muted);
          font-size:11px;border-top:1px solid var(--border)}}
  @media(max-width:680px){{
    .hdr,.kpis,.content{{padding-left:16px;padding-right:16px}}
    .kpi .num{{font-size:18px}}
  }}
</style>
</head>
<body>

<div class="hdr">
  <h1>INFORME ANÁLISIS — AUTOS USADOS BRUNO FRITSCH</h1>
  <div class="sub">Revisión de consistencia: precio vs año/km y equipamiento declarado</div>
  <div class="meta">
    <span>Generado: <strong>{fecha_gen} {hora_gen}</strong></span>
    <span>Fuente: <strong>brunofritsch.cl/autos-usados</strong></span>
    <span>Total: <strong>{stats['total']} vehículos</strong></span>
  </div>
  <div class="update-badge">Se actualiza automáticamente cada lunes</div>
</div>

<div class="kpis">
  <div class="kpi"><div class="num">{stats['total']}</div><div class="lbl">Autos totales</div></div>
  <div class="kpi"><div class="num">{stats['con_precio']}</div><div class="lbl">Con precio ({pct_precio}%)</div></div>
  <div class="kpi"><div class="num">{len(comb_err)}</div><div class="lbl">Combustible incorrecto</div></div>
  <div class="kpi"><div class="num">{len(km_p)}</div><div class="lbl">Km vs precio</div></div>
  <div class="kpi"><div class="num">{len(ano_p)}</div><div class="lbl">Año vs precio</div></div>
  <div class="kpi"><div class="num">{len(ver_p)}</div><div class="lbl">Versión vs precio</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_min'])}</div><div class="lbl">Precio mínimo</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_max'])}</div><div class="lbl">Precio máximo</div></div>
  <div class="kpi"><div class="num">{fp(stats['precio_prom'])}</div><div class="lbl">Precio promedio</div></div>
  <div class="kpi"><div class="num">{fk(stats['km_prom'])}</div><div class="lbl">Km promedio</div></div>
</div>

<div class="content">

<div class="section">
  <div class="sec-title">⚠️ Combustible mal catalogado
    <span class="cnt">{len(comb_err)} casos</span></div>
  <table><thead><tr>
    <th>Vehículo</th><th>Km</th><th>Precio</th>
    <th>Actual</th><th>Debería ser</th><th>Detalle</th><th>Sev.</th>
  </tr></thead><tbody>{filas_comb}</tbody></table>
</div>

<div class="section">
  <div class="sec-title">📈 Mismo modelo/año/versión — Más km, precio mayor
    <span class="cnt">{len(km_p)} grupos · top 15</span></div>
  <p class="hint">Criterio: misma versión y año exactos → el precio debería bajar (o mantenerse) al subir los km.</p>
  <table><thead><tr>
    <th>Vehículo</th><th>Secuencia km → precio (⚠ sube cuando no debería)</th><th>Sev.</th>
  </tr></thead><tbody>{filas_km}</tbody></table>
</div>

<div class="section">
  <div class="sec-title">🟡 Año más nuevo con precio menor (misma versión)
    <span class="cnt">{len(ano_p)} casos</span></div>
  <table><thead><tr>
    <th>Modelo / Versión</th><th>Año antiguo</th><th>Año nuevo</th><th>Nota</th><th>Sev.</th>
  </tr></thead><tbody>{filas_ano}</tbody></table>
</div>

<div class="section">
  <div class="sec-title">🏆 Versión inferior más cara que versión superior
    <span class="cnt">{len(ver_p)} casos</span></div>
  <p class="hint">Criterio: versión de menor equipamiento/potencia/tracción aparece más cara, sin que los km justifiquen la brecha.</p>
  <table><thead><tr>
    <th>Modelo / Año</th><th>Versión inferior</th><th>Versión superior</th><th>Análisis</th><th>Sev.</th>
  </tr></thead><tbody>{filas_ver}</tbody></table>
</div>

<div class="section">
  <div class="sec-title">📊 Estadísticas del inventario</div>
  <div class="stats-grid">
    <div class="stat-card"><h4>Top 10 marcas</h4>
      <table><tbody>{marcas_html}</tbody></table></div>
    <div class="stat-card"><h4>Combustible</h4>
      <table><tbody>{comb_html}</tbody></table></div>
    <div class="stat-card"><h4>Transmisión</h4>
      <table><tbody>{trans_html}</tbody></table></div>
    <div class="stat-card"><h4>Por año</h4>
      <table><tbody>{anos_html}</tbody></table></div>
  </div>
</div>

<div class="section">
  <div class="sec-title">📋 Resumen y recomendaciones</div>
  <div class="res-grid">
    <div class="res-card r"><h4>🔴 Prioridad Alta</h4><ul>
      <li>Combustible incorrecto (ALTO): <strong>{n_comb_a}</strong> casos</li>
      <li>Km vs precio (ALTO): <strong>{n_km_a}</strong> grupos</li>
      <li>Versión inferior más cara (ALTO): <strong>{n_ver_a}</strong> casos</li>
    </ul></div>
    <div class="res-card o"><h4>🟠 Prioridad Media</h4><ul>
      <li>Km vs precio (MEDIO/BAJO): <strong>{len(km_p)-n_km_a}</strong> grupos adicionales</li>
      <li>Año vs precio revisable: <strong>{len(ano_p)}</strong> casos</li>
    </ul></div>
    <div class="res-card y"><h4>🟡 Revisar etiquetas</h4><ul>
      <li>Sufijos MHEV, B4, B5, B6, E-TSI → catalogar como Híbrido</li>
      <li>Verificar precios de versiones AWD/4WD vs 2WD</li>
      <li>Revisar tops de gama que aparecen más baratos que base</li>
    </ul></div>
    <div class="res-card g"><h4>✅ Cobertura</h4><ul>
      <li><strong>{stats['total']}</strong> vehículos procesados</li>
      <li><strong>{pct_precio}%</strong> con precio visible</li>
      <li>Actualización: cada lunes automático</li>
    </ul></div>
  </div>
</div>

</div>

<footer>
  Informe generado automáticamente · Bruno Fritsch Autos Usados ·
  brunofritsch.cl · {fecha_gen} {hora_gen} · {stats['total']} vehículos
</footer>
</body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.now()
    fecha_gen = now.strftime("%d/%m/%Y")
    hora_gen  = now.strftime("%H:%M")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 50)
    log.info("  AUDITORÍA BF USADOS")
    log.info(f"  {fecha_gen} {hora_gen}")
    log.info("=" * 50)

    veh = scrape_todo()

    if not veh:
        log.error("No se extrajeron vehículos. Abortando.")
        sys.exit(1)

    comb_err = analizar_combustible(veh)
    km_p     = analizar_km_precio(veh)
    ano_p    = analizar_ano_precio(veh)
    ver_p    = analizar_version_precio(veh)
    stats    = estadisticas(veh)

    html = generar_html(veh, comb_err, km_p, ano_p, ver_p, stats, fecha_gen, hora_gen)
    OUTPUT_FILE.write_text(html, encoding="utf-8")

    # Guardar datos crudos para referencia
    DATA_FILE.write_text(
        json.dumps({"generado": now.isoformat(), "total": len(veh), "vehiculos": veh[:50]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    log.info(f"✓ Informe → {OUTPUT_FILE}")
    log.info(f"✓ Vehículos: {stats['total']} | Combustible: {len(comb_err)} | "
             f"Km/precio: {len(km_p)} | Año/precio: {len(ano_p)} | Versión: {len(ver_p)}")


if __name__ == "__main__":
    main()
