# -*- coding: utf-8 -*-
"""
Vinted deal finder v9 — sutvarkyta versija.

Ka daro:
  1. Pagal config.json modelius iesko skelbimu Vinted kataloge (naujausi pirmi).
  2. Filtruoja pagal kaina, salį, kalba ir "slamsto" zodzius.
  3. Naujus tinkamus skelbimus siuncia i Telegram.

Paleidimui reikia:
  pip install curl_cffi requests
  Aplinkos kintamieji: BOT_TOKEN, CHAT_ID (GitHub Secrets).

Kodėl curl_cffi: Vinted (Cloudflare/Datadome) blokuoja paprasta python-requests
TLS "pirstu atspauda" -> 403 ir 0 skelbimu. curl_cffi apsimeta tikru Chrome.
Jei curl_cffi neidiegtas, naudojamas requests (bet gali buti blokuojama).
"""

import html
import json
import os
import re
import time
import traceback
import unicodedata

# ======================================================================
# 1. KONFIGURACIJA
# ======================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"
BASE = "https://www.vinted.lt"

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
    "PRICE_LAST_DIGITS": [],
    "PAGES": 3,
    "SLEEP_SECONDS": 3,
    "DRY_RUN": False,
    "DEBUG": False,
    "SEEN_MAX_AGE_DAYS": 7,
    "SEEN_MAX_ENTRIES": 10000,
}


def load_config():
    cfg = dict(DEFAULTS)
    if not os.path.exists(CONFIG_FILE):
        print(f"! {CONFIG_FILE} nerastas – naudojami numatytieji.")
        return cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            raise ValueError("ne JSON objektas")
        cfg.update({k: v for k, v in user_cfg.items() if k in cfg})
        print(f"Konfiguracija ikelta is {CONFIG_FILE}")
    except Exception as e:
        print(f"! Nepavyko nuskaityti {CONFIG_FILE} ({e}) – naudojami numatytieji.")
    return cfg


CFG = load_config()
DEBUG = bool(CFG["DEBUG"])
SLEEP = float(CFG["SLEEP_SECONDS"])


def debug(msg):
    if DEBUG:
        print("  [DEBUG]", msg)


# ======================================================================
# 2. HTTP KLIENTAS (Vinted sesija)
# ======================================================================

try:
    from curl_cffi import requests as http
    USING_CFFI = True
except ImportError:
    import requests as http
    USING_CFFI = False

import requests as plain_requests  # Telegram'ui uztenka paprasto


