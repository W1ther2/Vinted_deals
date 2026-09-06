# -*- coding: utf-8 -*-
"""
Vinted deal finder v4 — taisoma kainu nuskaitymas (API kainos formatas pasikeite).
"""

import requests
import json
import os
import time

# ========== SUSIKONFIGUROK SITAS EILUTES ==========
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

MODELS = [
    {"query": "iPhone 12", "min_price": 50, "max_price": 100},
    {"query": "iPhone 12 Pro", "min_price": 50, "max_price": 120},
    {"query": "iPhone 13",     "min_price": 50, "max_price": 170},
    {"query": "iPhone 13 Pro",     "min_price": 50, "max_price": 200},
    {"query": "iPhone 14",     "min_price": 50, "max_price": 170},
    {"query": "iPhone 14 Pro",     "min_price": 100, "max_price": 300},
    {"query": "iPhone 15",     "min_price": 200, "max_price": 300},
]

BLACKLIST_WORDS = [
    "case", "deklas", "cover", "custodia", "coque",
    "ladegerat", "charger", "kroviklis", "cable", "laidas",
    "box", "dezute", "schutzglas", "glass",
    "hulle", "folija", "grudintas",
]

PAGES = 3
SLEEP_SECONDS = 3
SEEN_FILE = "seen.json"
DEBUG = False   # Jei True - parodys pirmu skelbimu kainu struktura diagnostikai
DRY_RUN = False # Jei True - NESIUNCIA zinuciu i Telegram, tik issaugo ka jau matei.
               # Pirmam paleidimui palik True, antram - pakeisk i False.

# Saliu kodai, kuriu skelimu NENORI (pvz. "PL" = Lenkija).
EXCLUDE_COUNTRY_CODES = ["PL"]

# Jei True - atmeta skelbimus, kuriu tekste (pavadinimas/aprasymas) lenkiska kalba.
FILTER_POLISH_TEXT = True
# ===================================================

BASE = "https://www.vinted.lt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
}

session = requests.Session()
_debug_price_printed = False
_debug_user_printed = False


def init_session():
    try:
        r = session.get(BASE + "/", headers=HEADERS, timeout=20)
        print(f"Sesija pradeta (statusas {r.status_code})")
    except Exception as e:
        print(f"! Nepavyko pradeti sesijos: {e}")
    time.sleep(2)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


def fetch_items(query, pages):
    items = []
    for page in range(1, pages + 1):
        url = BASE + "/api/v2/catalog/items"
        params = {"search_text": query, "per_page": 96, "page": page}
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                print(f"  ! '{query}' p.{page}: HTTP {resp.status_code}: {resp.text[:200]}")
                break
            batch = resp.json().get("items", [])
        except Exception as e:
            print(f"  ! Klaida siunciant '{query}' puslapi {page}: {e}")
            break
        if not batch:
            break
        items.extend(batch)
        time.sleep(SLEEP_SECONDS)
    return items


def get_price(item):
    """Supranta abu Vinted kainu formatus:
    - {"amount": "299.0", "currency_code": "EUR"}  (naujas)
    - "29900"  (sena, centais)"""
    global _debug_price_printed
    p = item.get("price")
    if DEBUG and not _debug_price_printed:
        print(f"  [DEBUG] price: {repr(p)}")
        print(f"  [DEBUG] description yra: {'description' in item}, ilgis: {len(item.get('description') or '')}")
        _debug_price_printed = True
    if isinstance(p, dict):
        try:
            return float(p.get("amount", 0))
        except (ValueError, TypeError):
            return None
    raw = str(p or "0")
    try:
        return int(raw) / 100.0
    except ValueError:
        return None


def get_country_code(item):
    """Grazina pardavejo salies koda is profilio URL domeno.
    Pvz. https://www.vinted.pl/member/... -> "PL",  vinted.fr -> "FR",
    vinted.co.uk -> "UK". Jei nepavyksta - None."""
    import re
    user = item.get("user") or {}
    url = user.get("profile_url") or ""
    m = re.search(r"vinted\.([a-z.]+)/", url)
    if not m:
        return None
    domain = m.group(1)          # pvz. "pl", "fr", "co.uk"
    if domain == "co.uk":
        return "UK"
    return domain.upper()


# Raidziu, kuriu nera lietuviu kalboje (beveik visada = lenkiska kalba)
POLISH_ONLY_CHARS = set("łńśźżć")

# Dazni lenkiski zodziai skelbimuose
POLISH_WORDS = [
    "sprzedam", "kupie", "telefon", "oryginalny", "stan", "wysylka",
    "zestaw", "paragon", "faktura", "nieuszkodzony", "ladny",
    "przesylka", "polecam", "okazja", "komplet", "uszkodzony",
]


def is_polish_text(*texts):
    """True jei tekstas (pavadinimas/aprasymas) rasytas lenkiskai."""
    t = " ".join(x for x in texts if x).lower()
    if not t:
        return False
    char_hits = sum(1 for ch in t if ch in POLISH_ONLY_CHARS)
    word_hits = sum(1 for w in POLISH_WORDS if w in t)
    return char_hits >= 2 or (char_hits >= 1 and word_hits >= 1)


def is_junk(title):
    t = title.lower()
    return any(w in t for w in BLACKLIST_WORDS)


def send_telegram(text):
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"  ! Telegram klaida: {r.text[:150]}")
    except Exception as e:
        print(f"  ! Nepavyko issiusti Telegram: {e}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("Nenurodyti BOT_TOKEN / CHAT_ID (GitHub Secrets)!")
        return

    init_session()

    seen = load_seen()
    new_seen = set(seen)
    alerts = []

    for model in MODELS:
        q = model["query"]
        print(f"Tikrinama: '{q}' ({model['min_price']}-{model['max_price']} EUR)...")
        items = fetch_items(q, PAGES)
        fresh = 0
        excluded_by_country = 0
        excluded_polish = 0

        for item in items:
            item_id = item.get("id")
            if item_id in seen:
                continue
            new_seen.add(item_id)

            price = get_price(item)
            if price is None or not (model["min_price"] <= price <= model["max_price"]):
                continue

            country = get_country_code(item)
            if country and country in EXCLUDE_COUNTRY_CODES:
                excluded_by_country += 1
                continue

            title = item.get("title", "?")
            description = item.get("description") or ""
            if FILTER_POLISH_TEXT and is_polish_text(title, description):
                excluded_polish += 1
                continue

            if is_junk(title):
                continue

            url = item.get("url") or ""
            full_url = BASE + url if url.startswith("/") else url
            alerts.append((q, title, price, full_url))
            fresh += 1

        print(f"  Gauta: {len(items)}, tinkama: {fresh}, atmesta salis: {excluded_by_country}, atmesta lenk. tekstas: {excluded_polish}")
        time.sleep(SLEEP_SECONDS)

    save_seen(new_seen)

    if not alerts:
        print("Nauju deal'u nera.")
        return

    if DRY_RUN:
        print(f"[DRY_RUN] Rasta {len(alerts)} dealu, bet zinuciu NESIUNCIAMA.")
        print("[DRY_RUN] Pakeisk DRY_RUN = False ir paleisk dar karta.")
        return

    for q, title, price, full_url in alerts:
        msg = "<b>" + q + "</b> - " + str(round(price)) + " EUR\n" + title + "\n" + full_url
        send_telegram(msg)
        print(f"  -> {q} {price:.0f} EUR: {title[:50]}")

    print(f"Issiusta {len(alerts)} alert'u.")


if __name__ == "__main__":
    main()
