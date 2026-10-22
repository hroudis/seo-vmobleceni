#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEO popisy KATEGORIÍ pro vmobleceni.cz
======================================
Pro každou kategorii z kategorie.csv vygeneruje:
  - perex (krátký popis nad produkty),
  - delší popis pod produkty s nadpisy, odrážkami a VNITŘNÍMI ODKAZY
    na nadřazenou, podřízené a příbuzné kategorie,
  - časté dotazy (FAQ),
  - SEO titulek a meta description kategorie.

Fakta (počet produktů, materiály, velikosti, značky, typy produktů) se berou
z produktových feedů (FEED_URL = Mergado, FEED_URL_2 = Shoptet github.xml),
takže texty popisují skutečný sortiment, ne obecné fráze.

Odkazy smí vést JEN na URL z kategorie.csv — skript každý odkaz ověří,
takže v textu nikdy nevznikne neexistující adresa.

Hotové kategorie se ukládají do state-kategorie.json -> při dalším běhu
se generují jen chybějící. Přegenerování: proměnná REGENERATE
("all" = vše, nebo seznam URL oddělených čárkou).

Výstupy (složka output/):
  kategorie-popisy.html  — pomocná stránka s tlačítky Kopírovat pro vložení do Shoptetu
  kategorie-popisy.csv   — totéž v tabulce
