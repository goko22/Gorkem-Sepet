import asyncio
import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from utils import detect_store, parse_price


# =========================================================
# AYARLAR
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}

HTTP_TIMEOUT = httpx.Timeout(
    connect=4.0,
    read=7.0,
    write=7.0,
    pool=4.0,
)

BROWSER_GOTO_TIMEOUT = 25000


# =========================================================
# GLOBAL CHROMIUM
# =========================================================

_playwright_instance = None
_browser_instance = None
_browser_lock = None


def get_browser_lock():
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def get_browser():
    global _playwright_instance
    global _browser_instance

    if (
        _browser_instance is not None
        and _browser_instance.is_connected()
    ):
        return _browser_instance

    async with get_browser_lock():
        if (
            _browser_instance is not None
            and _browser_instance.is_connected()
        ):
            return _browser_instance

        print("BROWSER STARTING...", flush=True)

        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()

        _browser_instance = await _playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-component-update",
                "--disable-default-apps",
                "--no-first-run",
            ],
        )

        print("BROWSER READY", flush=True)
        return _browser_instance


async def reset_browser():
    global _browser_instance

    if _browser_instance is not None:
        try:
            await _browser_instance.close()
        except Exception:
            pass

    _browser_instance = None


# =========================================================
# MODEL
# =========================================================

@dataclass
class ScrapedProduct:
    title: str
    store: str
    url: str
    price: Optional[float] = None
    image_url: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    variants: Optional[dict] = None
    variant_text: Optional[str] = None
    method: str = "unknown"


# =========================================================
# GENEL YARDIMCILAR
# =========================================================

