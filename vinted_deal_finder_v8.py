# -*- coding: utf-8 -*-
"""
Vinted deal finder v4 — taisoma kainu nuskaitymas (API kainos formatas pasikeite).
"""

import requests
import json
import os
import time

# ========== SUSIKONFIGUROK SITAS EILUTES ==========
# BOT_TOKEN ir CHAT_ID imami is aplinkos kintamuju (GitHub Secrets).
# Repo -> Settings -> Secrets and variables -> Actions -> sukurk BOT_TOKEN ir CHAT_ID.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

MODELS = [
    {"query": "iPhone 13 Pro", "min_price": 250, "max_price": 420},
    {"query": "iPhone 14 Pro", "min_price": 350, "max_price": 550},
    {"query": "iPhone 15",     "min_price": 400, "max_price": 600},
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
DEBUG = True   # Jei True - parodys pirmu skelbimu kainu struktura diagnostikai
DRY_RUN = False # Jei True - NESIUNCIA zinuciu i Telegram, tik issaugo ka jau matei.
               # Pirmam paleidimui palik True, antram - pakeisk i False.

# Kuriu saliu pardavejus LEISTI (pagal profilio nuorodos domena, pvz. vinted.lt -> "LT").
# Jei nori leisti ir daugiau saliu, pridek koda, pvz. ["LT", "LV"].
ALLOWED_COUNTRY_CODES = ["LT"]

# Jei True - taip pat atmeta skelbimus, kuriu salies nepavyksta nustatyti is profilio
# nuorodos (saugiau, bet gali atmesti ir tikrus lietuviskus skelbimus, jei API tiesiog
# negrazina profile_url). Jei nesi tikras - palik False ir stebek DEBUG isvesti.
REQUIRE_KNOWN_COUNTRY = False

# Jei True - palieka tik skelbimus, kuriu tekstas (pavadinimas/aprasymas) atrodo
# lietuviskas. Atmeta lenkiska, latviska, vokiska, rusiska (kirilica) ar angliska kalba.
# Skelbimai be jokiu aiskiu kalbos pozymiu (pvz. vien "iPhone 13 Pro 128GB")
# PRALEIDZIAMI (nes negalima patikimai nustatyti kalbos vien is modelio pavadinimo).
ONLY_LITHUANIAN_TEXT = True

# Palieka tik skelbimus, kuriu kaina (sveiku euru dalis) baigiasi vienu is siu
# skaitmenu. Pvz. {0, 5, 9} praleis 250, 255, 259, 260, 265... bet ne 251, 262 ir t.t.
# Jei nenori sio filtro - palik tuscia aibe: set()
PRICE_LAST_DIGITS = {0, 5, 9}
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
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                print(f"! {SEEN_FILE} tuscias, pradedama nuo tuscio saraso.")
                return set()
            return set(json.loads(content))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"! {SEEN_FILE} sugadintas ({e}), pradedama nuo tuscio saraso.")
            return set()
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


_debug_detail_printed = False
DETAIL_SLEEP_SECONDS = 1.0