"""

import os
import re
import csv
import json
import html
import time
import threading
import collections
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from generate_meta import (fetch_feed, strip_html, FOREIGN_SCRIPT, fix_title,
                           log, PermanentError, API_URL)

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FEED_URL = os.environ.get("FEED_URL", "").strip()
FEED_URL_2 = os.environ.get("FEED_URL_2", "").strip()
MODEL = os.environ.get("MODEL", "claude-haiku-5-5").strip()
CONCURRENCY = int(os.environ.get("CONCURRENCY") or "4")
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT") or "1000")
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
REGENERATE = os.environ.get("REGENERATE", "").strip()

CATS_FILE = "kategorie-shoptet.csv"   # export z Shoptetu: Produkty > Kategorie > Export
STATE_FILE = "state-kategorie.json"
OUTPUT_DIR = "output"
SITE = "https://www.vmobleceni.cz"

# kategorie, pro které se popis negeneruje (akční/sezónní/pomocné)
SKIP_URL_PARTS = ("black-friday", "slevy-az-80", "velkoobchod", "doplnky-nerozdeleno", "ostatni-2")


# ---------------------------------------------------------------- strom kategorií
def read_shoptet_export():
    """Načte export kategorií ze Shoptetu (CSV, středník, windows-1250)."""
    raw = open(CATS_FILE, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1250")
    reader = csv.DictReader(__import__("io").StringIO(text, newline=""), delimiter=";")
    return reader.fieldnames, list(reader)


def load_categories():
    _, rows = read_shoptet_export()
    by_slug = {r["url"]: r for r in rows}

    def visible(r):
        # kategorie je vidět, jen když je viditelná ona i všichni předci
        while r:
            if r.get("visible") != "1":
                return False
            r = by_slug.get(r.get("parentUrl") or "")
        return True

    cats = []
    for r in rows:
        if not visible(r):
            continue
        cats.append({
            "id": r["id"],
            "slug": r["url"],
            "url": f"{SITE}/{r['url'].strip('/')}/",
            "rodic_url": f"{SITE}/{r['parentUrl'].strip('/')}/" if r.get("parentUrl") else "",
            "nazev": r["title"].strip(),                         # název v administraci (= cesta ve feedech)
            "nazev_seo": (r.get("linkText") or r["title"]).strip(),  # název v menu
            "puvodni_horni": r.get("topDescription", ""),
        })
    by_url = {c["url"]: c for c in cats}
    for c in cats:
        if c["rodic_url"] not in by_url:
            c["rodic_url"] = ""
    for c in cats:
        c["children"] = [x["url"] for x in cats if x["rodic_url"] == c["url"]]
        names, seo, cur = [], [], c
        while cur:
            names.insert(0, cur["nazev"])
            seo.insert(0, cur["nazev_seo"])
            cur = by_url.get(cur["rodic_url"])
        c["names"] = names
        c["cesta"] = " > ".join(seo)
        c["uroven"] = len(names)
    return cats, by_url


def tok(name):
    """Množina slov názvu — „Oblečení dámské" == „Dámské oblečení"."""
    s = re.sub(r"[^\w]+", " ", name.lower())
    return frozenset(w for w in s.split() if w)


# ---------------------------------------------------------------- statistiky z feedů
SIZE_RE = re.compile(r"\(([^()]{1,30})\)")
CODE_LIKE = re.compile(r"\b[A-Z]{1,6}[0-9][A-Z0-9/\-_.]{2,}\b")


def clean_product_name(n):
    n = CODE_LIKE.sub("", n)
    n = re.sub(r"\s+", " ", n).strip(" -,")
    return n


def product_type(n):
    """První 2–3 slova názvu = typ produktu („Šaty dlouhé letní")."""
    n = SIZE_RE.sub("", n)
    words = [w for w in re.split(r"\s+", n) if w]
    return " ".join(words[:3]).lower()


def normalize_material(m):
    m = strip_html(m or "").lower()
    m = re.sub(r"^\d+\.\s*", "", m)
    m = re.sub(r"\s+", " ", m).strip(" ,.")
    return m[:60]


def iter_feed_products(data, flat):
    """Vrací (názvy kategorií jako seznam cest, název, značka, materiál)."""
    for ev, el in ET.iterparse(__import__("io").BytesIO(data)):
        if el.tag != "SHOPITEM":
            continue
        if not flat and (el.findtext("VISIBILITY") or "").strip() == "hidden":
            el.clear()
            continue
        name = (el.findtext("NAME") or "").strip()
        man = (el.findtext("MANUFACTURER") or "").strip()
        paths = []
        if flat:
            c = (el.findtext("CATEGORY") or "").strip()
            if c:
                paths.append([p.strip() for p in c.split("|") if p.strip()])
        else:
            for x in el.findall("CATEGORIES/CATEGORY"):
                if x.text:
                    paths.append([p.strip() for p in x.text.split(">") if p.strip()])
        material = ""
        for ip in el.iter("INFORMATION_PARAMETER"):
            nm = ip.findtext("NAME") or ""
            if re.search(r"slož|sloz", nm, re.I):
                material = normalize_material(ip.findtext("VALUE"))
                break
        el.clear()
        if name and paths:
            yield paths, name, man, material


def collect_stats(cats, feeds):
    index = {}
    for c in cats:
        index[tuple(tok(n) for n in c["names"])] = c["url"]
    stats = {c["url"]: {"n": 0, "mat": collections.Counter(), "man": collections.Counter(),
                        "size": collections.Counter(), "type": collections.Counter(),
                        "examples": []} for c in cats}
    by_url = {c["url"]: c for c in cats}
    seen_names = set()
    for data, flat in feeds:
        for paths, name, man, mat in iter_feed_products(data, flat):
            key = name.lower()
            if flat and key in seen_names:
                continue  # plochý feed = varianta na řádek -> produkt jen jednou
            seen_names.add(key)
            hit = set()
            for p in paths:
                tp = [tok(x) for x in p]
                for L in range(len(tp), 0, -1):
                    u = index.get(tuple(tp[:L]))
                    if u:
                        hit.add(u)
                        break
            # započítat i do všech nadřazených kategorií
            full = set()
            for u in hit:
                cur = by_url.get(u)
                while cur:
                    full.add(cur["url"])
                    cur = by_url.get(cur["rodic_url"])
            for u in full:
                s = stats[u]
                s["n"] += 1
                if mat:
                    s["mat"][mat] += 1
                if man and man.upper() not in ("ITALSKÁ MÓDA", "ITALSKá MóDA"):
                    s["man"][man] += 1
                for sz in SIZE_RE.findall(name):
                    s["size"][sz.strip()] += 1
                s["type"][product_type(name)] += 1
                if len(s["examples"]) < 8 and u in hit:
                    s["examples"].append(clean_product_name(name))
    return stats


# ---------------------------------------------------------------- plán odkazů
GENDER = {"dámské", "dámská", "dámský", "dívčí", "chlapecké", "chlapecká", "chlapecký",
          "kojenecké", "kojenecká", "kojenecký", "pánské", "pánská", "pánský",
          "dětské", "dětská", "dětský"}


def main_noun(name):
    words = [w for w in re.split(r"[\s,\-]+", name.lower()) if w and w not in GENDER]
    return words[0][:5] if words else ""


def link_plan(c, by_url, stats, cats):
    """Povolené odkazy pro kategorii: rodič, děti, sourozenci, příbuzné z jiných větví."""
    links = []

    def add(u, role):
        if u and u != c["url"] and u in by_url and u not in [x[0] for x in links] \
                and not any(p in u for p in SKIP_URL_PARTS):
            links.append((u, by_url[u]["nazev_seo"], role))

    add(c["rodic_url"], "nadřazená kategorie")
    kids = sorted(c["children"], key=lambda u: -stats[u]["n"])
    for u in kids[:10]:
        add(u, "podkategorie")
    if c["rodic_url"]:
        sib = [u for u in by_url[c["rodic_url"]]["children"] if u != c["url"]]
        for u in sorted(sib, key=lambda u: -stats[u]["n"])[:5]:
            add(u, "příbuzná kategorie")
    noun = main_noun(c["nazev_seo"])
    if noun:
        rel = [x for x in cats if x["url"] != c["url"] and main_noun(x["nazev_seo"]) == noun
               and x["names"][0] != c["names"][0]]
        for x in sorted(rel, key=lambda x: -stats[x["url"]]["n"])[:3]:
            add(x["url"], "stejný typ pro jinou skupinu")
    return links


# ---------------------------------------------------------------- prompt a API
INSTRUCTION = """Jsi český e-commerce SEO copywriter pro eshop vmobleceni.cz (dámské, dětské, kojenecké a nadrozměrné oblečení, hodně italské módy).
Píšeš text KATEGORIE, který funguje pro Google/Seznam i pro AI vyhledávače (ChatGPT, Perplexity, Google AI Overviews):
- fakta první: co v kategorii zákazník najde, pro koho, z jakých materiálů, v jakých velikostech,
- konkrétní údaje z dodaných dat, žádné vymyšlené vlastnosti, ceny, slevy ani počty kusů,
- přirozená čeština, tykání ne — vykej, žádné superlativy typu „nejlepší na trhu",
- NEPOUŽÍVEJ uvozovky ani znaky < >.

ODKAZY: v textu POPIS použij 3 až 6 odkazů zápisem [[URL|text odkazu]].
- Smíš použít JEN URL ze seznamu POVOLENÉ ODKAZY, přesně jak jsou napsané.
- Pokud je v seznamu nadřazená kategorie, odkaz na ni použij.
- Text odkazu je přirozená fráze ve větě (např. „vyberte si i [[URL|letní šaty na ramínka]]"), ne „klikněte zde".
- Každé URL nejvýše jednou.

Odpověz PŘESNĚ v tomto formátu (oddělovače ponech):
===PEREX===
1–2 věty, max 280 znaků, bez odkazů. Co kategorie obsahuje a pro koho.
===POPIS===
180–300 slov. Struktura:
## nadpis (obsahuje název kategorie)
odstavec
## nadpis (např. jak vybrat velikost / materiál / k čemu nosit)
odstavec nebo odrážky začínající „- "
Odkazy [[URL|text]] rozmísti přirozeně v textu.
===FAQ===
Otázka: ...
Odpověď: ... (1–2 věty)
(celkem 3 dvojice; otázky, které zákazník skutečně hledá — velikosti, materiál, praní, výběr)
===SEO_TITLE===
max 60 znaků, klíčové slovo na začátku, končí „ | vmobleceni.cz"
===META===
140–155 znaků, věcný popis kategorie + jemná výzva k akci"""


def build_prompt(c, s, links, by_url):
    def top(counter, k):
        return ", ".join(f"{x}" for x, _ in counter.most_common(k)) or "neuvedeno"
    data = [
        f"KATEGORIE: {c['nazev_seo']}",
        f"Umístění v eshopu: {' > '.join(by_url[u]['nazev_seo'] for u in path_urls(c, by_url))}",
        f"Počet produktů v kategorii (přibližně): {s['n']}",
        f"Nejčastější typy produktů: {top(s['type'], 6)}",
        f"Nejčastější materiály: {top(s['mat'], 5)}",
        f"Nejčastější velikostní řady: {top(s['size'], 6)}",
        f"Značky / původ: {top(s['man'], 5)}",
        "Příklady produktů: " + ("; ".join(s["examples"][:6]) or "neuvedeno"),
        "",
        "POVOLENÉ ODKAZY:",
    ]
    for u, name, role in links:
        data.append(f"- {u}  ({name} — {role})")
    return INSTRUCTION + "\n\n" + "\n".join(data)


def path_urls(c, by_url):
    out, cur = [], c
    while cur:
        out.insert(0, cur["url"])
        cur = by_url.get(cur["rodic_url"])
    return out


def call_api(prompt, retries=3):
    payload = {"model": MODEL, "max_tokens": 3000,
               "messages": [{"role": "user", "content": prompt}]}
    if not re.search(r"-4-|-4$|-3-", MODEL):
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY,
               "anthropic-version": "2023-06-01"}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text"), data.get("stop_reason")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
            except Exception:
                msg = str(e)
            last = RuntimeError(f"HTTP {e.code}: {msg[:200]}")
            if e.code in (400, 401, 403, 404):
                raise PermanentError(str(last))
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last or RuntimeError("API selhalo")


