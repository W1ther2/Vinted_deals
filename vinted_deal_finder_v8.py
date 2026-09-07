# -*- coding: utf-8 -*-
"""
Vinted deal finder v4 — taisoma kainu nuskaitymas (API kainos formatas pasikeite).
"""

import requests
import json
import os
import time
import re
import html

# ========== SUSIKONFIGUROK SITAS EILUTES ==========
# BOT_TOKEN ir CHAT_ID imami is aplinkos kintamuju (GitHub Secrets).
# Repo -> Settings -> Secrets and variables -> Actions -> sukurk BOT_TOKEN ir CHAT_ID.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

# ================== KONFIGURACIJA ==================
# VISKAS kraunama is config.json failo (saugomo tame paciame repo).
# Ten keici: modelius, kainu ribas, filtrus – kodo liesti NEREIKIA.
# Jei config.json nera ar sugadintas – naudojami apatiniai numatytieji.

DEFAULTS = {
    "MODELS": [
        {"query": "iPhone 13",     "min_price": 100, "max_price": 160},
        {"query": "iPhone 13 Pro", "min_price": 100, "max_price": 200},
        {"query": "iPhone 14",     "min_price": 100, "max_price": 200},
        {"query": "iPhone 14 Pro", "min_price": 100, "max_price": 350},
    ],
    "BLACKLIST_WORDS": [
        "case", "deklas", "cover", "custodia", "coque",
        "ladegerat", "charger", "kroviklis", "cable", "laidas",
        "box", "dezute", "schutzglas", "glass",
        "hulle", "folija", "grudintas",
    ],
    "ALLOWED_COUNTRY_CODES": ["LT"],
    "REQUIRE_KNOWN_COUNTRY": False,
    "ONLY_LITHUANIAN_TEXT": True,
    "PRICE_LAST_DIGITS": [0, 5, 9],
    "PAGES": 3,
    "SLEEP_SECONDS": 3,
    "DRY_RUN": False,
    "DEBUG": False,
    "SEEN_MAX_AGE_DAYS": 7,
    "SEEN_MAX_ENTRIES": 10000,
}

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update({k: v for k, v in user_cfg.items() if k in cfg})
                print(f"Konfiguracija ikelta is {CONFIG_FILE}")
            else:
                print(f"! {CONFIG_FILE} nera JSON objektas – naudojami numatytieji.")
        except Exception as e:
            print(f"! Nepavyko nuskaityti {CONFIG_FILE} ({e}) – naudojami numatytieji.")
    else:
        print(f"! {CONFIG_FILE} nerastas – naudojami numatytieji.")
    return cfg


_CFG = load_config()

MODELS = _CFG["MODELS"]
BLACKLIST_WORDS = _CFG["BLACKLIST_WORDS"]
ALLOWED_COUNTRY_CODES = list(_CFG["ALLOWED_COUNTRY_CODES"])
REQUIRE_KNOWN_COUNTRY = bool(_CFG["REQUIRE_KNOWN_COUNTRY"])
ONLY_LITHUANIAN_TEXT = bool(_CFG["ONLY_LITHUANIAN_TEXT"])
PRICE_LAST_DIGITS = set(_CFG["PRICE_LAST_DIGITS"])
PAGES = int(_CFG["PAGES"])
SLEEP_SECONDS = int(_CFG["SLEEP_SECONDS"])
DRY_RUN = bool(_CFG["DRY_RUN"])
DEBUG = bool(_CFG["DEBUG"])
SEEN_MAX_AGE_DAYS = int(_CFG["SEEN_MAX_AGE_DAYS"])
SEEN_MAX_ENTRIES = int(_CFG["SEEN_MAX_ENTRIES"])
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
    """Grazina zodyna {skelbimo_id: laiko_zyme}.

    Palaiko ir senaji formata (paprastas ID sarasas) – konvertuoja automatiskai."""
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        now = time.time()
        if isinstance(data, list):          # senas formatas
            return {str(x): now for x in data}
        if isinstance(data, dict):          # naujas formatas
            out = {}
            for k, v in data.items():
                try:
                    out[str(k)] = float(v)
                except (ValueError, TypeError):
                    out[str(k)] = now
            return out
        return {}
    except Exception as e:
        print(f"! {SEEN_FILE} sugadintas ({e}), pradedama nuo tuscio.")
        return {}


