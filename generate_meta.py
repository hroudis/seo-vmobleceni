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
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------- konfigurace (z proměnných prostředí) ----------
API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FEED_URL  = os.environ.get("FEED_URL", "").strip()
MODEL     = os.environ.get("MODEL", "claude-haiku-4-5-20251001").strip()
# kolik NOVÝCH produktů max zpracovat za jeden běh (kontrola nákladů)
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "500"))
# volitelně: zpracovat jen produkty obsahující tento řetězec v kategorii
CATEGORY_FILTER = os.environ.get("CATEGORY_FILTER", "").strip()

STATE_FILE  = "state.json"
OUTPUT_DIR  = "output"

API_URL = "https://api.anthropic.com/v1/messages"

INSTRUCTION = """Jsi český e-commerce SEO specialista pro eshop s dámským, dětským a nadrozměrným (XXL+) oblečením vmobleceni.cz.

Vytvoř:
1. meta_description: poutavý český meta popis pro Google, 140–155 znaků, s konkrétní výhodou/benefitem a jemnou výzvou k akci. Přirozená čeština, žádné uvozovky, žádné kódy produktu.
2. seo_title: SEO titulek do 60 znaků, klíčové slovo na začátku, na konci " | VMObleceni.cz"."""


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
    """Vrátí seznam produktů: {code, name, category, material, desc}."""
    root = ET.fromstring(data)
    items = []
    for it in root.iter("SHOPITEM"):
        # kód: stabilní atribut id (CODE bývá i ve FLAGS/VARIANT)
        code = it.get("id") or ""
        name_el = it.find("NAME")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        if not name:
            continue
        cat_el = it.find("DEFAULT_CATEGORY")
        if cat_el is None:
            cat_el = it.find("CATEGORY")
        category = (cat_el.text or "").strip() if cat_el is not None else ""

        # složení z TEXT_PROPERTY (NAME ~ "Složení")
        material = ""
        for tp in it.iter("TEXT_PROPERTY"):
            nm = tp.find("NAME")
            vl = tp.find("VALUE")
            if nm is not None and nm.text and re.search(r"slož|sloz", nm.text, re.I) and vl is not None:
                material = re.sub(r"^\d+\.\s*", "", strip_html(vl.text))
                break

        desc_el = it.find("DESCRIPTION")
        desc = strip_html(desc_el.text)[:300] if desc_el is not None else ""

        items.append({
            "code": code,
            "name": name,
            "category": category,
            "material": material,
            "desc": desc,
        })
    log(f"Feed obsahuje {len(items)} produktů")
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

    # filtr kategorie (volitelně)
    if CATEGORY_FILTER:
        before = len(items)
        items = [p for p in items if CATEGORY_FILTER.lower() in p["category"].lower()]
        log(f"Filtr kategorie '{CATEGORY_FILTER}': {len(items)} z {before} produktů")

    # seskupit podle názvu (varianty sdílí popis) a vzít jen nové
    seen_names = {}
    for p in items:
        key = p["name"].strip().lower()
        if key not in seen_names:
            seen_names[key] = p

    todo = [p for key, p in seen_names.items() if p["code"] not in done]
    log(f"Unikátních názvů: {len(seen_names)} | už hotovo: "
        f"{sum(1 for p in seen_names.values() if p['code'] in done)} | "
        f"nových ke zpracování: {len(todo)}")

    if not todo:
        log("Nic nového k vygenerování. Končím.")
        # přesto zapíšeme aktuální kompletní export (viz níže)
    batch = todo[:BATCH_LIMIT]
    if len(todo) > BATCH_LIMIT:
        log(f"Tento běh zpracuje prvních {BATCH_LIMIT} (limit). Zbytek příště.")

    ok, err = 0, 0
    for i, p in enumerate(batch, 1):
        try:
            res = call_api(build_prompt(p))
            done[p["code"]] = {
                "meta": (res.get("meta_description") or "").strip(),
                "seo_title": (res.get("seo_title") or "").strip(),
                "name": p["name"],
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            ok += 1
            if i % 10 == 0 or i == len(batch):
                log(f"  {i}/{len(batch)} hotovo")
                save_state(state)  # průběžné ukládání
        except Exception as e:
            err += 1
            log(f"  Produkt {p['code']} selhal: {e}")
    save_state(state)
    log(f"Vygenerováno: {ok} ok, {err} chyb")

    # ---------- export CSV + XLSX (kompletní, pro Shoptet import) ----------
    # propíše hotové popisy do VŠECH variant podle názvu
    name_to_meta = {}
    for code, d in done.items():
        nm = d.get("name", "").strip().lower()
        if nm:
            name_to_meta[nm] = d

    rows = []
    for p in items:
        d = name_to_meta.get(p["name"].strip().lower())
        if not d:
            continue
        rows.append({
            "code": p["code"],
            "name": p["name"],
            "metaDescription": d["meta"],
            "seoTitle": d["seo_title"],
            "categoryText": p["category"],
        })

    stamp = datetime.now().strftime("%Y-%m-%d")
    csv_path = os.path.join(OUTPUT_DIR, f"vmobleceni-meta-{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "metaDescription", "seoTitle", "categoryText"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
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