# ---------------------------------------------------------------- zpracování odpovědi
SECTION_RE = re.compile(r"^===\s*([A-Z_]+)\s*===\s*$", re.M)
LINK_RE = re.compile(r"\[\[\s*([^|\]]+?)\s*\|\s*([^\]]+?)\s*\]\]")


def parse_sections(text):
    parts = SECTION_RE.split(text)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].strip()] = parts[i + 1].strip()
    return out


def md_to_html(text):
    """## nadpis, - odrážky, odstavce, [[url|text]] -> HTML."""
    def inline(t):
        t = html.escape(t, quote=False)
        return LINK_RE.sub(lambda m: f'<a href="{html.escape(m.group(1).replace(SITE, ""))}">{m.group(2)}</a>', t)
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            items.clear()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush()
        elif line.startswith("#"):
            flush()
            out.append("<h2>" + inline(line.lstrip("#").strip()) + "</h2>")
        elif line.startswith(("- ", "• ", "* ")):
            if para:
                flush()
            items.append(line[2:].strip())
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "\n".join(out)


def parse_faq(text):
    faq, q = [], None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(Otázka|Q)\s*:\s*(.+)$", line, re.I)
        if m:
            q = m.group(2).strip()
            continue
        m = re.match(r"^(Odpověď|A)\s*:\s*(.+)$", line, re.I)
        if m and q:
            faq.append((q, m.group(2).strip()))
            q = None
    return faq