def normalize_text(text):
    text = str(text or "").lower()

    replacements = {
        "ı": "i",
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ö": "o",
        "ç": "c",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


def normalize_image_url(url):
    if not url:
        return None

    url = str(url).strip()

    if url.startswith("//"):
        return "https:" + url

    return url


# =========================================================
# FIYAT
# =========================================================

def clean_price(value):
    """
    4.799 TL       -> 4799
    12.499 TL      -> 12499
    4.799,90 TL    -> 4799.90
    899,99 TL      -> 899.99

    API:
    229.00         -> 229
    898.99         -> 898.99
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if 0 < number < 100_000_000:
            return number
        return None

    text = str(value).strip()

    if not text:
        return None

    match = re.search(r"\d[\d.,]*", text)

    if not match:
        try:
            return parse_price(text)
        except Exception:
            return None

    raw = match.group(0)

    try:
        if "." in raw and "," in raw:
            if raw.rfind(",") > raw.rfind("."):
                raw = raw.replace(".", "").replace(",", ".")
            else:
                raw = raw.replace(",", "")

        elif "," in raw:
            parts = raw.split(",")

            if len(parts) == 2 and len(parts[-1]) in (1, 2):
                raw = raw.replace(",", ".")
            else:
                raw = raw.replace(",", "")

        elif "." in raw:
            parts = raw.split(".")

            if (
                len(parts) >= 2
                and all(len(part) == 3 for part in parts[1:])
            ):
                raw = raw.replace(".", "")

        number = float(raw)

        if 0 < number < 100_000_000:
            return number

    except Exception:
        pass

    try:
        return parse_price(text)
    except Exception:
        return None


# =========================================================
# VARIANT
# =========================================================

def pretty_variant_value(key, value):
    if value is None:
        return None

    value = unquote(str(value)).strip()

    if not value:
        return None

    normalized = normalize_text(value)

    uppercase_values = {
        "soft",
        "xsoft",
        "x soft",
        "mid",
        "xl",
        "xxl",
        "xxxl",
        "s",
        "m",
        "l",
    }

    if normalized in uppercase_values:
        if normalized == "x soft":
            return "XSOFT"
        return value.upper()

    colors = {
        "siyah": "Siyah",
        "black": "Black",
        "beyaz": "Beyaz",
        "white": "White",
        "turuncu": "Turuncu",
        "orange": "Orange",
        "kirmizi": "Kırmızı",
        "red": "Red",
        "mavi": "Mavi",
        "blue": "Blue",
        "yesil": "Yeşil",
        "green": "Green",
        "pembe": "Pembe",
        "pink": "Pink",
        "mor": "Mor",
        "purple": "Purple",
        "gri": "Gri",
        "gray": "Gray",
        "grey": "Grey",
    }

    if normalized in colors:
        return colors[normalized]

    if re.search(r"\d", value):
        return value

    if any(char.isupper() for char in value):
        return value

    return value.capitalize()


def infer_variant_label(label, value, index=0):
    original = str(label or "").strip()
    normalized = normalize_text(original)

    mapping = {
        "surface": "Yüzey",
        "yuzey": "Yüzey",
        "size": "Boyut",
        "boyut": "Boyut",
        "dimension": "Ölçü",
        "dimensions": "Ölçü",
        "olcu": "Ölçü",
        "length": "Uzunluk",
        "uzunluk": "Uzunluk",
        "color": "Renk",
        "colour": "Renk",
        "renk": "Renk",
        "beden": "Beden",
        "switch": "Switch",
        "dongle": "Dongle",
        "layout": "Layout",
        "capacity": "Kapasite",
        "kapasite": "Kapasite",
        "memory": "Hafıza",
        "hafiza": "Hafıza",
        "model": "Model",
        "style": "Stil",
        "stil": "Stil",
    }

    if normalized in mapping:
        return mapping[normalized]

    value_text = str(value or "").strip()
    value_norm = normalize_text(value_text)

    generic_names = {
        "",
        "option",
        "options",
        "secenek",
        "secenekler",
        "variant",
        "varyant",
        "title",
        "default",
    }

    if normalized in generic_names:
        if re.search(r"\d+\s*[xX×]\s*\d+", value_text):
            return "Boyut"

        if re.fullmatch(
            r"\s*\d+(?:[.,]\d+)?\s*(?:mm|cm|m|metre|meter)\s*",
            value_text,
            re.I,
        ):
            return "Ölçü"

        if value_norm in {
            "soft",
            "xsoft",
            "x soft",
            "mid",
            "speed",
            "control",
            "surge",
        }:
            return "Yüzey"

        return "Seçenek" if index == 0 else f"Seçenek {index + 1}"

    if original:
        return original

    return "Seçenek" if index == 0 else f"Seçenek {index + 1}"


def make_variant_text(variants):
    if not variants:
        return None

    parts = []

    for key, value in variants.items():
        if value is None:
            continue

        value = str(value).strip()

        if value:
            parts.append(f"{key}: {value}")

    return " • ".join(parts) if parts else None


def query_variants(url):
    query = parse_qs(urlparse(url).query)

    mapping = {
        "yuzey": "Yüzey",
        "surface": "Yüzey",
        "boyut": "Boyut",
        "size": "Boyut",
        "olcu": "Ölçü",
        "dimension": "Ölçü",
        "beden": "Beden",
        "renk": "Renk",
        "color": "Renk",
        "colour": "Renk",
        "switch": "Switch",
        "dongle": "Dongle",
        "layout": "Layout",
        "kapasite": "Kapasite",
        "capacity": "Kapasite",
    }

    result = {}

    for raw_key, values in query.items():
        key = normalize_text(raw_key)
        label = mapping.get(key)

        if not label or not values:
            continue

        value = pretty_variant_value(label, values[0])

        if value:
            result[label] = value

    return result


# =========================================================
# JSON-LD
# =========================================================

def walk_for_product(data):
    if isinstance(data, dict):
        product_type = data.get("@type")

        if product_type == "Product":
            return data

        if (
            isinstance(product_type, list)
            and "Product" in product_type
        ):
            return data

        for value in data.values():
            result = walk_for_product(value)
            if result:
                return result

    elif isinstance(data, list):
        for item in data:
            result = walk_for_product(item)
            if result:
                return result

    return None


# =========================================================
# HEPSIBURADA
# =========================================================

def extract_hepsiburada_code(url):
    match = re.search(
        r"(HBCV[A-Z0-9]+|HBC[A-Z0-9]+)",
        url,
        re.I,
    )

    if match:
        return match.group(1).upper()

    return None


def _hb_number_candidates_from_json(
    obj,
    product_code,
    title,
    path="",
    context="",
    output=None,
):
    """
    Hepsiburada browser network response'larındaki
    kampanya/basket fiyat adaylarını toplar.

    Yalnızca fiyat/kampanya çağrışımlı alanları kabul eder.
    Product code/title aynı JSON parçasında varsa puanı yükseltir.
    """

    if output is None:
        output = []

    title_norm = normalize_text(title)
    product_code_norm = normalize_text(product_code)

    if isinstance(obj, dict):
        serialized = ""

        try:
            serialized = json.dumps(
                obj,
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            serialized = str(obj)

        serialized_norm = normalize_text(serialized)

        local_match = (
            product_code_norm
            and product_code_norm in serialized_norm
        )

        if (
            not local_match
            and title_norm
            and len(title_norm) > 12
        ):
            title_words = [
                x
                for x in title_norm.split()
                if len(x) >= 3
            ]

            if title_words:
                hit_count = sum(
                    1
                    for word in title_words[:8]
                    if word in serialized_norm
                )

                local_match = hit_count >= min(
                    3,
                    len(title_words[:8]),
                )

        for key, value in obj.items():
            key_text = str(key)
            key_norm = normalize_text(key_text)
            next_path = f"{path}.{key_text}" if path else key_text

            if isinstance(value, (dict, list)):
                _hb_number_candidates_from_json(
                    value,
                    product_code,
                    title,
                    path=next_path,
                    context=serialized_norm if local_match else context,
                    output=output,
                )
                continue

            keywords = {
                "price",
                "fiyat",
                "basket",
                "cart",
                "sepette",
                "discount",
                "indirim",
                "campaign",
                "kampanya",
                "final",
                "sale",
                "selling",
                "current",
                "special",
                "promotion",
                "promo",
            }

            if not any(word in key_norm for word in keywords):
                continue

            price = clean_price(value)

            if price is None:
                continue

            score = 0

            strong_words = {
                "basket",
                "cart",
                "sepette",
                "final",
                "campaign",
                "kampanya",
                "promotion",
                "promo",
                "discounted",
                "discount",
                "indirimli",
            }

            if any(word in key_norm for word in strong_words):
                score += 40
            else:
                score += 10

            if local_match:
                score += 100

            if context and (
                product_code_norm in context
                or (
                    title_norm
                    and len(title_norm) > 12
                    and title_norm.split()[0] in context
                )
            ):
                score += 30

            output.append(
                {
                    "price": price,
                    "score": score,
                    "path": next_path,
                    "value": value,
                }
            )

    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            _hb_number_candidates_from_json(
                item,
                product_code,
                title,
                path=f"{path}[{index}]",
                context=context,
                output=output,
            )

    return output


def _hb_choose_basket_candidate(
    candidates,
    original_price=None,
    listing_price=None,
):
    """
    Yanlış ürünü seçmemek için aşırı agresif davranmaz.

    Aday:
    - pozitif olmalı
    - normal/listing fiyatın çok uçuk altında olmamalı
    - normal fiyatın üstünde olmamalı
    """

    if not candidates:
        return None

    reference_high = original_price or listing_price
    reference_low = listing_price or original_price

    valid = []

    for item in candidates:
        price = item["price"]

        if price is None or price <= 0:
            continue

        if (
            reference_high is not None
            and price > reference_high * 1.02
        ):
            continue

        if (
            reference_low is not None
            and price < reference_low * 0.55
        ):
            continue

        if (
            original_price is not None
            and price > original_price * 1.02
        ):
            continue

        valid.append(item)

    if not valid:
        return None

    valid.sort(
        key=lambda x: (
            -x["score"],
            x["price"],
        )
    )

    best = valid[0]

    if best["score"] < 70:
        return None

    return best


async def scrape_hepsiburada_basket_price_browser(
    url,
    title,
    product_code,
    original_price=None,
    listing_price=None,
):
    """
    Hepsiburada sepete özel fiyatı için ikinci katman.

    1) Network JSON response'larını dinler.
    2) DOM'da "Sepette ... TL" metnini arar.
    3) Güvenli doğrulama yapar.

    Render Hepsiburada güvenlik sayfasına düşerse
    None döner ve normal Parse.bot fiyatı kullanılmaya devam eder.
    """

    print(
        "HB BASKET DETECTOR START",
        flush=True,
    )

    browser = await get_browser()
    context = None
    page = None
    network_candidates = []

    try:
        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=HEADERS["User-Agent"],
            viewport={
                "width": 1365,
                "height": 900,
            },
            extra_http_headers={
                "Accept-Language":
                    "tr-TR,tr;q=0.9,en;q=0.8"
            },
        )

        page = await context.new_page()

        async def route_handler(route):
            resource_type = route.request.resource_type

            if resource_type in {
                "font",
                "media",
            }:
                await route.abort()
            else:
                await route.continue_()

        await page.route(
            "**/*",
            route_handler,
        )

        async def handle_response(response):
            response_url = response.url.lower()

            interesting_url = any(
                token in response_url
                for token in [
                    "campaign",
                    "promo",
                    "promotion",
                    "discount",
                    "basket",
                    "cart",
                    "price",
                    "product",
                    "listing",
                    "offer",
                ]
            )

            if not interesting_url:
                return

            try:
                content_type = (
                    response.headers.get(
                        "content-type",
                        "",
                    )
                    .lower()
                )

                if (
                    "json" not in content_type
                    and "javascript" not in content_type
                    and "text" not in content_type
                ):
                    return

                payload = await response.json()

            except Exception:
                return

            try:
                found = _hb_number_candidates_from_json(
                    payload,
                    product_code,
                    title,
                )

                if found:
                    network_candidates.extend(
                        found
                    )

                    print(
                        "HB NETWORK PRICE CANDIDATES:",
                        [
                            (
                                x["price"],
                                x["score"],
                                x["path"],
                            )
                            for x in found[:12]
                        ],
                        flush=True,
                    )

            except Exception:
                pass

        page.on(
            "response",
            handle_response,
        )

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_GOTO_TIMEOUT,
            )

        except PlaywrightTimeoutError as e:
            print(
                "HB BASKET GOTO TIMEOUT -> CONTINUE:",
                repr(e),
                flush=True,
            )

        try:
            await page.wait_for_timeout(
                1800
            )
        except Exception:
            pass

        # =================================================
        # SECURITY PAGE KONTROL
        # =================================================

        try:
            page_title = await page.title()
        except Exception:
            page_title = ""

        print(
            "HB BASKET PAGE TITLE:",
            page_title,
            flush=True,
        )

        # =================================================
        # DOM: "Sepette 6.048 TL"
        # =================================================

        dom_candidates = []

        try:
            dom_candidates = await page.evaluate(
                """
                () => {
                    const results = [];

                    function visible(el) {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }

                    const nodes = Array.from(
                        document.querySelectorAll(
                            'body *'
                        )
                    );

                    for (const node of nodes) {
                        if (!visible(node)) {
                            continue;
                        }

                        const text = (
                            node.innerText
                            || node.textContent
                            || ''
                        ).replace(/\\s+/g, ' ').trim();

                        if (
                            !text
                            || text.length > 300
                        ) {
                            continue;
                        }

                        if (
                            !/sepette/i.test(text)
                            || !/\\d[\\d.,]*\\s*TL/i.test(text)
                        ) {
                            continue;
                        }

                        const matches = (
                            text.match(
                                /\\d[\\d.,]*\\s*TL/gi
                            )
                            || []
                        );

                        for (const match of matches) {
                            results.push({
                                text,
                                priceText: match
                            });
                        }
                    }

                    return results.slice(0, 50);
                }
                """
            )

        except Exception as e:
            print(
                "HB DOM BASKET ERROR:",
                repr(e),
                flush=True,
            )

        parsed_dom = []

        for item in dom_candidates:
            price = clean_price(
                item.get(
                    "priceText"
                )
            )

            if price is None:
                continue

            parsed_dom.append(
                {
                    "price": price,
                    "score": 85,
                    "path": "DOM:Sepette",
                    "value": item.get("text"),
                }
            )

        if parsed_dom:
            print(
                "HB DOM BASKET CANDIDATES:",
                [
                    (
                        x["price"],
                        x["value"],
                    )
                    for x in parsed_dom[:10]
                ],
                flush=True,
            )

        combined = (
            network_candidates
            + parsed_dom
        )

        best = _hb_choose_basket_candidate(
            combined,
            original_price=original_price,
            listing_price=listing_price,
        )

        if best:
            print(
                "HB BASKET DETECTED:",
                best["price"],
                best["path"],
                "score=",
                best["score"],
                flush=True,
            )

            return best["price"]

        print(
            "HB BASKET PRICE NOT FOUND",
            flush=True,
        )

        return None

    except Exception as e:
        print(
            "HB BASKET DETECTOR ERROR:",
            repr(e),
            flush=True,
        )

        return None

    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def scrape_hepsiburada_api(url):
    api_key = os.getenv(
        "PARSE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "PARSE_API_KEY bulunamadı."
        )

    product_code = extract_hepsiburada_code(
        url
    )

    if not product_code:
        raise RuntimeError(
            "Hepsiburada ürün kodu bulunamadı."
        )

    base_url = (
        "https://api.parse.bot/scraper/"
        "a42a78b8-347f-4c69-8e83-135320d2b001"
    )

    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
    ) as client:
        response = await client.get(
            f"{base_url}/get_product_details",
            headers=headers,
            params={
                "url": url
            },
        )

        print(
            "HB DETAILS STATUS:",
            response.status_code,
            flush=True,
        )

        response.raise_for_status()

        payload = response.json()

        data = (
            payload.get(
                "data",
                payload,
            )
            if isinstance(payload, dict)
            else payload
        )

        if isinstance(data, list):
            if not data:
                raise RuntimeError(
                    "Hepsiburada API boş cevap verdi."
                )

            data = data[0]

        if not isinstance(data, dict):
            raise RuntimeError(
                "Hepsiburada API cevabı geçersiz."
            )

        title = (
            data.get("product_name")
            or data.get("name")
            or data.get("title")
        )

        if not title:
            raise RuntimeError(
                "Hepsiburada ürün adı alınamadı."
            )

        detail_price = clean_price(
            data.get("unit_price")
            or data.get("current_price")
            or data.get("sale_price")
            or data.get("discounted_price")
            or data.get("price")
        )

        original_price = clean_price(
            data.get("original_price")
            or data.get("list_price")
            or data.get("old_price")
        )

        brand = (
            data.get("brand")
            or data.get("brand_name")
        )

        model = (
            data.get("sku")
            or data.get("productId")
            or data.get("product_id")
            or product_code
        )

        image_url = (
            data.get("imageUrl")
            or data.get("image_url")
            or data.get("image")
        )

        search_price = None

        try:
            search_response = await client.get(
                f"{base_url}/search_products",
                headers=headers,
                params={
                    "page": 1,
                    "query": title,
                },
            )

            print(
                "HB SEARCH STATUS:",
                search_response.status_code,
                flush=True,
            )

            if search_response.status_code == 200:
                search_payload = search_response.json()

                search_data = (
                    search_payload.get(
                        "data",
                        search_payload,
                    )
                    if isinstance(
                        search_payload,
                        dict,
                    )
                    else search_payload
                )

                products = []

                if isinstance(search_data, dict):
                    products = search_data.get(
                        "products",
                        [],
                    )

                elif isinstance(search_data, list):
                    products = search_data

                if not isinstance(products, list):
                    products = []

                details_product_id = str(
                    data.get("product_id")
                    or data.get("productId")
                    or ""
                ).upper()

                parsed_query = parse_qs(
                    urlparse(url).query
                )

                requested_merchant = (
                    parsed_query.get(
                        "magaza",
                        [None],
                    )[0]
                )

                requested_merchant_norm = (
                    normalize_text(
                        requested_merchant
                    )
                    if requested_merchant
                    else ""
                )

                best_item = None
                best_score = -1

                for item in products:
                    if not isinstance(item, dict):
                        continue

                    item_name = (
                        item.get("name")
                        or item.get("product_name")
                        or ""
                    )

                    score = (
                        similarity(
                            title,
                            item_name,
                        )
                        * 50
                    )

                    item_sku = str(
                        item.get("sku")
                        or ""
                    ).upper()

                    item_product_id = str(
                        item.get("productId")
                        or item.get("product_id")
                        or ""
                    ).upper()

                    if item_sku == product_code:
                        score += 150

                    if (
                        details_product_id
                        and item_product_id
                        == details_product_id
                    ):
                        score += 150

                    merchant_name = (
                        item.get("merchantName")
                        or item.get("merchant_name")
                        or item.get("seller")
                        or ""
                    )

                    if (
                        requested_merchant_norm
                        and normalize_text(
                            merchant_name
                        )
                        == requested_merchant_norm
                    ):
                        score += 100

                    candidate_price = clean_price(
                        item.get("price")
                        or item.get("priceText")
                    )

                    if candidate_price is not None:
                        score += 5

                    if score > best_score:
                        best_score = score
                        best_item = item

                if best_item:
                    search_price = clean_price(
                        best_item.get("price")
                        or best_item.get("priceText")
                    )

                    if not image_url:
                        image_url = (
                            best_item.get("imageUrl")
                            or best_item.get("image_url")
                            or best_item.get("image")
                        )

                    print(
                        "HB SEARCH MATCH:",
                        best_item.get("merchantName")
                        or best_item.get("merchant_name")
                        or best_item.get("seller"),
                        search_price,
                        flush=True,
                    )

        except Exception as e:
            print(
                "HB SEARCH ERROR:",
                repr(e),
                flush=True,
            )

        normal_candidates = [
            p
            for p in [
                detail_price,
                search_price,
            ]
            if p is not None and p > 0
        ]

        listing_price = (
            min(normal_candidates)
            if normal_candidates
            else original_price
        )

        if original_price is None:
            original_price = (
                max(normal_candidates)
                if normal_candidates
                else listing_price
            )

        # =================================================
        # SEPLETE ÖZEL FIYAT
        # =================================================

        basket_price = None

        try:
            basket_price = (
                await scrape_hepsiburada_basket_price_browser(
                    url=url,
                    title=title,
                    product_code=product_code,
                    original_price=original_price,
                    listing_price=listing_price,
                )
            )

        except Exception as e:
            print(
                "HB BASKET LAYER ERROR:",
                repr(e),
                flush=True,
            )

        effective_price = (
            basket_price
            if basket_price is not None
            else listing_price
        )

        if effective_price is None:
            raise RuntimeError(
                "Hepsiburada fiyat alınamadı."
            )

        print(
            "==================================",
            flush=True,
        )
        print(
            "HB TITLE:",
            title,
            flush=True,
        )
        print(
            "HB DETAIL PRICE:",
            detail_price,
            flush=True,
        )
        print(
            "HB ORIGINAL:",
            original_price,
            flush=True,
        )
        print(
            "HB SEARCH PRICE:",
            search_price,
            flush=True,
        )
        print(
            "HB LISTING PRICE:",
            listing_price,
            flush=True,
        )
        print(
            "HB BASKET PRICE:",
            basket_price,
            flush=True,
        )
        print(
            "HB FINAL PRICE:",
            effective_price,
            flush=True,
        )
        print(
            "==================================",
            flush=True,
        )

        variants = query_variants(
            url
        )

        return ScrapedProduct(
            title=title[:500],
            store="Hepsiburada",
            url=url,
            price=effective_price,
            image_url=normalize_image_url(
                image_url
            ),
            brand=brand,
            model=model,
            variants=variants,
            variant_text=make_variant_text(
                variants
            ),
            method=(
                "parse-api+basket"
                if basket_price is not None
                else "parse-api-listing"
            ),
        )


# =========================================================
# GENEL SHOPIFY
# =========================================================

def looks_like_shopify_product_url(url):
    try:
        parsed = urlparse(url)
        return "/products/" in parsed.path.lower()
    except Exception:
        return False


def shopify_product_json_url(url):
    parsed = urlparse(url)
    clean_path = parsed.path.rstrip("/")

    if clean_path.endswith(".js"):
        path = clean_path
    else:
        path = clean_path + ".js"

    return (
        f"{parsed.scheme}"
        f"://{parsed.netloc}"
        f"{path}"
    )


def shopify_price(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        if value <= 0:
            return None
        return float(value) / 100

    if isinstance(value, float):
        if value <= 0:
            return None
        return (
            value / 100
            if value >= 10000
            else value
        )

    text = str(value).strip()

    if not text:
        return None

    if re.fullmatch(r"\d+", text):
        number = int(text)

        if number <= 0:
            return None

        return number / 100

    return clean_price(text)


def shopify_option_names(product):
    raw_options = product.get("options") or []
    names = []

    for index, option in enumerate(raw_options):
        if isinstance(option, dict):
            name = (
                option.get("name")
                or option.get("title")
                or f"Seçenek {index + 1}"
            )
        else:
            name = str(option)

        names.append(name)

    return names


def shopify_variant_values(variant):
    options = variant.get("options")

    if isinstance(options, list):
        return options

    return [
        variant.get("option1"),
        variant.get("option2"),
        variant.get("option3"),
    ]


def extract_shopify_variants(
    product,
    selected,
):
    names = shopify_option_names(
        product
    )

    values = shopify_variant_values(
        selected
    )

    result = {}

    for index, raw_value in enumerate(values):
        if raw_value is None:
            continue

        value = str(raw_value).strip()

        if not value:
            continue

        if normalize_text(value) == "default title":
            continue

        raw_label = (
            names[index]
            if index < len(names)
            else f"Seçenek {index + 1}"
        )

        label = infer_variant_label(
            raw_label,
            value,
            index,
        )

        pretty = pretty_variant_value(
            label,
            value,
        )

        if not pretty:
            continue

        final_label = label

        if final_label in result:
            final_label = (
                f"{label} {index + 1}"
            )

        result[final_label] = pretty

    return result


def extract_shopify_image(
    product,
    selected,
):
    featured = selected.get(
        "featured_image"
    )

    if isinstance(featured, dict):
        image = (
            featured.get("src")
            or featured.get("url")
        )

        if image:
            return normalize_image_url(
                image
            )

    elif isinstance(featured, str):
        return normalize_image_url(
            featured
        )

    media = selected.get(
        "featured_media"
    )

    if isinstance(media, dict):
        preview = media.get(
            "preview_image"
        )

        if isinstance(preview, dict):
            image = (
                preview.get("src")
                or preview.get("url")
            )

            if image:
                return normalize_image_url(
                    image
                )

    product_image = product.get(
        "featured_image"
    )

    if isinstance(product_image, str):
        return normalize_image_url(
            product_image
        )

    if isinstance(product_image, dict):
        image = (
            product_image.get("src")
            or product_image.get("url")
        )

        if image:
            return normalize_image_url(
                image
            )

    images = product.get("images") or []

    if images:
        first = images[0]

        if isinstance(first, str):
            return normalize_image_url(
                first
            )

        if isinstance(first, dict):
            return normalize_image_url(
                first.get("src")
                or first.get("url")
            )

    return None


async def scrape_shopify_product(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    selected_variant_id = (
        query.get(
            "variant",
            [None],
        )[0]
    )

    json_url = shopify_product_json_url(
        url
    )

    print(
        "SHOPIFY TRY:",
        json_url,
        flush=True,
    )

    headers = dict(HEADERS)
    headers["Accept"] = (
        "application/json,"
        "text/javascript,*/*;q=0.8"
    )

    async with httpx.AsyncClient(
        headers=headers,
        timeout=10,
        follow_redirects=True,
    ) as client:
        response = await client.get(
            json_url
        )

        print(
            "SHOPIFY STATUS:",
            response.status_code,
            flush=True,
        )

        response.raise_for_status()

        try:
            product = response.json()
        except Exception:
            raise RuntimeError(
                "Shopify product.js JSON döndürmedi."
            )

    if not isinstance(product, dict):
        raise RuntimeError(
            "Shopify ürün verisi geçersiz."
        )

    variants_list = product.get(
        "variants"
    )

    if not isinstance(variants_list, list):
        raise RuntimeError(
            "Shopify variants bulunamadı."
        )

    if not variants_list:
        raise RuntimeError(
            "Shopify variants boş."
        )

    title = product.get(
        "title"
    )

    if not title:
        raise RuntimeError(
            "Shopify ürün adı bulunamadı."
        )

    selected = None

    if selected_variant_id:
        for variant in variants_list:
            if not isinstance(
                variant,
                dict,
            ):
                continue

            if (
                str(
                    variant.get("id")
                )
                ==
                str(
                    selected_variant_id
                )
            ):
                selected = variant
                break

    if selected is None:
        for variant in variants_list:
            if (
                isinstance(variant, dict)
                and variant.get("available")
            ):
                selected = variant
                break

    if selected is None:
        selected = variants_list[0]

    if not isinstance(selected, dict):
        raise RuntimeError(
            "Shopify seçili varyant geçersiz."
        )

    price = shopify_price(
        selected.get("price")
    )

    if price is None:
        price = shopify_price(
            product.get("price")
        )

    if price is None:
        raise RuntimeError(
            "Shopify varyant fiyatı bulunamadı."
        )

    variants = extract_shopify_variants(
        product,
        selected,
    )

    variant_text = make_variant_text(
        variants
    )

    image_url = extract_shopify_image(
        product,
        selected,
    )

    brand = product.get(
        "vendor"
    )

    variant_id = selected.get(
        "id"
    )

    store = detect_store(
        url
    )

    if not store:
        store = (
            parsed.netloc
            .replace("www.", "")
            .split(".")[0]
            .title()
        )

    print(
        "==========================",
        flush=True,
    )
    print(
        "SHOPIFY SUCCESS:",
        store,
        flush=True,
    )
    print(
        "SHOPIFY PRODUCT:",
        title,
        flush=True,
    )
    print(
        "SHOPIFY VARIANT ID:",
        variant_id,
        flush=True,
    )
    print(
        "SHOPIFY VARIANTS:",
        variants,
        flush=True,
    )
    print(
        "SHOPIFY PRICE:",
        price,
        flush=True,
    )
    print(
        "==========================",
        flush=True,
    )

    return ScrapedProduct(
        title=title[:500],
        store=store,
        url=url,
        price=price,
        image_url=image_url,
        brand=brand,
        model=(
            str(variant_id)
            if variant_id
            else None
        ),
        variants=variants,
        variant_text=variant_text,
        method="generic-shopify",
    )


# =========================================================
# NORMAL HTML
# =========================================================

def extract_html_data(html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    title = None
    price = None
    image_url = None
    brand = None
    model = None

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = (
            script.string
            or script.get_text(
                strip=True
            )
        )

        if not raw:
            continue

        try:
            payload = json.loads(raw)
        except Exception:
            continue

        product = walk_for_product(
            payload
        )

        if not product:
            continue

        if not title:
            title = product.get(
                "name"
            )

        image = product.get(
            "image"
        )

        if isinstance(image, str):
            image_url = image

        elif (
            isinstance(image, list)
            and image
        ):
            first = image[0]

            if isinstance(first, str):
                image_url = first

            elif isinstance(first, dict):
                image_url = (
                    first.get("url")
                    or first.get(
                        "contentUrl"
                    )
                )

        elif isinstance(image, dict):
            image_url = (
                image.get("url")
                or image.get(
                    "contentUrl"
                )
            )

        brand_data = product.get(
            "brand"
        )

        if isinstance(brand_data, dict):
            brand = brand_data.get(
                "name"
            )

        elif isinstance(brand_data, str):
            brand = brand_data

        model = (
            product.get("model")
            or product.get("sku")
            or product.get("mpn")
        )

        offers = product.get(
            "offers"
        )

        if isinstance(offers, dict):
            offers = [offers]

        if isinstance(offers, list):
            for offer in offers:
                if not isinstance(
                    offer,
                    dict,
                ):
                    continue

                candidate = clean_price(
                    offer.get("price")
                    or offer.get(
                        "lowPrice"
                    )
                    or offer.get(
                        "salePrice"
                    )
                    or offer.get(
                        "sellingPrice"
                    )
                )

                if candidate:
                    price = candidate
                    break

    if not title:
        for attrs in [
            {"property": "og:title"},
            {"name": "twitter:title"},
        ]:
            node = soup.find(
                "meta",
                attrs=attrs,
            )

            if node:
                value = node.get(
                    "content"
                )

                if value:
                    title = value.strip()
                    break

    if not image_url:
        for attrs in [
            {"property": "og:image"},
            {"name": "twitter:image"},
        ]:
            node = soup.find(
                "meta",
                attrs=attrs,
            )

            if node:
                value = node.get(
                    "content"
                )

                if value:
                    image_url = value
                    break

    if price is None:
        for selector in [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
        ]:
            node = soup.select_one(
                selector
            )

            if not node:
                continue

            candidate = clean_price(
                node.get("content")
                or node.get("value")
            )

            if candidate:
                price = candidate
                break

    if (
        not title
        and soup.title
    ):
        title = soup.title.get_text(
            " ",
            strip=True,
        )

    return {
        "title": title,
        "price": price,
        "image_url":
            normalize_image_url(
                image_url
            ),
        "brand": brand,
        "model": model,
    }


async def scrape_http(url):
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=HTTP_TIMEOUT,
    ) as client:
        response = await client.get(
            url
        )

        response.raise_for_status()

        final_url = str(
            response.url
        )

        data = extract_html_data(
            response.text
        )

        return (
            final_url,
            data,
        )


# =========================================================
# MEYER
# =========================================================

def get_meyer_raw_variants(url):
    query = parse_qs(
        urlparse(url).query
    )

    def get_value(name):
        value = query.get(
            name,
            [None],
        )[0]

        if value is None:
            return None

        return unquote(
            str(value)
        ).strip()

    return {
        "yuzey": get_value("yuzey"),
        "boyut": get_value("boyut"),
        "renk": get_value("renk"),
    }


def get_meyer_variants(url):
    raw = get_meyer_raw_variants(
        url
    )

    variants = {}

    mapping = {
        "yuzey": "Yüzey",
        "boyut": "Boyut",
        "renk": "Renk",
    }

    for key, value in raw.items():
        if not value:
            continue

        label = mapping[key]
        pretty = pretty_variant_value(
            label,
            value,
        )

        if pretty:
            variants[label] = pretty

    return variants


async def meyer_click_variant(
    page,
    value,
):
    if not value:
        return False

    target = normalize_text(
        value
    )

    aliases = {
        "siyah": [
            "siyah",
            "black",
        ],
        "turuncu": [
            "turuncu",
            "daidai orange",
            "orange",
        ],
        "soft": ["soft"],
        "xsoft": [
            "xsoft",
            "x-soft",
        ],
        "x soft": [
            "xsoft",
            "x-soft",
        ],
        "mid": ["mid"],
        "xl": ["xl"],
        "xxl": ["xxl"],
        "l": ["l"],
        "m": ["m"],
        "s": ["s"],
    }

    targets = aliases.get(
        target,
        [target],
    )

    result = await page.evaluate(
        """
        (targets) => {
            function norm(value) {
                return (value || "")
                    .toString()
                    .toLowerCase()
                    .replaceAll("ı","i")
                    .replaceAll("ğ","g")
                    .replaceAll("ü","u")
                    .replaceAll("ş","s")
                    .replaceAll("ö","o")
                    .replaceAll("ç","c")
                    .replace(/\\s+/g," ")
                    .trim();
            }

            const wanted =
                targets.map(norm);

            const nodes =
                Array.from(
                    document.querySelectorAll(
                        'button,[role="button"],label'
                    )
                );

            for (const node of nodes) {
                const rect =
                    node.getBoundingClientRect();

                if (
                    rect.width <= 0
                    || rect.height <= 0
                ) {
                    continue;
                }

                const values = [
                    node.innerText,
                    node.textContent,
                    node.getAttribute(
                        "aria-label"
                    ),
                    node.getAttribute(
                        "title"
                    ),
                    node.getAttribute(
                        "value"
                    ),
                    node.getAttribute(
                        "data-value"
                    ),
                    node.getAttribute(
                        "data-name"
                    ),
                ]
                .filter(Boolean)
                .map(norm);

                const matched =
                    wanted.some(
                        target =>
                            values.some(
                                value =>
                                    value === target
                            )
                    );

                if (!matched) {
                    continue;
                }

                node.scrollIntoView({
                    block:"center",
                    inline:"center"
                });

                node.click();

                return {
                    success:true,
                    text:
                        node.innerText
                        || node.textContent
                        || ""
                };
            }

            return {
                success:false
            };
        }
        """,
        targets,
    )

    if result.get("success"):
        print(
            "MEYER CLICKED:",
            result.get("text"),
            flush=True,
        )

        await page.wait_for_timeout(
            200
        )

        return True

    print(
        "MEYER OPTION NOT FOUND:",
        value,
        flush=True,
    )

    return False


async def meyer_get_title(
    page,
    original_url,
):
    try:
        result = await page.evaluate(
            """
            () => {
                const nodes =
                    Array.from(
                        document.querySelectorAll(
                            "h1"
                        )
                    );

                for (const node of nodes) {
                    const r =
                        node.getBoundingClientRect();

                    if (
                        r.width <= 0
                        || r.height <= 0
                    ) {
                        continue;
                    }

                    const text =
                        (
                            node.innerText
                            || node.textContent
                            || ""
                        ).trim();

                    if (text) {
                        return text;
                    }
                }

                return null;
            }
            """
        )

        if result:
            return result.strip()

    except Exception:
        pass

    try:
        title = await page.locator(
            'meta[property="og:title"]'
        ).get_attribute(
            "content",
            timeout=1000,
        )

        if title:
            return title.strip()

    except Exception:
        pass

    try:
        title = await page.title()

        if title:
            title = re.sub(
                r"\s*[-|]\s*Meyer.*$",
                "",
                title,
                flags=re.I,
            ).strip()

            if title:
                return title

    except Exception:
        pass

    try:
        path = urlparse(
            original_url
        ).path.strip("/")

        slug = path.split("/")[-1]
        slug = unquote(slug)
        slug = slug.replace("-", " ")
        slug = re.sub(
            r"\s+",
            " ",
            slug,
        ).strip()

        if slug:
            return slug.title()

    except Exception:
        pass

    return "Meyer Ürünü"


async def meyer_get_price(page):
    try:
        result = await page.evaluate(
            """
            () => {
                function visible(el) {
                    if (!el) {
                        return false;
                    }

                    const r =
                        el.getBoundingClientRect();

                    return (
                        r.width > 0
                        && r.height > 0
                    );
                }

                const h1 =
                    Array.from(
                        document.querySelectorAll(
                            "h1"
                        )
                    ).find(visible);

                const titleRect =
                    h1
                    ? h1.getBoundingClientRect()
                    : null;

                const selectors = [
                    "div.text-xl.font-medium",
                    ".text-xl.font-medium",
                    '[class*="product"] [class*="price"]',
                    '[class*="Product"] [class*="price"]'
                ];

                const results = [];
                const seen = new Set();

                for (
                    const selector
                    of selectors
                ) {
                    let nodes;

                    try {
                        nodes =
                            document.querySelectorAll(
                                selector
                            );
                    }
                    catch {
                        continue;
                    }

                    for (const el of nodes) {
                        if (
                            seen.has(el)
                            || !visible(el)
                        ) {
                            continue;
                        }

                        seen.add(el);

                        const text =
                            (
                                el.innerText
                                || el.textContent
                                || ""
                            ).trim();

                        if (
                            !/TL/i.test(text)
                            || !/\\d/.test(text)
                        ) {
                            continue;
                        }

                        const r =
                            el.getBoundingClientRect();

                        let score = 0;

                        if (
                            el.classList.contains(
                                "text-xl"
                            )
                        ) {
                            score += 2000;
                        }

                        if (
                            el.classList.contains(
                                "font-medium"
                            )
                        ) {
                            score += 1500;
                        }

                        if (titleRect) {
                            const ty =
                                titleRect.top
                                + titleRect.height / 2;

                            const py =
                                r.top
                                + r.height / 2;

                            const dy =
                                Math.abs(
                                    ty - py
                                );

                            score +=
                                Math.max(
                                    0,
                                    2000 - dy
                                );
                        }

                        results.push({
                            text,
                            score
                        });
                    }
                }

                results.sort(
                    (a,b) =>
                        b.score
                        - a.score
                );

                return (
                    results.length
                    ? results[0]
                    : null
                );
            }
            """
        )

        if result:
            text = result.get(
                "text"
            )

            price = clean_price(
                text
            )

            if price:
                print(
                    "MEYER PRICE ELEMENT:",
                    text,
                    flush=True,
                )

                return price

    except Exception as e:
        print(
            "MEYER PRICE ERROR:",
            repr(e),
            flush=True,
        )

    try:
        matches = await page.evaluate(
            """
            () => {
                const h1 =
                    document.querySelector(
                        "h1"
                    );

                let root = h1;

                for (
                    let i=0;
                    i<5 && root;
                    i++
                ) {
                    if (
                        root.innerText
                        &&
                        /\\d[\\d.,]*\\s*TL/i
                        .test(root.innerText)
                    ) {
                        break;
                    }

                    root =
                        root.parentElement;
                }

                const text =
                    root
                    ? root.innerText
                    : document.body.innerText;

                return (
                    text.match(
                        /\\d[\\d.,]*\\s*TL/gi
                    )
                    || []
                );
            }
            """
        )

        for text in matches[:15]:
            price = clean_price(
                text
            )

            if (
                price is not None
                and price > 20
            ):
                return price

    except Exception:
        pass

    return None


async def meyer_get_image(
    page,
    renk=None,
):
    color = normalize_text(
        renk
    )

    try:
        result = await page.evaluate(
            """
            (color) => {
                function visible(el) {
                    const r =
                        el.getBoundingClientRect();

                    return (
                        r.width > 0
                        && r.height > 0
                    );
                }

                const h1 =
                    Array.from(
                        document.querySelectorAll(
                            "h1"
                        )
                    ).find(visible);

                const titleRect =
                    h1
                    ? h1.getBoundingClientRect()
                    : null;

                const images =
                    Array.from(
                        document.querySelectorAll(
                            'img[alt="Image"],img'
                        )
                    );

                const results = [];

                for (const img of images) {
                    if (!visible(img)) {
                        continue;
                    }

                    const src =
                        img.currentSrc
                        || img.src
                        || img.getAttribute(
                            "data-src"
                        )
                        || "";

                    if (!src) {
                        continue;
                    }

                    if (
                        !src.includes(
                            "cdn.myikas.com"
                        )
                    ) {
                        continue;
                    }

                    const r =
                        img.getBoundingClientRect();

                    const area =
                        r.width * r.height;

                    if (area < 20000) {
                        continue;
                    }

                    let score =
                        Math.min(
                            area / 500,
                            1500
                        );

                    if (titleRect) {
                        const imageY =
                            r.top
                            + r.height / 2;

                        const titleY =
                            titleRect.top
                            + titleRect.height / 2;

                        const dy =
                            Math.abs(
                                imageY - titleY
                            );

                        score +=
                            Math.max(
                                0,
                                2000 - dy
                            );

                        if (
                            r.left
                            < titleRect.left
                        ) {
                            score += 700;
                        }
                    }

                    const lower =
                        src.toLowerCase();

                    if (
                        color === "siyah"
                    ) {
                        if (
                            lower.includes("-bk-")
                            || lower.includes("_bk_")
                            || lower.includes("black")
                            || lower.includes("siyah")
                        ) {
                            score += 5000;
                        }
                    }

                    if (
                        color === "turuncu"
                    ) {
                        if (
                            lower.includes("orange")
                            || lower.includes("daidai")
                            || lower.includes("-or-")
                        ) {
                            score += 5000;
                        }
                    }

                    results.push({
                        src,
                        score
                    });
                }

                results.sort(
                    (a,b) =>
                        b.score
                        - a.score
                );

                return (
                    results.length
                    ? results[0]
                    : null
                );
            }
            """,
            color,
        )

        if result:
            src = result.get("src")

            if src:
                print(
                    "MEYER IMAGE FOUND:",
                    src,
                    flush=True,
                )

                return src

    except Exception as e:
        print(
            "MEYER IMAGE ERROR:",
            repr(e),
            flush=True,
        )

    try:
        return await page.locator(
            'meta[property="og:image"]'
        ).get_attribute(
            "content",
            timeout=800,
        )

    except Exception:
        return None


async def scrape_meyer(
    page,
    original_url,
):
    raw_variants = (
        get_meyer_raw_variants(
            original_url
        )
    )

    variants = (
        get_meyer_variants(
            original_url
        )
    )

    print(
        "==========================",
        flush=True,
    )

    print(
        "MEYER VARIANTS:",
        variants,
        flush=True,
    )

    try:
        await page.wait_for_function(
            """
            () =>
                document.querySelector("h1")
                ||
                document.querySelectorAll(
                    "button"
                ).length > 3
            """,
            timeout=3500,
        )

    except Exception:
        print(
            "MEYER DOM WAIT TIMEOUT -> CONTINUE",
            flush=True,
        )

    if raw_variants.get("yuzey"):
        await meyer_click_variant(
            page,
            raw_variants["yuzey"],
        )

    if raw_variants.get("boyut"):
        await meyer_click_variant(
            page,
            raw_variants["boyut"],
        )

    if raw_variants.get("renk"):
        await meyer_click_variant(
            page,
            raw_variants["renk"],
        )

    await page.wait_for_timeout(
        400
    )

    title = await meyer_get_title(
        page,
        original_url,
    )

    price = await meyer_get_price(
        page
    )

    image_url = await meyer_get_image(
        page,
        raw_variants.get("renk"),
    )

    variant_text = make_variant_text(
        variants
    )

    print(
        "MEYER FINAL TITLE:",
        title,
        flush=True,
    )

    print(
        "MEYER FINAL PRICE:",
        price,
        flush=True,
    )

    print(
        "MEYER FINAL IMAGE:",
        image_url,
        flush=True,
    )

    print(
        "MEYER VARIANT TEXT:",
        variant_text,
        flush=True,
    )

    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "brand": None,
        "model": None,
        "variants": variants,
        "variant_text": variant_text,
    }


# =========================================================
# GENERIC BROWSER
# =========================================================

def get_price_selectors(store):
    if store == "Amazon Türkiye":
        return [
            ".priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            ".apexPriceToPay .a-offscreen",
            ".a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
        ]

    if store == "Trendyol":
        return [
            ".prc-dsc",
            ".prc-slg",
            '[data-testid*="price"]',
            '[class*="price"]',
        ]

    if store == "N11":
        return [
            ".newPrice ins",
            ".price",
            '[class*="price"]',
        ]

    return [
        '[itemprop="price"]',
        '[class*="price"]',
    ]


async def browser_find_price(
    page,
    store,
):
    selectors = get_price_selectors(
        store
    )

    try:
        results = await page.evaluate(
            """
            (selectors) => {
                const output = [];

                for (
                    let priority=0;
                    priority<selectors.length;
                    priority++
                ) {
                    const selector =
                        selectors[priority];

                    let nodes;

                    try {
                        nodes =
                            document.querySelectorAll(
                                selector
                            );
                    }
                    catch {
                        continue;
                    }

                    for (
                        let i=0;
                        i<Math.min(
                            nodes.length,
                            25
                        );
                        i++
                    ) {
                        const node =
                            nodes[i];

                        const r =
                            node.getBoundingClientRect();

                        if (
                            r.width <= 0
                            || r.height <= 0
                        ) {
                            continue;
                        }

                        const text =
                            (
                                node.innerText
                                || node.textContent
                                || node.getAttribute(
                                    "content"
                                )
                                || ""
                            ).trim();

                        if (
                            !text
                            || !/\\d/.test(text)
                        ) {
                            continue;
                        }

                        output.push({
                            text,
                            priority
                        });
                    }
                }

                output.sort(
                    (a,b) =>
                        a.priority
                        - b.priority
                );

                return output;
            }
            """,
            selectors,
        )

        for item in results:
            price = clean_price(
                item.get("text")
            )

            if price:
                return price

    except Exception:
        pass

    return None


async def browser_find_title(page):
    try:
        title = await page.evaluate(
            """
            () => {
                const selectors = [
                    "#productTitle",
                    "h1",
                    '[data-test-id="product-name"]',
                    '[data-testid="product-name"]'
                ];

                for (
                    const selector
                    of selectors
                ) {
                    const nodes =
                        document.querySelectorAll(
                            selector
                        );

                    for (
                        const node
                        of nodes
                    ) {
                        const r =
                            node.getBoundingClientRect();

                        if (
                            r.width <= 0
                            || r.height <= 0
                        ) {
                            continue;
                        }

                        const text =
                            (
                                node.innerText
                                || node.textContent
                                || ""
                            ).trim();

                        if (text) {
                            return text;
                        }
                    }
                }

                const meta =
                    document.querySelector(
                        'meta[property="og:title"]'
                    );

                if (
                    meta
                    && meta.content
                ) {
                    return meta.content;
                }

                return (
                    document.title
                    || null
                );
            }
            """
        )

        if title:
            return title.strip()

    except Exception:
        pass

    return None


async def browser_find_image(page):
    try:
        result = await page.evaluate(
            """
            () => {
                const image =
                    document.querySelector(
                        "#landingImage"
                    )
                    ||
                    document.querySelector(
                        'img[itemprop="image"]'
                    );

                if (image) {
                    const src =
                        image.currentSrc
                        || image.src
                        || image.getAttribute(
                            "data-src"
                        );

                    if (src) {
                        return src;
                    }
                }

                const meta =
                    document.querySelector(
                        'meta[property="og:image"]'
                    );

                if (
                    meta
                    && meta.content
                ) {
                    return meta.content;
                }

                return null;
            }
            """
        )

        return normalize_image_url(
            result
        )

    except Exception:
        return None


async def browser_find_variants(page):
    try:
        data = await page.evaluate(
            """
            () => {
                const result = [];

                function clean(v) {
                    return (
                        v || ""
                    )
                    .replace(/\\s+/g," ")
                    .trim();
                }

                for (
                    const select
                    of document.querySelectorAll(
                        "select"
                    )
                ) {
                    const option =
                        select.options[
                            select.selectedIndex
                        ];

                    if (!option) {
                        continue;
                    }

                    const value =
                        clean(
                            option.textContent
                        );

                    if (
                        !value
                        || value.toLowerCase()
                        === "seçiniz"
                    ) {
                        continue;
                    }

                    let label = "";

                    if (select.id) {
                        const node =
                            document.querySelector(
                                `label[for="${select.id}"]`
                            );

                        if (node) {
                            label =
                                clean(
                                    node.textContent
                                );
                        }
                    }

                    label =
                        label
                        || select.getAttribute(
                            "aria-label"
                        )
                        || select.name
                        || "";

                    result.push({
                        label,
                        value
                    });
                }

                for (
                    const input
                    of document.querySelectorAll(
                        'input[type="radio"]:checked'
                    )
                ) {
                    let value =
                        input.value || "";

                    let label = "";

                    if (input.id) {
                        const node =
                            document.querySelector(
                                `label[for="${input.id}"]`
                            );

                        if (node) {
                            value =
                                clean(
                                    node.textContent
                                )
                                || value;
                        }
                    }

                    const fieldset =
                        input.closest(
                            "fieldset"
                        );

                    if (fieldset) {
                        const legend =
                            fieldset.querySelector(
                                "legend"
                            );

                        if (legend) {
                            label =
                                clean(
                                    legend.textContent
                                );
                        }
                    }

                    label =
                        label
                        || input.name
                        || "";

                    result.push({
                        label,
                        value
                    });
                }

                return result;
            }
            """
        )

    except Exception:
        return {}

    variants = {}

    allowed_keywords = {
        "renk": "Renk",
        "color": "Renk",
        "colour": "Renk",
        "boyut": "Boyut",
        "size": "Boyut",
        "olcu": "Ölçü",
        "dimension": "Ölçü",
        "beden": "Beden",
        "yuzey": "Yüzey",
        "surface": "Yüzey",
        "switch": "Switch",
        "layout": "Layout",
        "dongle": "Dongle",
        "kapasite": "Kapasite",
        "capacity": "Kapasite",
    }

    for index, item in enumerate(data):
        label_raw = item.get("label")
        value = item.get("value")

        label_norm = normalize_text(
            label_raw
        )

        label = None

        for key, pretty in (
            allowed_keywords.items()
        ):
            if key in label_norm:
                label = pretty
                break

        if not label:
            inferred = infer_variant_label(
                label_raw,
                value,
                index,
            )

            if inferred.startswith(
                "Seçenek"
            ):
                continue

            label = inferred

        pretty_value = pretty_variant_value(
            label,
            value,
        )

        if pretty_value:
            variants[label] = pretty_value

    return variants


async def amazon_body_price(page):
    try:
        body = (
            await page.locator(
                "body"
            ).text_content(
                timeout=1800
            )
            or ""
        )

        matches = re.findall(
            r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*TL",
            body,
            re.I,
        )

        parsed = [
            clean_price(x)
            for x in matches
        ]

        parsed = [
            x
            for x in parsed
            if x is not None
        ]

        if not parsed:
            return None

        counts = {}

        for value in parsed:
            counts[value] = (
                counts.get(
                    value,
                    0,
                )
                + 1
            )

        return sorted(
            counts.items(),
            key=lambda x: (
                -x[1],
                x[0],
            ),
        )[0][0]

    except Exception:
        return None


async def scrape_browser(
    url,
    retry=True,
):
    browser = await get_browser()
    context = None
    page = None

    try:
        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=HEADERS["User-Agent"],
            viewport={
                "width": 1365,
                "height": 900,
            },
            extra_http_headers={
                "Accept-Language":
                    "tr-TR,tr;q=0.9,en;q=0.8"
            },
        )

        page = await context.new_page()

        async def route_handler(route):
            resource_type = (
                route.request.resource_type
            )

            if resource_type in {
                "font",
                "media",
            }:
                await route.abort()
            else:
                await route.continue_()

        await page.route(
            "**/*",
            route_handler,
        )

        print(
            "BROWSER GOTO:",
            url,
            flush=True,
        )

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_GOTO_TIMEOUT,
            )

        except PlaywrightTimeoutError as e:
            print(
                "BROWSER GOTO TIMEOUT -> CONTINUE:",
                repr(e),
                flush=True,
            )

            try:
                await page.wait_for_timeout(
                    1000
                )
            except Exception:
                pass

        final_url = page.url or url
        store = detect_store(
            final_url
        )

        if not store:
            store = detect_store(
                url
            )

        print(
            "BROWSER STORE:",
            store,
            flush=True,
        )

        if (
            store == "Meyer"
            or "meyergaming.com"
            in url.lower()
        ):
            data = await scrape_meyer(
                page,
                url,
            )

            return (
                final_url,
                data,
            )

        html = await page.content()
        data = extract_html_data(
            html
        )

        if not data.get("title"):
            data["title"] = (
                await browser_find_title(
                    page
                )
            )

        if data.get("price") is None:
            data["price"] = (
                await browser_find_price(
                    page,
                    store,
                )
            )

        if (
            store == "Amazon Türkiye"
            and data.get("price")
            is None
        ):
            data["price"] = (
                await amazon_body_price(
                    page
                )
            )

        if not data.get("image_url"):
            data["image_url"] = (
                await browser_find_image(
                    page
                )
            )

        variants = query_variants(
            url
        )

        try:
            dom_variants = (
                await browser_find_variants(
                    page
                )
            )

            for key, value in (
                dom_variants.items()
            ):
                variants[key] = value

        except Exception:
            pass

        data["variants"] = variants
        data["variant_text"] = (
            make_variant_text(
                variants
            )
        )

        print(
            "BROWSER FINAL TITLE:",
            data.get("title"),
            flush=True,
        )
        print(
            "BROWSER FINAL PRICE:",
            data.get("price"),
            flush=True,
        )
        print(
            "BROWSER FINAL VARIANT:",
            data.get("variant_text"),
            flush=True,
        )

        return (
            final_url,
            data,
        )

    except Exception as e:
        print(
            "BROWSER ERROR:",
            repr(e),
            flush=True,
        )

        message = str(e).lower()

        browser_dead = (
            "browser has been closed"
            in message
            or "target page" in message
            or "browser closed" in message
            or "connection closed" in message
        )

        if retry and browser_dead:
            print(
                "BROWSER RESET + RETRY",
                flush=True,
            )

            await reset_browser()

            return await scrape_browser(
                url,
                retry=False,
            )

        raise

    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


# =========================================================
# ANA SCRAPER
# =========================================================

def _normalize_input_url(url):
    """Kullanıcının yapıştırdığı ürün linkini güvenli biçimde normalize eder.

    - baş/son boşlukları temizler
    - <https://...> biçimini açar
    - Markdown [başlık](https://...) içinden URL'yi alır
    - www.amazon.com.tr/... veya amazon.com.tr/... gibi şemasız linklere https:// ekler
    """
    value = str(url or "").strip()

    markdown = re.search(r"\((https?://[^)]+)\)", value)
    if markdown:
        value = markdown.group(1).strip()

    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()

    value = value.replace("\\&", "&")

    if value.startswith("//"):
        value = "https:" + value
    elif not re.match(r"^https?://", value, re.I):
        # Bir domain/link gibi görünüyorsa kullanıcıdan http(s) yazmasını bekleme.
        if re.match(r"^(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}(?:/|$)", value, re.I):
            value = "https://" + value

    return value


async def scrape_product(url):
    url = _normalize_input_url(url)

    if not re.match(r"^https?://", url, re.I):
        raise ValueError(
            "Geçerli bir ürün linki gir."
        )

    store = detect_store(
        url
    )

    print(
        "=================================",
        flush=True,
    )
    print(
        "SCRAPE START:",
        store,
        flush=True,
    )

    # =====================================================
    # HEPSIBURADA
    # =====================================================

    if store == "Hepsiburada":
        print(
            "HEPSIBURADA -> PARSE API + BASKET DETECTOR",
            flush=True,
        )

        return await scrape_hepsiburada_api(
            url
        )

    # =====================================================
    # MEYER
    # =====================================================

    if (
        store == "Meyer"
        or "meyergaming.com"
        in url.lower()
    ):
        print(
            "MEYER -> FAST VARIANT BROWSER",
            flush=True,
        )

        final_url, data = (
            await scrape_browser(
                url
            )
        )

        title = (
            data.get("title")
            or "Meyer Ürünü"
        )

        price = data.get(
            "price"
        )

        if price is None:
            raise RuntimeError(
                "Meyer ürün fiyatı alınamadı."
            )

        return ScrapedProduct(
            title=title[:500],
            store="Meyer",
            url=url,
            price=price,
            image_url=data.get(
                "image_url"
            ),
            brand=data.get(
                "brand"
            ),
            model=data.get(
                "model"
            ),
            variants=data.get(
                "variants"
            ),
            variant_text=data.get(
                "variant_text"
            ),
            method="meyer-fast-browser",
        )

    # =====================================================
    # GENEL SHOPIFY
    # =====================================================

    if looks_like_shopify_product_url(
        url
    ):
        try:
            result = (
                await scrape_shopify_product(
                    url
                )
            )

            print(
                "GENERIC SHOPIFY SUCCESS",
                flush=True,
            )

            return result

        except Exception as e:
            print(
                "NOT SHOPIFY OR SHOPIFY FAILED -> NORMAL FLOW:",
                repr(e),
                flush=True,
            )

    # =====================================================
    # NORMAL HTTP
    # =====================================================

    http_error = None

    try:
        final_url, data = (
            await scrape_http(
                url
            )
        )

        if (
            data.get("price")
            is not None
            and data.get("title")
        ):
            detected = detect_store(
                final_url
            )

            variants = query_variants(
                url
            )

            print(
                "HTTP SUCCESS:",
                detected,
                data["price"],
                flush=True,
            )

            return ScrapedProduct(
                title=data[
                    "title"
                ][:500],
                store=detected,
                url=url,
                price=data[
                    "price"
                ],
                image_url=data.get(
                    "image_url"
                ),
                brand=data.get(
                    "brand"
                ),
                model=data.get(
                    "model"
                ),
                variants=variants,
                variant_text=make_variant_text(
                    variants
                ),
                method="http",
            )

        print(
            "HTTP DATA INCOMPLETE -> BROWSER",
            flush=True,
        )

    except Exception as e:
        http_error = e

        print(
            "HTTP ERROR -> BROWSER:",
            repr(e),
            flush=True,
        )

    # =====================================================
    # BROWSER FALLBACK
    # =====================================================

    try:
        final_url, data = (
            await scrape_browser(
                url
            )
        )

        title = (
            data.get("title")
            or "Ürün"
        )

        price = data.get(
            "price"
        )

        if price is None:
            raise RuntimeError(
                "Sayfa açıldı fakat fiyat bulunamadı."
            )

        return ScrapedProduct(
            title=title[:500],
            store=detect_store(
                final_url
            ),
            url=url,
            price=price,
            image_url=data.get(
                "image_url"
            ),
            brand=data.get(
                "brand"
            ),
            model=data.get(
                "model"
            ),
            variants=data.get(
                "variants"
            ),
            variant_text=data.get(
                "variant_text"
            ),
            method="browser",
        )

    except Exception as browser_error:
        print(
            "FINAL BROWSER ERROR:",
            repr(browser_error),
            flush=True,
        )

        if http_error:
            raise RuntimeError(
                f"HTTP başarısız ({http_error}); "
                f"tarayıcı da başarısız "
                f"({browser_error})"
            )

        raise RuntimeError(
            f"Tarayıcı başarısız "
            f"({browser_error})"
        )
