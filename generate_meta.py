#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEO Studio – dávkový generátor meta popisů pro vmobleceni.cz
============================================================
Stáhne XML feed ze Shoptetu, najde produkty bez meta popisu,
vygeneruje je přes Anthropic API (Haiku) a uloží do CSV/XLSX
ve formátu pro hromadný import do Shoptetu.

Stav už zpracovaných produktů se ukládá do state.json, takže
při dalším běhu se generují JEN nové produkty.

Spuštění lokálně:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export FEED_URL="https://www.vmobleceni.cz/export/products.xml"
    python generate_meta.py

V GitHub Actions se proměnné berou ze secrets (viz NAVOD.md).
"""

import os
import sys
import csv
import json
import time
import html
import re
import threading
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------- konfigurace (z proměnných prostředí) ----------
API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FEED_URL  = os.environ.get("FEED_URL", "").strip()
# volitelný DRUHÝ feed (např. kompletní export přímo ze Shoptetu, vedle
# dodavatelského/Mergado feedu) — produkty z obou zdrojů se sloučí,
# duplicity (stejný CODE) se berou jen jednou. Nepovinné.
FEED_URL_2 = os.environ.get("FEED_URL_2", "").strip()
MODEL     = os.environ.get("MODEL", "claude-haiku-4-5-20251001").strip()
# kolik NOVÝCH produktů max zpracovat za jeden běh (kontrola nákladů)
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "500"))
# kolik požadavků běží souběžně (4-6 je bezpečné vůči rate limitům)
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
# volitelně: zpracovat jen produkty obsahující tento řetězec v kategorii
CATEGORY_FILTER = os.environ.get("CATEGORY_FILTER", "").strip()

STATE_FILE  = "state.json"
OUTPUT_DIR  = "output"

API_URL = "https://api.anthropic.com/v1/messages"

INSTRUCTION = """Jsi český e-commerce SEO specialista pro eshop s dámským, dětským a nadrozměrným (XXL+) oblečením vmobleceni.cz.

Texty musí fungovat pro klasické vyhledávače (Google, Seznam) i pro AI vyhledávání (ChatGPT, Perplexity, Google AI Overviews, Copilot). AI asistenti citují texty, které věcně popisují CO produkt je, PRO KOHO a ČÍM se liší — proto:
- první část popisu je faktická: typ oblečení + pro koho + klíčová vlastnost (materiál, střih, rozsah velikostí),
- konkrétní atributy místo vágních frází ("bavlněné šaty s kapsami, velikosti M–3XL" místo "krásné šaty pro každou příležitost"),
- přirozený jazyk, jako odpověď na otázku zákazníka.