def build_result(text, allowed):
    sec = parse_sections(text)
    perex = sec.get("PEREX", "").strip()
    popis = sec.get("POPIS", "").strip()
    faq = parse_faq(sec.get("FAQ", ""))
    title = fix_title(sec.get("SEO_TITLE", "").strip().strip('"'))
    meta = re.sub(r"\s+", " ", sec.get("META", "")).strip().strip('"')

    problems = []
    if not perex or not popis:
        problems.append("chybí perex nebo popis")
    if FOREIGN_SCRIPT.search(perex + popis + title + meta + str(faq)):
        problems.append("cizí písmo v textu")
    links = LINK_RE.findall(popis)
    bad = [u for u, _ in links if u not in allowed]
    if bad:
        # neplatný odkaz -> odkaz zrušit, text ponechat (nikdy nesmí vzniknout 404)
        popis = LINK_RE.sub(lambda m: m.group(0) if m.group(1) in allowed else m.group(2), popis)
    good = [u for u, _ in LINK_RE.findall(popis)]
    if len(set(good)) < 2:
        problems.append(f"málo platných odkazů ({len(set(good))})")
    words = len(re.sub(r"\[\[[^|]+\|", "", popis).split())
    if words < 120:
        problems.append(f"krátký popis ({words} slov)")
    if len(faq) < 2:
        problems.append("chybí FAQ")
    if len(meta) < 100:
        problems.append("krátká meta description")
    if problems:
        raise ValueError("; ".join(problems))

    faq_html = "<h2>Časté dotazy</h2>\n" + "\n".join(
        f"<h3>{html.escape(q, quote=False)}</h3>\n<p>{html.escape(a, quote=False)}</p>" for q, a in faq)
    faq_ld = json.dumps({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]},
        ensure_ascii=False)
    return {
        "perex_html": "<p>" + html.escape(perex, quote=False) + "</p>",
        "popis_html": md_to_html(popis) + "\n" + faq_html,
        "faq_jsonld": '<script type="application/ld+json">' + faq_ld + "</script>",
        "seo_title": title,
        "meta_description": meta,
        "links": sorted(set(good)),
        "words": words,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- výstupy
def write_import_file(done):
    """Soubor pro Shoptet: Produkty > Kategorie > Import.
    Obsahuje VŠECHNY řádky původního exportu beze změny (struktura, názvy, URL,
    viditelnost) — mění se jen 4 pole u kategorií, které mají nový text.
    Díky tomu import nemůže nic smazat ani přesunout."""
    fields, rows = read_shoptet_export()
    changed = kept_pobo = 0
    for r in rows:
        d = done.get(f"{SITE}/{r['url'].strip('/')}/")
        if not d:
            continue
        if "pobo" in (r.get("topDescription") or "").lower():
            kept_pobo += 1  # obsah z doplňku Pobo (page builder) nepřepisujeme
        else:
            r["topDescription"] = d["perex_html"]
        r["bottomDescription"] = d["popis_html"]
        r["metaTitle"] = d["seo_title"]
        r["metaDescription"] = d["meta_description"]
        changed += 1
    out = os.path.join(OUTPUT_DIR, "kategorie-import.csv")
    with open(out, "w", encoding="cp1250", errors="xmlcharrefreplace", newline="") as f:
        f.write(";".join(fields) + "\r\n")
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", quoting=csv.QUOTE_ALL,
                           lineterminator="\r\n", extrasaction="ignore")
        for r in rows:
            w.writerow({k: (v if v is not None else "") for k, v in r.items() if k is not None})
    log(f"Uloženo: {out} ({len(rows)} řádků, z toho {changed} s novým textem"
        + (f", u {kept_pobo} ponechán horní popis z Pobo" if kept_pobo else "") + ")")