class VintedClient:
    def __init__(self):
        self.session = None
        self.last_error = ""   # paskutine klaida – siunciama i Telegram diagnostikai

    def _headers(self, json_api=True):
        h = {"Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8", "Referer": BASE + "/"}
        h["Accept"] = "application/json, text/plain, */*" if json_api else "text/html,*/*"
        if not USING_CFFI:  # curl_cffi pats nustato tikra Chrome User-Agent
            h["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        return h

    def start(self):
        """Nauja sesija: atidarom pagrindini puslapi, kad gautume access_token_web slapuka.
        Be sito slapuko API grazina 401."""
        self.session = http.Session(impersonate="chrome") if USING_CFFI else http.Session()
        try:
            r = self.session.get(BASE + "/", headers=self._headers(json_api=False), timeout=20)
            cookies = list(self.session.cookies.keys())
            has_token = "access_token_web" in cookies
            print(f"Sesija: HTTP {r.status_code}, access_token_web={'yra' if has_token else 'NERA'}"
                  f" (klientas: {'curl_cffi' if USING_CFFI else 'requests'})")
            if r.status_code != 200 or not has_token:
                self.last_error = (f"Pagrindinis puslapis: HTTP {r.status_code}, "
                                   f"access_token_web {'yra' if has_token else 'nera'}")
            debug(f"slapukai: {cookies}")
        except Exception as e:
            self.last_error = f"Nepavyko atidaryti {BASE}: {e}"
            print("!", self.last_error)
        time.sleep(2)

    def get_json(self, path, params, retries=3):
        """GET i Vinted API. Grazina dict arba None."""
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(BASE + path, params=params, headers=self._headers(), timeout=20)
            except Exception as e:
                self.last_error = f"Tinklo klaida: {e}"
                print(f"  ! {self.last_error} (bandymas {attempt}/{retries})")
                time.sleep(SLEEP * attempt)
                continue

            code = r.status_code
            if code == 200:
                try:
                    return r.json()
                except ValueError:
                    self.last_error = f"Atsakymas ne JSON: {r.text[:150]}"
                    print("  !", self.last_error)
                    return None

            self.last_error = f"HTTP {code}: {r.text[:150]}"
            print(f"  ! {self.last_error} (bandymas {attempt}/{retries})")
            if code in (401, 403):
                self.start()                      # pasenes/ neduotas tokenas – nauja sesija
                time.sleep(SLEEP)
            elif code == 429 or code >= 500:
                time.sleep(SLEEP * attempt * 2)
            else:
                return None
        return None

    def get_html(self, url, max_bytes=300_000):
        try:
            r = self.session.get(url, headers=self._headers(json_api=False), timeout=20)
            if r.status_code != 200:
                debug(f"skelbimo puslapis HTTP {r.status_code}: {url}")
                return ""
            return r.text[:max_bytes]
        except Exception as e:
            debug(f"skelbimo puslapio klaida: {e}")
            return ""


# ======================================================================
# 3. VINTED DUOMENYS
# ======================================================================

def search_items(client, query, pages):
    """Grazina visu puslapiu skelbimu sarasa (naujausi pirmi)."""
    items = []
    for page in range(1, pages + 1):
        data = client.get_json("/api/v2/catalog/items", {
            "search_text": query,
            "order": "newest_first",
            "per_page": 96,
            "page": page,
        })
        if not isinstance(data, dict):
            break
        batch = data.get("items")
        if not isinstance(batch, list):
            client.last_error = f"Atsakyme nera 'items' saraso. Raktai: {list(data.keys())[:10]}"
            print("  !", client.last_error)
            break
        if not batch:
            break
        items.extend(batch)
        time.sleep(SLEEP)
    return items


def get_price(item):
    """Kaina EUR. Dabartinis formatas: {"amount": "150.0", "currency_code": "EUR"}."""
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(str(p).replace(",", "."))
    except (TypeError, ValueError):
        return None


def get_item_url(item):
    url = item.get("url") or item.get("path") or ""
    return BASE + url if url.startswith("/") else url


def get_country_code(item):
    """Salies kodas is pardavejo profilio domeno (vinted.pl -> PL). Gali buti None."""
    url = (item.get("user") or {}).get("profile_url") or ""
    m = re.search(r"vinted\.([a-z.]+)/", url)
    if not m:
        return None
    return "UK" if m.group(1) == "co.uk" else m.group(1).upper()


_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_PROP_RE = re.compile(r'property=["\']og:([^"\']+)["\']', re.I)
_CONTENT_RE = re.compile(r'content=["\']([^"\']*)["\']', re.I)


def get_og_tags(client, url):
    """Skelbimo pavadinimas/aprasymas is puslapio OpenGraph zymu
    (kataloge aprasymo nera, o /api/v2/items/{id} daznai blokuojamas)."""
    og = {}
    for tag in _META_RE.findall(client.get_html(url)):
        pm, cm = _PROP_RE.search(tag), _CONTENT_RE.search(tag)
        if pm and cm:
            og[pm.group(1)] = html.unescape(cm.group(1))
    return og


# ======================================================================
# 4. FILTRAI
# ======================================================================

def _fold(text):
    """Nuima diakritikus: 'būklė' -> 'bukle' (kad raktažodžiai veiktų abiem rašymo būdais)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _word_re(words):
    parts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


LT_WORDS = _word_re([
    "parduodu", "pardodu", "parduosiu", "bukle", "puiki", "puikus", "puikioje",
    "gera", "geras", "geros", "tvarkingas", "tvarkinga", "veikia", "kaina",
    "euru", "originalus", "originali", "idealios", "baterija", "irasyta",
    "naujas", "nauja", "naudotas", "naudota", "su deklu", "deklas pridedamas",
    "be defektu", "be jokiu defektu", "kokybiskas", "mazai naudotas",
])
PL_WORDS = _word_re([
    "sprzedam", "sprzedaje", "kupie", "telefon", "oryginalny", "oryginalne",
    "stan", "stanie", "wysylka", "wysylke", "zestaw", "paragon", "faktura",
    "nieuszkodzony", "uszkodzony", "ladny", "przesylka", "polecam", "okazja",
    "komplet", "kondycja", "sprawny", "sprawna", "pudelko", "gwarancja",
    "cena", "pekniety", "peknieta", "zbite", "zbita", "wyswietlacz",
    "bateria", "akumulator", "dziala", "pilne", "negocjacje", "akcesoria",
])
DE_WORDS = _word_re([
    "verkaufe", "neuwertig", "versand", "zustand", "gebraucht",
    "originalverpackung", "rechnung", "funktioniert", "einwandfrei",
])
EN_WORDS = _word_re([
    "selling", "brand new", "like new", "shipping", "great condition",
    "excellent condition", "as new", "no issues", "works perfectly",
])
PL_CHARS = set("łńśźżć")
DE_CHARS = set("äöüß")
LV_CHARS = set("āēīōļņģ")   # 'ū' pašalinta – ji yra ir lietuvių kalboje


def detect_foreign_language(text):
    """'PL'/'DE'/'LV'/'RU'/'EN' jei tekstas aiškiai ne lietuviškas, kitaip None."""
    raw = text.lower()
    folded = _fold(raw)
    if not raw.strip() or LT_WORDS.search(folded):
        return None
    if any("Ѐ" <= c <= "ӿ" for c in raw):
        return "RU"
    if PL_CHARS & set(raw) or PL_WORDS.search(folded):
        return "PL"
    if DE_CHARS & set(raw) or DE_WORDS.search(folded):
        return "DE"
    if LV_CHARS & set(raw):
        return "LV"
    if EN_WORDS.search(folded):
        return "EN"
    return None


def is_junk(title):
    t = _fold(title.lower())
    return any(w in t for w in CFG["BLACKLIST_WORDS"])


# ======================================================================
# 5. "JAU MATYTI" SKELBIMAI
# ======================================================================

def load_seen():
    """{id: laiko_zyme}. Palaiko ir senaji formata (ID sarasas)."""
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"! {SEEN_FILE} sugadintas ({e}) – pradedama nuo tuscio.")
        return {}
    now = time.time()
    if isinstance(data, list):
        return {str(x): now for x in data}
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                out[str(k)] = now
        return out
    return {}


def save_seen(seen):
    now = time.time()
    max_age = CFG["SEEN_MAX_AGE_DAYS"] * 86400
    seen = {k: v for k, v in seen.items() if now - v <= max_age}
    newest = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[: CFG["SEEN_MAX_ENTRIES"]]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(newest), f)


# ======================================================================
# 6. TELEGRAM
# ======================================================================

def send_telegram(text):
    if CFG["DRY_RUN"]:
        print("[DRY_RUN] Telegram:", text.replace("\n", " | ")[:200])
        return
    try:
        r = plain_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  ! Telegram klaida: {r.text[:150]}")
    except Exception as e:
        print(f"  ! Nepavyko issiusti Telegram: {e}")


# ======================================================================
# 7. PAGRINDINE LOGIKA
# ======================================================================

def check_item(client, item, model):
    """Grazina (priezastis, None) jei atmesta, arba (None, alert) jei tinka."""
    price = get_price(item)
    if price is None or not (model["min_price"] <= price <= model["max_price"]):
        return "kaina", None

    digits = CFG["PRICE_LAST_DIGITS"]
    if digits and int(price) % 10 not in digits:
        return "kainos skaitmuo", None

    title = item.get("title") or "?"
    if is_junk(title):
        return "slamstas", None

    country = get_country_code(item)
    if country:
        if country not in CFG["ALLOWED_COUNTRY_CODES"]:
            return "salis", None
    elif CFG["REQUIRE_KNOWN_COUNTRY"]:
        return "salis", None

    url = get_item_url(item)
    if CFG["ONLY_LITHUANIAN_TEXT"]:
        og = get_og_tags(client, url)   # brangi uzklausa – tik kai kiti filtrai praeiti
        time.sleep(1)
        title = og.get("title") or title
        if is_junk(title):
            return "slamstas", None
        lang = detect_foreign_language(title + " " + og.get("description", ""))
        if lang:
            debug(f"atmesta kalba={lang}: {title[:60]}")
            return "kalba", None

    return None, {"query": model["query"], "title": title, "price": price, "url": url}


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("Nenurodyti BOT_TOKEN / CHAT_ID (GitHub Secrets)!")
        return

    client = VintedClient()
    client.start()

    seen = load_seen()
    alerts = []
    total_fetched = 0

    for model in CFG["MODELS"]:
        q = model["query"]
        print(f"Tikrinama: '{q}' ({model['min_price']}-{model['max_price']} EUR)...")
        items = search_items(client, q, int(CFG["PAGES"]))
        total_fetched += len(items)
        if items:
            debug(f"pirmo skelbimo price: {items[0].get('price')!r}")

        stats = {}
        for item in items:
            item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            if not item_id or item_id in seen:
                continue
            seen[item_id] = time.time()

            reason, alert = check_item(client, item, model)
            if alert:
                alerts.append(alert)
                reason = "TINKA"
            stats[reason] = stats.get(reason, 0) + 1

        print(f"  Gauta: {len(items)}, nauji: {sum(stats.values())}, {stats}")
        time.sleep(SLEEP)

    save_seen(seen)

    if total_fetched == 0:
        send_telegram("<b>ISPEJIMAS</b>: negauta nei vieno skelbimo is Vinted.\n"
                      f"Priezastis: <code>{html.escape(client.last_error or 'nezinoma')}</code>")
        return

    if not alerts:
        print("Nauju deal'u nera.")
        return

    for a in sorted(alerts, key=lambda a: a["price"]):
        send_telegram(f"<b>{html.escape(a['query'])}</b> – {a['price']:.0f} EUR\n"
                      f"{html.escape(a['title'])}\n{a['url']}")
        print(f"  -> {a['query']} {a['price']:.0f} EUR: {a['title'][:50]}")
    print(f"Issiusta {len(alerts)} alert'u.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(traceback.format_exc())
        send_telegram("<b>SKRIPTAS UZLUZO</b>\n" + html.escape(str(e)[:400]))