Vytvoř:
1. meta_description: český meta popis, 140–155 znaků. Struktura: věcný popis s konkrétními atributy → benefit → jemná výzva k akci. Žádné uvozovky, žádné kódy produktu.
2. seo_title: SEO titulek do 60 znaků, typ produktu na začátku, na konci " | vmobleceni.cz"."""


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fail(msg):
    log("CHYBA: " + msg)
    sys.exit(1)


# ---------- načtení/uložení stavu ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log("Varování: state.json je poškozený, začínám načisto.")
    return {"done": {}}  # code -> {meta, seo_title, ts}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- stažení a parsování feedu ----------
def fetch_feed(url):
    log(f"Stahuji feed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "SEO-Studio-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    log(f"Staženo {len(data)//1024} kB")
    return data


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(data):
    """Vrátí seznam produktů: {id, codes[], name, category, material, desc}.
    Skutečné kódy zboží (pro párování v Shoptetu/Mergadu) jsou u variant
    (<VARIANTS><VARIANT><CODE>9504/M</CODE>), u produktů bez variant je CODE
    přímým potomkem SHOPITEM. Atribut id je interní ID — jen poslední záchrana."""
    root = ET.fromstring(data)
    items = []
    for it in root.iter("SHOPITEM"):
        pid = it.get("id") or ""
        name_el = it.find("NAME")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        if not name:
            continue
        cat_el = it.find("DEFAULT_CATEGORY")
        if cat_el is None:
            cat_el = it.find("CATEGORY")
        if cat_el is None:
            cats = it.find("CATEGORIES")
            if cats is not None:
                cat_el = cats.find("DEFAULT_CATEGORY")
                if cat_el is None:
                    cat_el = cats.find("CATEGORY")
        category = (cat_el.text or "").strip() if cat_el is not None else ""

        # kódy variant (find/findall hledá jen přímé potomky -> FLAGS/CODE se nechytí)
        codes = []
        vs = it.find("VARIANTS")
        if vs is not None:
            for v in vs.findall("VARIANT"):
                c = v.find("CODE")
                if c is not None and c.text and c.text.strip():
                    codes.append(c.text.strip())
        if not codes:
            c = it.find("CODE")
            if c is not None and c.text and c.text.strip():
                codes.append(c.text.strip())
        if not codes and pid:
            codes = [pid]

        # složení z TEXT_PROPERTY (Shoptet nativní) nebo INFORMATION_PARAMETER (Mergado)
        material = ""
        for tp in it.iter("TEXT_PROPERTY"):
            nm = tp.find("NAME")
            vl = tp.find("VALUE")
            if nm is not None and nm.text and re.search(r"slož|sloz", nm.text, re.I) and vl is not None:
                material = re.sub(r"^\d+\.\s*", "", strip_html(vl.text))
                break
        if not material:
            for ip in it.iter("INFORMATION_PARAMETER"):
                nm = ip.find("NAME")
                vl = ip.find("VALUE")
                if nm is not None and nm.text and re.search(r"slož|sloz", nm.text, re.I) and vl is not None and vl.text:
                    material = strip_html(vl.text)
                    break

        desc_el = it.find("DESCRIPTION")
        desc = strip_html(desc_el.text)[:300] if desc_el is not None else ""

        # už vyplněné meta z e-shopu (pokud je feed obsahuje) -> negenerovat znovu
        m_el = it.find("META_DESCRIPTION")
        existing_meta = (m_el.text or "").strip() if m_el is not None and m_el.text else ""

        items.append({
            "id": pid or (codes[0] if codes else name),
            "codes": codes,
            "name": name,
            "category": category,
            "material": material,
            "desc": desc,
            "existing_meta": existing_meta,
        })
    log(f"Feed obsahuje {len(items)} produktů ({sum(len(i['codes']) for i in items)} variant)")
    return items


# ---------- volání Anthropic API ----------
def build_prompt(p):
    ctx = ""
    if p["material"]:
        ctx += f"\nMateriál/složení: {p['material']}"
    if p["desc"]:
        ctx += f"\nInfo z popisu: {p['desc']}"
    return (
        INSTRUCTION
        + f"\n\nProdukt: {p['name']}\nKategorie: {p['category']}{ctx}"
        + '\n\nOdpověz POUZE validním JSON bez markdownu: {"meta_description":"...","seo_title":"..."}'
    )


def parse_loose_json(raw):
    t = raw.replace("```json", "").replace("```", "").strip()
    start = t.find("{")
    if start > 0:
        t = t[start:]
    try:
        return json.loads(t)
    except Exception:
        pass
    # pokus o záchranu useknutého JSON
    s = t
    if s.count('"') % 2 != 0:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    s += "}" * max(0, s.count("{") - s.count("}"))
    try:
        return json.loads(s)
    except Exception:
        pass
    out = {}
    m = re.search(r'"meta_description"\s*:\s*"((?:[^"\\]|\\.)*)"', t)
    if m:
        out["meta_description"] = m.group(1)
    m = re.search(r'"seo_title"\s*:\s*"((?:[^"\\]|\\.)*)"', t)
    if m:
        out["seo_title"] = m.group(1)
    if out:
        return out
    raise ValueError("Nelze přečíst odpověď API")


def call_api(prompt, retries=3):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return parse_loose_json(text)
        except urllib.error.HTTPError as e:
            last = e
            # 429 = rate limit, 529 = overloaded → počkat a zkusit znovu
            wait = 5 * (attempt + 1)
            if e.code in (429, 529):
                log(f"  API přetížené ({e.code}), čekám {wait}s…")
                time.sleep(wait)
            else:
                log(f"  HTTP chyba {e.code}, čekám {wait}s…")
                time.sleep(wait)
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last if last else RuntimeError("API selhalo")


# ---------- hlavní běh ----------
def main():
    if not API_KEY:
        fail("Chybí ANTHROPIC_API_KEY (proměnná prostředí nebo GitHub secret).")
    if not FEED_URL:
        fail("Chybí FEED_URL (URL tvého XML feedu).")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    state = load_state()
    done = state["done"]

    items = parse_feed(fetch_feed(FEED_URL))
    for p in items:
        p["source"] = "mergado"   # primární/dodavatelský feed -> jde i do mergado-meta.csv

    if FEED_URL_2:
        items2 = parse_feed(fetch_feed(FEED_URL_2))
        for p in items2:
            p["source"] = "shoptet"  # produkty vlastní/přímo v Shoptetu
        # sloučení: pokud se produkt (podle id) vyskytuje v obou feedech,
        # ponecháme verzi z prvního (mergado) feedu, ať se nezdvojí.
        seen_ids = {p["id"] for p in items}
        added = 0
        for p in items2:
            if p["id"] not in seen_ids:
                items.append(p)
                seen_ids.add(p["id"])
                added += 1
        log(f"Druhý feed (Shoptet): {len(items2)} produktů, {added} nových (zbytek už byl v prvním feedu)")

    # filtr kategorie (volitelně)
    if CATEGORY_FILTER:
        before = len(items)
        items = [p for p in items if CATEGORY_FILTER.lower() in p["category"].lower()]
        log(f"Filtr kategorie '{CATEGORY_FILTER}': {len(items)} z {before} produktů")

    # produkty s už vyplněným meta popisem v e-shopu přeskočit (negenerovat, nepřepisovat)
    pre_filled = [p for p in items if p.get("existing_meta")]
    if pre_filled:
        log(f"Přeskakuji {len(pre_filled)} produktů s již vyplněným meta popisem v e-shopu.")
    items = [p for p in items if not p.get("existing_meta")]

    # ---------- seskupení podle názvu ----------
    # Jedna skupina = jeden vygenerovaný popis. Skupina nese VŠECHNY kódy
    # (a zdroje) všech položek se stejným názvem — díky tomu export čte
    # popisy přímo odsud, žádné druhé „hádání podle názvu" (to byl zdroj
    # chyby: dva různé produkty se stejným názvem si navzájem přepisovaly
    # popis v odděleném exportním kroku).
    groups = {}
    for p in items:
        key = p["name"].strip().lower()
        if key not in groups:
            groups[key] = {
                "id": p["id"], "name": p["name"], "category": p["category"],
                "material": p["material"], "desc": p["desc"],
                "codes": [], "sources": set(),
            }
        g = groups[key]
        g["codes"].extend(p["codes"])
        g["sources"].add(p.get("source", "mergado"))

    todo = [g for g in groups.values() if g["id"] not in done]
    log(f"Unikátních názvů: {len(groups)} | už hotovo: "
        f"{sum(1 for g in groups.values() if g['id'] in done)} | "
        f"nových ke zpracování: {len(todo)}")

    if not todo:
        log("Nic nového k vygenerování. Končím.")
        # přesto zapíšeme aktuální kompletní export (viz níže)
    batch = todo[:BATCH_LIMIT]
    if len(todo) > BATCH_LIMIT:
        log(f"Tento běh zpracuje prvních {BATCH_LIMIT} (limit). Zbytek příště.")

    ok, err = 0, 0
    lock = threading.Lock()

    def process(p):
        res = call_api(build_prompt(p))
        return p, res

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(process, p): p for p in batch}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                _, res = fut.result()
                with lock:
                    done[p["id"]] = {
                        "meta": (res.get("meta_description") or "").strip(),
                        "seo_title": (res.get("seo_title") or "").strip(),
                        "name": p["name"],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    ok += 1
            except Exception as e:
                with lock:
                    err += 1
                log(f"  Produkt {p['id']} selhal: {e}")
            if i % 25 == 0 or i == len(batch):
                with lock:
                    log(f"  {i}/{len(batch)} hotovo ({err} chyb)")
                    save_state(state)  # průběžné ukládání
    save_state(state)
    log(f"Vygenerováno: {ok} ok, {err} chyb")

    # ---------- export CSV + XLSX (kompletní, pro Shoptet import) ----------
    # čte se PŘÍMO ze `groups` (skupina má id, kterým se sáhne do `done`) —
    # žádné rekonstruování přes název, takže se nemůže stát, že si dva
    # produkty se stejným názvem přepíšou popis.
    rows = []
    for g in groups.values():
        d = done.get(g["id"])
        if not d:
            continue
        # zdroj řádku: pokud skupina vznikla jen z mergado položek -> mergado,
        # jinak (obsahuje i shoptet položku) řádek patří i do shoptet exportu
        row_source = "shoptet" if "shoptet" in g["sources"] and "mergado" not in g["sources"] else "mergado"
        for code in g["codes"]:   # jeden řádek na variantu — skutečné kódy zboží
            rows.append({
                "code": code,
                "name": g["name"],
                "metaDescription": d["meta"],
                "seoTitle": d["seo_title"],
                "categoryText": g["category"],
                "source": row_source,
            })

    stamp = datetime.now().strftime("%Y-%m-%d")
    csv_path = os.path.join(OUTPUT_DIR, f"vmobleceni-meta-{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "metaDescription", "seoTitle", "categoryText"], delimiter=";")
        w.writeheader()
        w.writerows({k: v for k, v in r.items() if k != "source"} for r in rows)

    # stabilní kopie pro Shoptet — JEN produkty výhradně mimo Mergado (source=="shoptet").
    # Produkty z Mergado feedu se do Shoptetu propíší automaticky přes Mergado
    # import, takže je sem záměrně nedáváme (žádný zbytečný duplicitní import).
    shoptet_rows = [r for r in rows if r["source"] == "shoptet"]
    stable_path = os.path.join(OUTPUT_DIR, "shoptet-meta.csv")
    with open(stable_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "metaDescription", "seoTitle", "categoryText"], delimiter=";")
        w.writeheader()
        w.writerows({k: v for k, v in r.items() if k != "source"} for r in shoptet_rows)
    log(f"Uloženo: {stable_path} ({len(shoptet_rows)} řádků — jen produkty mimo Mergado, stabilní URL pro Shoptet)")

    # ---------- export pro MERGADO (pravidlo Import datového souboru) ----------
    # Přidán sloupec NAME: u vícevariantních produktů je CODE v Mergadu
    # "vícenásobný element" (zanořený uvnitř VARIANTS>VARIANT), zatímco
    # META_DESCRIPTION je hodnota na celý produkt (nezanořená) — párování podle
    # CODE proto u variantních produktů selhávalo. NAME je vždy na úrovni
    # produktu (nikdy zanořené) a je to oficiálně podporovaný párovací element
    # v Mergadu — proto ho nabízíme jako alternativní párovací klíč.
    # CODE necháváme v souboru taky, pro případ že by párování na NAME
    # nefungovalo tak dobře jako se čeká (menší riziko kolize při identickém
    # názvu dvou různých produktů).
    # Sloupce se musí jmenovat PŘESNĚ jako elementy v Mergadu.
    # Bez BOM (Mergado BOM neumí).
    mergado_path = os.path.join(OUTPUT_DIR, "mergado-meta.csv")
    mergado_count = 0
    with open(mergado_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["NAME", "CODE", "META_DESCRIPTION", "SEO_TITLE"])
        for g in groups.values():
            if "mergado" not in g["sources"] or not g["codes"]:
                continue
            d = done.get(g["id"])
            if not d:
                continue
            w.writerow([g["name"], g["codes"][0], d["meta"], d["seo_title"]])
            mergado_count += 1
    log(f"Uloženo: {mergado_path} ({mergado_count} řádků — 1 na produkt, pro Mergado import)")

    # ---------- druhý export: JEDEN ŘÁDEK NA VARIANTU (pro variantní produkty) ----------
    # Pro produkty s > 1 variantou je CODE v Mergadu zanořený uvnitř
    # VARIANTS > VARIANT > CODE. V pravidle importu proto tenhle soubor
    # napoj se ZADANOU CESTOU K ELEMENTU (element-path): "VARIANTS | VARIANT | CODE"
    # jako párovací klíč — ne jen obecné "CODE" (to hledá jen na úrovni
    # produktu a u variantních produktů tam nic nenajde).
    variants_path = os.path.join(OUTPUT_DIR, "mergado-meta-variants.csv")
    variants_count = 0
    with open(variants_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["CODE", "META_DESCRIPTION", "SEO_TITLE"])
        for g in groups.values():
            if "mergado" not in g["sources"] or len(g["codes"]) < 2:
                continue  # jen skutečně variantní produkty (2+ kódy)
            d = done.get(g["id"])
            if not d:
                continue
            for code in g["codes"]:
                w.writerow([code, d["meta"], d["seo_title"]])
                variants_count += 1
    log(f"Uloženo: {variants_path} ({variants_count} řádků — 1 na variantu, pro pravidlo s cestou VARIANTS|VARIANT|CODE)")
    log(f"Uloženo: {csv_path} ({len(rows)} řádků)")

    # XLSX jen pokud je dostupná knihovna openpyxl
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Produkty"
        ws.append(["code", "name", "metaDescription", "seoTitle", "categoryText"])
        for r in rows:
            ws.append([r["code"], r["name"], r["metaDescription"], r["seoTitle"], r["categoryText"]])
        xlsx_path = os.path.join(OUTPUT_DIR, f"vmobleceni-meta-{stamp}.xlsx")
        wb.save(xlsx_path)
        log(f"Uloženo: {xlsx_path}")
    except ImportError:
        log("openpyxl není k dispozici – XLSX přeskočeno (CSV stačí pro import).")

    log("Hotovo.")


if __name__ == "__main__":
    main()