def write_outputs(cats, done, stats):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows = [(c, done[c["url"]]) for c in cats if c["url"] in done]
    with open(os.path.join(OUTPUT_DIR, "kategorie-popisy.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["url", "kategorie", "cesta", "seo_title", "meta_description",
                    "popis_nahore_html", "popis_dole_html", "faq_jsonld"])
        for c, d in rows:
            w.writerow([c["url"], c["nazev_seo"], c["cesta"], d["seo_title"], d["meta_description"],
                        d["perex_html"], d["popis_html"], d["faq_jsonld"]])

    write_import_file(done)

    # ověření odkazů napříč celou sadou: kolik kategorií na danou kategorii odkazuje
    inbound = collections.Counter(u for _, d in rows for u in d["links"])
    cards = []
    for c, d in rows:
        fields = [("SEO titulek", d["seo_title"], False),
                  ("Meta description", d["meta_description"], False),
                  ("Popis nahoře (HTML)", d["perex_html"], True),
                  ("Popis dole (HTML)", d["popis_html"], True),
                  ("FAQ strukturovaná data (volitelné)", d["faq_jsonld"], False)]
        fh = []
        for label, val, preview in fields:
            fh.append(
                f'<div class="f"><div class="fh"><span>{html.escape(label)}'
                f'<em>{len(val)} znaků</em></span><button type="button" class="cp">Kopírovat</button></div>'
                f'<textarea readonly rows="{3 if len(val) < 200 else 8}">{html.escape(val)}</textarea>'
                + (f'<details><summary>Náhled</summary><div class="pv">{val}</div></details>' if preview else "")
                + "</div>")
        cards.append(
            f'<section class="card" data-k="{html.escape(c["cesta"].lower())}" data-u="{html.escape(c["url"])}">'
            f'<header><label><input type="checkbox" class="done"> <b>{html.escape(c["nazev_seo"])}</b></label>'
            f'<small>{html.escape(c["cesta"])} · {stats[c["url"]]["n"]} produktů · '
            f'{len(d["links"])} odkazů ven · {inbound[c["url"]]} odkazů sem · '
            f'<a href="{html.escape(c["url"])}" target="_blank" rel="noopener">otevřít na webu</a></small></header>'
            + "".join(fh) + "</section>")
    page = PAGE_TEMPLATE.replace("{{COUNT}}", str(len(rows))) \
        .replace("{{DATE}}", datetime.now().strftime("%d. %m. %Y")) \
        .replace("{{CARDS}}", "\n".join(cards))
    with open(os.path.join(OUTPUT_DIR, "kategorie-popisy.html"), "w", encoding="utf-8") as f:
        f.write(page)
    log(f"Uloženo: output/kategorie-popisy.html a .csv ({len(rows)} kategorií)")