def prune_seen(seen):
    """Istrina: (a) idesenes nei SEEN_MAX_AGE_DAYS; (b) pertekliu virs SEEN_MAX_ENTRIES."""
    now = time.time()
    limit = SEEN_MAX_AGE_DAYS * 86400
    pruned = {k: v for k, v in seen.items() if now - v <= limit}
    if len(pruned) > SEEN_MAX_ENTRIES:
        by_time = sorted(pruned.items(), key=lambda kv: kv[1], reverse=True)
        pruned = dict(by_time[:SEEN_MAX_ENTRIES])
    return pruned


def save_seen(seen):
    seen = prune_seen(seen)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


def fetch_page_with_retry(query, page, max_retries=3):
    """Uzklausia viena puslapi su pakartojimais:
    - 401/403 -> atnaujina sesija ir bando dar karta (sesija galejo pasenti)
    - 429     -> ilgesne pauze ir bando dar karta (rate limiting)
    - 5xx / tinklo klaida -> backoff ir bando dar karta
    Grazina items sarasa, tuscia sarasa (nebepuslapiuojam) arba None (viskas zlugo)."""
    url = BASE + "/api/v2/catalog/items"
    params = {"search_text": query, "per_page": 96, "page": page}
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=20)
            if resp.status_code in (401, 403):
                print(f"  ! {resp.status_code} – atnaujinu sesija (bandymas {attempt}/{max_retries})...")
                init_session()
                time.sleep(SLEEP_SECONDS)
                continue
            if resp.status_code == 429:
                wait = SLEEP_SECONDS * attempt * 2
                print(f"  ! 429 per daug uzklausu – laukiu {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                print(f"  ! Serverio klaida {resp.status_code} (bandymas {attempt}/{max_retries})")
                time.sleep(SLEEP_SECONDS * attempt)
                continue
            if resp.status_code != 200:
                print(f"  ! '{query}' p.{page}: HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            if not isinstance(data, dict):
                print(f"  ! '{query}' p.{page}: netiketas atsakymo formatas (ne JSON objektas)")
                return None
            batch = data.get("items") or []
            if not isinstance(batch, list):
                print(f"  ! '{query}' p.{page}: 'items' nera sarasas – API struktura galejo pasikeisti")
                return None
            return batch
        except (requests.RequestException, ValueError) as e:
            print(f"  ! Tinklo/JSON klaida (bandymas {attempt}/{max_retries}): {e}")
            time.sleep(SLEEP_SECONDS * attempt)
    print(f"  ! Visi {max_retries} bandymai nepavyko: '{query}' p.{page}")
    return None


def fetch_items(query, pages):
    items = []
    for page in range(1, pages + 1):
        batch = fetch_page_with_retry(query, page)
        if batch is None:      # viskas zlugo – stabdome si modeli
            break
        if not batch:          # daugiau nera – stabdome puslapiavima
            break
        items.extend(batch)
        time.sleep(SLEEP_SECONDS)
    return items


_debug_og_printed = False
DETAIL_SLEEP_SECONDS = 1.0

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_PROPERTY_RE = re.compile(r'property=["\']([^"\']+)["\']', re.IGNORECASE)
_CONTENT_RE = re.compile(r'content=["\']([^"\']*)["\']', re.IGNORECASE)


def _parse_og_tags(html_text):
    """Israsko visas 'og:*' meta zymas is HTML teksto, nepriklausomai nuo
    property/content atributu tvarkos tage."""
    og = {}
    for tag in _META_TAG_RE.findall(html_text):
        pm = _PROPERTY_RE.search(tag)
        if not pm or not pm.group(1).startswith("og:"):
            continue
        cm = _CONTENT_RE.search(tag)
        if not cm:
            continue
        key = pm.group(1)[3:]  # nuimam "og:" prefiksa
        og[key] = html.unescape(cm.group(1))
    return og


def fetch_item_page_og(item_id, url_path, max_bytes=200_000):
    """Katalogo/paieskos API skelbimo objekte NERA aprasymo, o atskiras JSON
    endpoint'as (/api/v2/items/{id}) Vinted DAZNIAUSIAI BLOKUOJA (403, anti-bot
    apsauga - tai patvirtinta ir populiariuose atviro kodo Vinted scraper'iuose).

    Todel aprasyma skaitome is vieso skelbimo puslapio OpenGraph <meta> zymu
    (title/description/image/url), kurios visada yra HTML <head> dalyje - siam
    keliui pakanka atsiusti tik pirmus kelis desimtis KB puslapio, o ne visa
    JSON API atsakyma, tad jis maziau panasus i "bot" elgesi.

    DEMESIO: jei og:description formatas skiriasi nuo tiketo (pvz. Vinted
    kartais dubliuoja kaina ar kt. teksta prieky), DEBUG isvestis parodys
    tiksliai, ka gavome - pagal tai galesim koreguoti."""
    global _debug_og_printed
    full_url = BASE + url_path if url_path.startswith("/") else url_path
    try:
        resp = session.get(full_url, headers=HEADERS, timeout=20, stream=True)
        if resp.status_code != 200:
            if DEBUG:
                print(f"  [DEBUG] skelbimo puslapio {item_id} uzklausa: HTTP {resp.status_code}")
            resp.close()
            return {}
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        resp.close()
        html_text = b"".join(chunks).decode("utf-8", errors="ignore")
        og = _parse_og_tags(html_text)
        if DEBUG and not _debug_og_printed:
            print(f"  [DEBUG] skelbimo {item_id} OG duomenys (is puslapio <head>):")
            print(" ", json.dumps(og, ensure_ascii=False))
            _debug_og_printed = True
        return og
    except Exception as e:
        if DEBUG:
            print(f"  [DEBUG] nepavyko gauti skelbimo {item_id} puslapio: {e}")
        return {}


def _to_float(v):
    """Saugiai vercia reiksme i float; None jei nepavyksta."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def get_price(item):
    """Lankstus kainos nuskaitymas – bando kelis formatus ir laukus,
    kad kintant API strukturai kuo ilgiau veiktu be taisymu:
    - {"amount": "299.0"} (dict)
    - "29900" (string centais)
    - 29900 (int centais)
    - atsarginiai laukai price_amount / amount / total_item_price"""
    global _debug_price_printed
    p = item.get("price")
    if DEBUG and not _debug_price_printed:
        print(f"  [DEBUG] price: {repr(p)}")
        _debug_price_printed = True
    if isinstance(p, dict):
        for k in ("amount", "value", "price"):
            f = _to_float(p.get(k))
            if f is not None:
                return f
        for v in p.values():
            f = _to_float(v)
            if f is not None:
                return f
        return None
    if p is not None and str(p).strip() != "":
        s = str(p).strip()
        try:
            return int(s) / 100.0          # senas formatas – centai
        except ValueError:
            f = _to_float(s)
            if f is not None:
                return f
    for key in ("price_amount", "amount", "total_item_price", "numeric_price"):
        if key in item:
            f = _to_float(item[key])
            if f is not None:
                return f
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

# Lietuviski pozymiai – jei jie yra, skelbimas laikomas lietuvisku
# (net jei atsitiktinai atsirado viena "uzsienietiska" raide).
LITHUANIAN_WORDS = [
    "parduodu", "pardodu", "parduosiu", "bukle", "puiki", "puikus", "puikioje",
    "gera", "geras", "geros", "tvarkingas", "tvarkinga", "veikia", "kaina",
    "euru", "originalus", "originali", "idealios", "baterija", "irasyta",
    "naujas", "nauja", "naudotas", "naudota", "su deklu", "deklas pridedamas",
    "be defektu", "be jokiu defektu", "kokybiskas", "mazai naudotas",
]
_LITHUANIAN_RE = _word_regex(LITHUANIAN_WORDS)


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

    # Jei yra aiskiu lietuvisku pozymiu – laikome lietuvisku (nepriklausomai
    # nuo atsitiktiniu raidziu). Tai apsaugo nuo klaidingu atmetimu, kai
    # aprasyme nera "tikru" lietuvisku raidziu, pvz. parasyta svelnai.
    if _LITHUANIAN_RE.search(t):
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
    total_fetched = 0

    for model in MODELS:
        q = model["query"]
        print(f"Tikrinama: '{q}' ({model['min_price']}-{model['max_price']} EUR)...")
        items = fetch_items(q, PAGES)
        total_fetched += len(items)
        fresh = 0
        excluded_by_country = 0
        excluded_foreign = 0
        excluded_price_digit = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id") or item.get("item_id") or item.get("entity_id")
            if item_id is None:
                continue
            item_id = str(item_id)
            if item_id in seen:
                continue
            new_seen[item_id] = time.time()

            price = get_price(item)
            if price is None or not (model["min_price"] <= price <= model["max_price"]):
                continue

            if PRICE_LAST_DIGITS and int(price) % 10 not in PRICE_LAST_DIGITS:
                excluded_price_digit += 1
                continue

            title = item.get("title") or item.get("name") or "?"
            url_path = item.get("url") or item.get("path") or item.get("web_url") or ""
            full_url = BASE + url_path if url_path.startswith("/") else url_path

            # Katalogo API nera aprasymo, o JSON detaliu endpoint'as Vinted
            # dazniausiai blokuoja (403). Todel aprasyma skaitome is vieso
            # skelbimo puslapio OpenGraph zymu.
            og = fetch_item_page_og(item_id, url_path)
            time.sleep(DETAIL_SLEEP_SECONDS)
            if og.get("title"):
                title = og["title"]
            description = og.get("description") or ""

            # OG zymos salies neduoda, tad sita liekam prie kataloginio
            # (nors, kaip aptikta, jis, atrodo, visada rodo LT).
            country = get_country_code(item)
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

            alerts.append((q, title, price, full_url))
            fresh += 1
            if DEBUG:
                print(f"  [DEBUG] PRIIMTA (salis={country}): {title[:60]}")

        print(f"  Gauta: {len(items)}, tinkama: {fresh}, atmesta salis: {excluded_by_country}, atmesta uzsienio kalba: {excluded_foreign}, atmesta kainos skaitmuo: {excluded_price_digit}")
        time.sleep(SLEEP_SECONDS)

    # Rusiuojame visus alertus pagal kaina (nuo maziausios)
    alerts.sort(key=lambda a: a[2])

    save_seen(new_seen)

    # Savaime diagnostika: jei is VISU paiesku negauta nei vieno skelbimo,
    # tai zenklas, kad Vinted galejo ka nors pakeisti – pranesame i Telegram.
    if total_fetched == 0 and not DRY_RUN and BOT_TOKEN and CHAT_ID:
        send_telegram("<b>ISPEJIMAS</b>: negauta nei vieno skelbimo is Vinted. "
                      "Galimai pasikeite API – patikrink skripto logus.")

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


def run_with_guard():
    """Visa programa apsupta apsauga: bet kokia netiketa klaida – pranesimas
    i Telegram, kad Vinted pakeitus kazka nezaltum be zinios."""
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            send_telegram("<b>SKRIPTAS UZLUZO</b>\n" + str(e)[:400])
        except Exception:
            pass


if __name__ == "__main__":
    run_with_guard()