def fetch_item_details(item_id):
    """Katalogo/paieskos API skelbimo objekte NERA aprasymo (description visada
    tuscias) - tai patvirtino DEBUG isvestis. Todel kandidatams, praejusiems
    kainos filtra, papildomai uzklausiame atskiro skelbimo endpoint'o, kuriame
    turetu buti pilnas aprasymas (ir galbut patikimesni pardavejo duomenys).

    DEMESIO: sio endpoint'o tiksli forma spejama pagal iprasta Vinted API
    struktura, nes neturiu galimybes pats jos patikrinti tiesiogiai. Jei
    DEBUG=True, pirmam issikvietimui bus atspausdintas visas atsakymas - jei
    endpoint'as neteisingas (pvz. gaunamas 404 ar kitokia JSON strauktura),
    tai bus matoma is karto ir galesime endpoint'a pataisyti kartu."""
    global _debug_detail_printed
    url = f"{BASE}/api/v2/items/{item_id}"
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            if DEBUG:
                print(f"  [DEBUG] skelbimo {item_id} detaliu uzklausa: HTTP {resp.status_code}")
            return None
        data = resp.json()
        detail = data.get("item") if isinstance(data, dict) else None
        if detail is None and isinstance(data, dict):
            detail = data  # gal atsakymas jau be israsymo "item" rakto
        if DEBUG and not _debug_detail_printed:
            print(f"  [DEBUG] pilnas skelbimo {item_id} detaliu atsakymas:")
            print(" ", json.dumps(data, ensure_ascii=False)[:3000])
            _debug_detail_printed = True
        return detail
    except Exception as e:
        if DEBUG:
            print(f"  [DEBUG] nepavyko gauti skelbimo {item_id} detaliu: {e}")
        return None


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
    vinted.co.uk -> "UK". Jei nepavyksta - None.

    DEMESIO: si euristika gali neveikti, jei API visada grazina profile_url
    su tuo paciu domenu, per kuri siunciama uzklausa (t.y. visada vinted.lt),
    nepriklausomai nuo tikros pardavejo salies. Jei DEBUG=True, pirmam
    skelbimui bus atspausdintas visas 'user' objektas - patikrink, ar jame
    yra kitas laukas (pvz. country_id / country_title), kuri reiketu naudoti
    vietoj profile_url domeno."""
    global _debug_user_printed
    import re
    user = item.get("user") or {}
    if DEBUG and not _debug_user_printed:
        print("  [DEBUG] pilnas 'user' objektas (ieskok salies lauko):")
        print(" ", json.dumps(user, ensure_ascii=False))
        _debug_user_printed = True
    url = user.get("profile_url") or ""
    m = re.search(r"vinted\.([a-z.]+)/", url)
    if not m:
        return None
    domain = m.group(1)          # pvz. "pl", "fr", "co.uk"
    if domain == "co.uk":
        return "UK"
    return domain.upper()


# --- Kalbos aptikimas -------------------------------------------------
# Tikslas: praleisti tik lietuviskus (arba kalbos pozymiu neturincius)
# skelbimus, atmesti aiskiai uzsienietiskus.

import re as _re


def _word_regex(words):
    """Sudaro viena regex su \\b riboms is zodziu/fraziu sarasa (case jau lower)."""
    parts = sorted((_re.escape(w) for w in words), key=len, reverse=True)
    return _re.compile(r"\b(?:" + "|".join(parts) + r")\b")


# Raidziu, kuriu nera lietuviu kalboje (beveik visada = lenkiska kalba)
POLISH_ONLY_CHARS = set("łńśźżć")
POLISH_WORDS = [
    "sprzedam", "sprzedaje", "kupie", "telefon", "oryginalny", "oryginalne",
    "stan", "stanie", "wysylka", "wysylke", "zestaw", "paragon", "faktura",
    "nieuszkodzony", "uszkodzony", "ladny", "przesylka", "polecam", "okazja",
    "komplet", "kondycja", "sprawny", "sprawna", "pudelko", "gwarancja",
    "cena", "pekniety", "peknieta", "zbite", "zbita", "wyswietlacz",
    "bateria", "akumulator", "dziala", "pilne", "negocjacje", "akcesoria",
]
_POLISH_RE = _word_regex(POLISH_WORDS)

# Raidziu, kuriu nera lietuviu kalboje, bet yra latviu
LATVIAN_ONLY_CHARS = set("āēīōūļņģ")

# Vokiskos raides ir dazni zodziai
GERMAN_ONLY_CHARS = set("äöüß")
GERMAN_WORDS = [
    "verkaufe", "neuwertig", "versand", "zustand", "gebraucht",
    "originalverpackung", "rechnung", "funktioniert", "einwandfrei",
]
_GERMAN_RE = _word_regex(GERMAN_WORDS)

# Dazni angliski zodziai/frazes skelbimuose
ENGLISH_WORDS = [
    "selling", "brand new", "like new", "shipping", "great condition",
    "excellent condition", "as new", "no issues", "works perfectly",
]
_ENGLISH_RE = _word_regex(ENGLISH_WORDS)


def _has_cyrillic(text):
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def detect_foreign_language(*texts):
    """Grazina 'PL' / 'LV' / 'DE' / 'RU' / 'EN' jei tekstas atrodo parasytas ne
    lietuviskai, arba None jei atrodo lietuviskas arba kalbos nustatyti
    negalima (per mazai teksto / vien modelio pavadinimas).

    Sie zodziu sarasai sudaryti is zodziu, kuriu praktiskai nepasitaiko
    lietuviu kalboje, tad UZTENKA VIENO atitikimo (naudojant \\b zodzio
    ribas, kad neuzkabintu dalies kito zodzio)."""
    t = " ".join(x for x in texts if x).lower()
    if not t:
        return None

    if _has_cyrillic(t):
        return "RU"

    if (sum(1 for ch in t if ch in POLISH_ONLY_CHARS) >= 1) or _POLISH_RE.search(t):
        return "PL"

    if (sum(1 for ch in t if ch in GERMAN_ONLY_CHARS) >= 1) or _GERMAN_RE.search(t):
        return "DE"

    if sum(1 for ch in t if ch in LATVIAN_ONLY_CHARS) >= 1:
        return "LV"

    if _ENGLISH_RE.search(t):
        return "EN"

    return None


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
        excluded_foreign = 0
        excluded_price_digit = 0

        for item in items:
            item_id = item.get("id")
            if item_id in seen:
                continue
            new_seen.add(item_id)

            price = get_price(item)
            if price is None or not (model["min_price"] <= price <= model["max_price"]):
                continue

            if PRICE_LAST_DIGITS and int(price) % 10 not in PRICE_LAST_DIGITS:
                excluded_price_digit += 1
                continue

            title = item.get("title", "?")

            # Katalogo API nera aprasymo, tad kandidatams (jau praejusiems
            # kainos filtra) uzklausiame skelbimo detales atskirai.
            detail = fetch_item_details(item_id)
            time.sleep(DETAIL_SLEEP_SECONDS)
            if detail:
                title = detail.get("title", title)
                description = detail.get("description") or ""
                country_source = detail
            else:
                description = item.get("description") or ""
                country_source = item

            country = get_country_code(country_source)
            country_ok = (country in ALLOWED_COUNTRY_CODES) if country else (not REQUIRE_KNOWN_COUNTRY)
            if not country_ok:
                excluded_by_country += 1
                if DEBUG:
                    print(f"  [DEBUG] atmesta (salis={country}): {title[:60]}")
                continue

            if ONLY_LITHUANIAN_TEXT:
                lang = detect_foreign_language(title, description)
                if lang:
                    excluded_foreign += 1
                    if DEBUG:
                        print(f"  [DEBUG] atmesta (kalba={lang}, salis={country}): {title[:60]}")
                    continue

            if is_junk(title):
                continue

            url = item.get("url") or ""
            full_url = BASE + url if url.startswith("/") else url
            alerts.append((q, title, price, full_url))
            fresh += 1
            if DEBUG:
                print(f"  [DEBUG] PRIIMTA (salis={country}): {title[:60]}")

        print(f"  Gauta: {len(items)}, tinkama: {fresh}, atmesta salis: {excluded_by_country}, atmesta uzsienio kalba: {excluded_foreign}, atmesta kainos skaitmuo: {excluded_price_digit}")
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