PAGE_TEMPLATE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Popisy kategorií</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mut:#667085;--line:#e3e6ea;--acc:#2f5597;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--ink:#e8eaee;--mut:#9aa3b2;--line:#2c313a;--acc:#7aa2ff;--ok:#66bb6a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
.top{position:sticky;top:0;background:var(--bg);padding:14px 16px;border-bottom:1px solid var(--line);z-index:2}
.top h1{margin:0 0 6px;font-size:20px}.top p{margin:0 0 8px;color:var(--mut);font-size:13px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1;min-width:200px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
main{max-width:980px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 14px}
.card.isdone{opacity:.55}.card header{margin-bottom:8px}.card header small{display:block;color:var(--mut)}
.card a{color:var(--acc)}
.f{margin:8px 0}.fh{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--mut)}
.fh em{font-style:normal;margin-left:8px;opacity:.8}
textarea{width:100%;font:12px/1.4 ui-monospace,Consolas,monospace;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);resize:vertical}
button{border:1px solid var(--acc);background:var(--acc);color:#fff;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
button.ok{background:var(--ok);border-color:var(--ok)}
details{font-size:13px;margin-top:4px}.pv{border-left:3px solid var(--line);padding:4px 12px;margin-top:6px}
.pv h2{font-size:17px;margin:12px 0 4px}.pv h3{font-size:15px;margin:10px 0 2px}
</style></head><body>
<div class="top"><h1>Popisy kategorií — vmobleceni.cz</h1>
<p>{{COUNT}} kategorií · vygenerováno {{DATE}} · Kopírovat → v Shoptetu vložit do editoru v režimu <b>Zdrojový kód</b> (u HTML polí).</p>
<div class="bar"><input type="search" id="q" placeholder="Hledat kategorii…"><label><input type="checkbox" id="hide"> skrýt hotové</label><span id="cnt"></span></div></div>
<main>{{CARDS}}</main>
<script>
(function(){
 var KEY='kat-hotovo';var st={};try{st=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
 function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
 var cards=[].slice.call(document.querySelectorAll('.card'));
 function apply(){var q=document.getElementById('q').value.toLowerCase(),h=document.getElementById('hide').checked,n=0,d=0;
  cards.forEach(function(c){var done=!!st[c.dataset.u];if(done)d++;c.classList.toggle('isdone',done);
   var show=(!q||c.dataset.k.indexOf(q)>=0)&&!(h&&done);c.style.display=show?'':'none';if(show)n++;});
  document.getElementById('cnt').textContent='zobrazeno '+n+' · hotovo '+d+'/'+cards.length;}
 cards.forEach(function(c){var cb=c.querySelector('.done');cb.checked=!!st[c.dataset.u];
  cb.addEventListener('change',function(){if(cb.checked)st[c.dataset.u]=1;else delete st[c.dataset.u];save();apply();});});
 document.addEventListener('click',function(e){if(!e.target.classList.contains('cp'))return;
  var ta=e.target.closest('.f').querySelector('textarea');var b=e.target;
  function ok(){b.textContent='Zkopírováno';b.classList.add('ok');setTimeout(function(){b.textContent='Kopírovat';b.classList.remove('ok')},1500)}
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(ta.value).then(ok,function(){ta.select();document.execCommand('copy');ok()})}
  else{ta.select();document.execCommand('copy');ok()}});
 document.getElementById('q').addEventListener('input',apply);document.getElementById('hide').addEventListener('change',apply);apply();
})();
</script></body></html>"""


# ---------------------------------------------------------------- hlavní běh
def main():
    if not API_KEY and not DRY_RUN:
        raise SystemExit("Chybí ANTHROPIC_API_KEY")
    cats, by_url = load_categories()
    log(f"Kategorií v kategorie.csv: {len(cats)}")

    feeds = []
    for url, flat in ((FEED_URL, False), (FEED_URL_2, True)):
        if url:
            try:
                feeds.append((fetch_feed(url), flat))
            except Exception as e:
                log(f"⚠ Feed {url[:40]}… se nepodařilo stáhnout: {e}")
    stats = collect_stats(cats, feeds)
    log(f"Kategorií s nalezenými produkty: {sum(1 for c in cats if stats[c['url']]['n'])}")

    state = {"done": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    done = state["done"]
    if REGENERATE == "all":
        done.clear()
    elif REGENERATE:
        for u in REGENERATE.split(","):
            done.pop(u.strip(), None)

    todo = [c for c in cats if c["url"] not in done
            and not any(p in c["url"] for p in SKIP_URL_PARTS)
            and (stats[c["url"]]["n"] > 0 or c["children"])]
    empty = [c for c in cats if stats[c["url"]]["n"] == 0 and not c["children"]]
    if empty:
        log(f"Přeskakuji {len(empty)} kategorií bez produktů ve feedech "
            f"(např. {', '.join(c['nazev_seo'] for c in empty[:5])})")
    log(f"Ke zpracování: {len(todo)} kategorií (limit {BATCH_LIMIT})")
    batch = todo[:BATCH_LIMIT]

    if DRY_RUN:
        with open(os.path.join(OUTPUT_DIR, "kategorie-nahled.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["url", "cesta", "produktu", "odkazy"])
            for c in batch:
                w.writerow([c["url"], c["cesta"], stats[c["url"]]["n"],
                            " | ".join(n for _, n, _ in link_plan(c, by_url, stats, cats))])
        log("DRY_RUN: náhled uložen do output/kategorie-nahled.csv, API se nevolalo.")
        write_outputs(cats, done, stats)
        return

    lock = threading.Lock()
    ok = err = 0

    def work(c):
        links = link_plan(c, by_url, stats, cats)
        allowed = {u for u, _, _ in links}
        prompt = build_prompt(c, stats[c["url"]], links, by_url)
        last = None
        for _ in range(2):  # jeden opakovaný pokus při vadném výstupu
            text, stop = call_api(prompt)
            try:
                return build_result(text, allowed)
            except ValueError as e:
                last = e
        raise ValueError(f"{last} (stop={stop})")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(work, c): c for c in batch}
        for i, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                res = fut.result()
                with lock:
                    done[c["url"]] = res
                    ok += 1
            except PermanentError as e:
                log(f"  Trvalá chyba API: {e} — končím.")
                for f in futs:
                    f.cancel()
                break
            except Exception as e:
                err += 1
                log(f"  {c['nazev_seo']}: {e}")
            if i % 20 == 0:
                with lock:
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=1)
                log(f"  {i}/{len(batch)} ({err} chyb)")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    log(f"Hotovo: {ok} ok, {err} chyb")
    write_outputs(cats, done, stats)


if __name__ == "__main__":
    main()
