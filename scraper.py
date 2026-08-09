import asyncio
import json
import os
import re

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from urllib.parse import (
    urlparse,
    parse_qs,
    unquote,
)

import httpx

from bs4 import BeautifulSoup

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from utils import (
    detect_store,
    parse_price,
)


# =========================================================
# AYARLAR
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/124.0.0.0 "
        "Safari/537.36"
    ),

    "Accept-Language":
        "tr-TR,tr;q=0.9,en;q=0.8",

    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
}


HTTP_TIMEOUT = httpx.Timeout(
    connect=4.0,
    read=6.0,
    write=6.0,
    pool=4.0,
)


BROWSER_GOTO_TIMEOUT = (
    25000
)


# =========================================================
# GLOBAL CHROMIUM
# =========================================================

_playwright_instance = None
_browser_instance = None
_browser_lock = None


def get_browser_lock():
    global _browser_lock

    if _browser_lock is None:
        _browser_lock = (
            asyncio.Lock()
        )

    return _browser_lock


async def get_browser():
    global _playwright_instance
    global _browser_instance

    if (
        _browser_instance
        is not None
        and
        _browser_instance
        .is_connected()
    ):
        return _browser_instance

    lock = get_browser_lock()

    async with lock:

        if (
            _browser_instance
            is not None
            and
            _browser_instance
            .is_connected()
        ):
            return _browser_instance

        print(
            "BROWSER STARTING..."
        )

        if (
            _playwright_instance
            is None
        ):
            _playwright_instance = (
                await async_playwright()
                .start()
            )

        _browser_instance = (
            await
            _playwright_instance
            .chromium
            .launch(
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
        )

        print(
            "BROWSER READY"
        )

        return _browser_instance


async def reset_browser():
    global _browser_instance

    if (
        _browser_instance
        is not None
    ):
        try:
            await (
                _browser_instance
                .close()
            )

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

    price: Optional[
        float
    ] = None

    image_url: Optional[
        str
    ] = None

    brand: Optional[
        str
    ] = None

    model: Optional[
        str
    ] = None

    variants: Optional[
        dict
    ] = None

    variant_text: Optional[
        str
    ] = None

    method: str = "unknown"


# =========================================================
# TEXT NORMALIZATION
# =========================================================

def normalize_text(text):
    text = str(
        text or ""
    ).lower()

    replacements = {
        "ı": "i",
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ö": "o",
        "ç": "c",
    }

    for old, new in (
        replacements.items()
    ):
        text = text.replace(
            old,
            new,
        )

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def similarity(
    a,
    b,
):
    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


# =========================================================
# VARIANT HELPERS
# =========================================================

def pretty_variant_value(
    key,
    value,
):
    value = unquote(
        str(value or "")
    ).strip()

    if not value:
        return None

    normalized = (
        normalize_text(value)
    )

    # Boyut / yüzey isimleri.
    uppercase_values = {
        "soft",
        "xsoft",
        "x-soft",
        "mid",
        "xl",
        "xxl",
        "xxxl",
        "s",
        "m",
        "l",
    }

    if normalized in uppercase_values:
        return value.upper()

    # Renk.
    if normalized == "siyah":
        return "Siyah"

    if normalized == "beyaz":
        return "Beyaz"

    if normalized == "turuncu":
        return "Turuncu"

    # Ölçü formatını bozma.
    if re.search(
        r"\d+\s*[xX×]\s*\d+",
        value,
    ):
        return value

    # Shopify zaten düzgün isim döndürüyorsa koru.
    if (
        any(
            char.isupper()
            for char in value
        )
    ):
        return value

    return value.capitalize()


def make_variant_text(
    variants,
):
    if not variants:
        return None

    parts = []

    for key, value in (
        variants.items()
    ):
        if value is None:
            continue

        value = str(
            value
        ).strip()

        if not value:
            continue

        parts.append(
            f"{key}: {value}"
        )

    if not parts:
        return None

    return " • ".join(
        parts
    )


def query_variants(
    url,
):
    """
    Diğer mağazalarda URL içinde
    bariz varyant bilgisi varsa yakala.
    """

    query = parse_qs(
        urlparse(url).query
    )

    mapping = {
        "yuzey": "Yüzey",
        "surface": "Yüzey",

        "boyut": "Boyut",
        "size": "Boyut",

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

    for raw_key, values in (
        query.items()
    ):
        key = normalize_text(
            raw_key
        )

        label = mapping.get(
            key
        )

        if not label:
            continue

        if not values:
            continue

        value = pretty_variant_value(
            key,
            values[0],
        )

        if value:
            result[label] = (
                value
            )

    return result


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
    229.00 -> 229
    """

    if value is None:
        return None

    if isinstance(
        value,
        (int, float),
    ):
        number = float(
            value
        )

        if (
            0
            < number
            < 100_000_000
        ):
            return number

        return None

    text = str(
        value
    ).strip()

    if not text:
        return None

    match = re.search(
        r"\d[\d.,]*",
        text,
    )

    if not match:
        try:
            return parse_price(
                text
            )

        except Exception:
            return None

    raw = match.group(0)

    try:

        if (
            "." in raw
            and
            "," in raw
        ):
            if (
                raw.rfind(",")
                >
                raw.rfind(".")
            ):
                raw = (
                    raw
                    .replace(
                        ".",
                        "",
                    )
                    .replace(
                        ",",
                        ".",
                    )
                )

            else:
                raw = (
                    raw.replace(
                        ",",
                        "",
                    )
                )

        elif "," in raw:
            parts = raw.split(
                ","
            )

            if (
                len(parts) == 2
                and
                len(parts[-1])
                in (1, 2)
            ):
                raw = (
                    raw.replace(
                        ",",
                        ".",
                    )
                )

            else:
                raw = (
                    raw.replace(
                        ",",
                        "",
                    )
                )

        elif "." in raw:
            parts = raw.split(
                "."
            )

            if (
                len(parts) >= 2
                and
                all(
                    len(part) == 3
                    for part
                    in parts[1:]
                )
            ):
                raw = (
                    raw.replace(
                        ".",
                        "",
                    )
                )

        number = float(
            raw
        )

        if (
            0
            < number
            < 100_000_000
        ):
            return number

    except Exception:
        pass

    try:
        return parse_price(
            text
        )

    except Exception:
        return None


# =========================================================
# JSON LD
# =========================================================

def walk_for_product(data):
    if isinstance(
        data,
        dict,
    ):
        product_type = (
            data.get("@type")
        )

        if (
            product_type
            == "Product"
        ):
            return data

        if (
            isinstance(
                product_type,
                list,
            )
            and
            "Product"
            in product_type
        ):
            return data

        for value in (
            data.values()
        ):
            result = (
                walk_for_product(
                    value
                )
            )

            if result:
                return result

    elif isinstance(
        data,
        list,
    ):
        for item in data:
            result = (
                walk_for_product(
                    item
                )
            )

            if result:
                return result

    return None


# =========================================================
# HEPSIBURADA
# =========================================================

def extract_hepsiburada_code(
    url,
):
    match = re.search(
        r"(HBCV[A-Z0-9]+|HBC[A-Z0-9]+)",
        url,
        re.I,
    )

    if match:
        return (
            match.group(1)
            .upper()
        )

    return None


async def scrape_hepsiburada_api(
    url,
):
    api_key = os.getenv(
        "PARSE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "PARSE_API_KEY bulunamadı."
        )

    product_code = (
        extract_hepsiburada_code(
            url
        )
    )

    if not product_code:
        raise RuntimeError(
            "Hepsiburada ürün kodu bulunamadı."
        )

    base_url = (
        "https://api.parse.bot/"
        "scraper/"
        "a42a78b8-347f-4c69-"
        "8e83-135320d2b001"
    )

    headers = {
        "X-API-Key":
            api_key,

        "Accept":
            "application/json",
    }

    async with (
        httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
        )
    ) as client:

        response = (
            await client.get(
                f"{base_url}"
                "/get_product_details",

                headers=headers,

                params={
                    "url": url
                },
            )
        )

        print(
            "HEPSIBURADA DETAILS STATUS:",
            response.status_code,
        )

        response.raise_for_status()

        payload = response.json()

        data = payload.get(
            "data",
            payload,
        )

        if isinstance(
            data,
            list,
        ):
            if not data:
                raise RuntimeError(
                    "Hepsiburada API boş cevap verdi."
                )

            data = data[0]

        if not isinstance(
            data,
            dict,
        ):
            raise RuntimeError(
                "Hepsiburada API cevabı geçersiz."
            )

        title = (
            data.get(
                "product_name"
            )
            or data.get(
                "name"
            )
            or data.get(
                "title"
            )
        )

        price = clean_price(
            data.get(
                "unit_price"
            )
            or data.get(
                "price"
            )
            or data.get(
                "current_price"
            )
            or data.get(
                "sale_price"
            )
        )

        brand = (
            data.get(
                "brand"
            )
            or data.get(
                "brand_name"
            )
        )

        model = (
            data.get(
                "sku"
            )
            or data.get(
                "productId"
            )
            or data.get(
                "product_id"
            )
            or product_code
        )

        if not title:
            raise RuntimeError(
                "Hepsiburada ürün adı alınamadı."
            )

        if price is None:
            raise RuntimeError(
                "Hepsiburada fiyat alınamadı."
            )

        image_url = (
            data.get(
                "imageUrl"
            )
            or data.get(
                "image_url"
            )
            or data.get(
                "image"
            )
        )

        # Details endpoint
        # resim döndürmezse search.
        if not image_url:
            try:
                search_response = (
                    await client.get(
                        f"{base_url}"
                        "/search_products",

                        headers=headers,

                        params={
                            "page": 1,
                            "query": title,
                        },
                    )
                )

                if (
                    search_response
                    .status_code
                    == 200
                ):
                    search_payload = (
                        search_response
                        .json()
                    )

                    search_data = (
                        search_payload
                        .get(
                            "data",
                            search_payload,
                        )
                    )

                    products = []

                    if isinstance(
                        search_data,
                        dict,
                    ):
                        products = (
                            search_data
                            .get(
                                "products",
                                [],
                            )
                        )

                    best_item = None
                    best_score = -1

                    for item in products:

                        if not isinstance(
                            item,
                            dict,
                        ):
                            continue

                        item_name = (
                            item.get(
                                "name"
                            )
                            or item.get(
                                "product_name"
                            )
                            or ""
                        )

                        score = (
                            similarity(
                                title,
                                item_name,
                            )
                            * 50
                        )

                        item_price = (
                            clean_price(
                                item.get(
                                    "price"
                                )
                            )
                        )

                        if (
                            item_price
                            is not None
                            and
                            abs(
                                item_price
                                - price
                            )
                            < 0.01
                        ):
                            score += 30

                        if (
                            score
                            > best_score
                        ):
                            best_score = (
                                score
                            )

                            best_item = (
                                item
                            )

                    if best_item:
                        image_url = (
                            best_item.get(
                                "imageUrl"
                            )
                            or best_item.get(
                                "image_url"
                            )
                            or best_item.get(
                                "image"
                            )
                        )

            except Exception as e:
                print(
                    "HEPSIBURADA IMAGE ERROR:",
                    repr(e),
                )

        variants = query_variants(
            url
        )

        variant_text = (
            make_variant_text(
                variants
            )
        )

        return ScrapedProduct(
            title=title[:500],

            store="Hepsiburada",

            url=url,

            price=price,

            image_url=image_url,

            brand=brand,

            model=model,

            variants=variants,

            variant_text=variant_text,

            method="parse-api",
        )


# =========================================================
# WRAITH / SHOPIFY
# =========================================================

def is_wraith(
    store,
    url="",
):
    text = normalize_text(
        f"{store} {url}"
    )

    return (
        "wraith" in text
    )


def shopify_price(
    value,
):
    if value is None:
        return None

    # product.js genelde kuruş/cents
    # şeklinde integer döndürür.
    if isinstance(
        value,
        int,
    ):
        return (
            float(value)
            / 100
        )

    if isinstance(
        value,
        float,
    ):
        return (
            value / 100
            if value > 10000
            else value
        )

    text = str(
        value
    ).strip()

    if re.fullmatch(
        r"\d+",
        text,
    ):
        return (
            float(text)
            / 100
        )

    return clean_price(
        text
    )


async def scrape_wraith_shopify(
    url,
):
    parsed = urlparse(
        url
    )

    query = parse_qs(
        parsed.query
    )

    variant_id = (
        query.get(
            "variant",
            [None],
        )[0]
    )

    # Product .js endpoint.
    clean_path = (
        parsed.path.rstrip("/")
    )

    js_url = (
        f"{parsed.scheme}"
        f"://{parsed.netloc}"
        f"{clean_path}.js"
    )

    print(
        "WRAITH SHOPIFY:",
        js_url,
    )

    async with (
        httpx.AsyncClient(
            headers=HEADERS,
            timeout=8,
            follow_redirects=True,
        )
    ) as client:

        response = (
            await client.get(
                js_url
            )
        )

        response.raise_for_status()

        product = (
            response.json()
        )

    if not isinstance(
        product,
        dict,
    ):
        raise RuntimeError(
            "Wraith ürün JSON'u okunamadı."
        )

    title = product.get(
        "title"
    )

    brand = (
        product.get(
            "vendor"
        )
    )

    variants_list = (
        product.get(
            "variants"
        )
        or []
    )

    selected = None

    # URL exact variant içeriyorsa
    # onu bul.
    if variant_id:
        for variant in (
            variants_list
        ):
            if (
                str(
                    variant.get(
                        "id"
                    )
                )
                ==
                str(
                    variant_id
                )
            ):
                selected = variant
                break

    # Variant yoksa ilk available.
    if selected is None:
        for variant in (
            variants_list
        ):
            if variant.get(
                "available"
            ):
                selected = variant
                break

    if (
        selected is None
        and variants_list
    ):
        selected = (
            variants_list[0]
        )

    if selected is None:
        raise RuntimeError(
            "Wraith varyantı bulunamadı."
        )

    price = shopify_price(
        selected.get(
            "price"
        )
    )

    if price is None:
        raise RuntimeError(
            "Wraith varyant fiyatı alınamadı."
        )

    # =====================================================
    # VARIANT LABELS
    # =====================================================

    option_names = []

    raw_options = (
        product.get(
            "options"
        )
        or []
    )

    for index, option in enumerate(
        raw_options
    ):
        if isinstance(
            option,
            dict,
        ):
            name = (
                option.get(
                    "name"
                )
                or
                f"Seçenek {index + 1}"
            )

        else:
            name = str(
                option
            )

        option_names.append(
            name
        )

    option_values = (
        selected.get(
            "options"
        )
    )

    if not isinstance(
        option_values,
        list,
    ):
        option_values = [
            selected.get(
                "option1"
            ),

            selected.get(
                "option2"
            ),

            selected.get(
                "option3"
            ),
        ]

    variants = {}

    for index, value in enumerate(
        option_values
    ):
        if value is None:
            continue

        value = str(
            value
        ).strip()

        if (
            not value
            or
            normalize_text(
                value
            )
            == "default title"
        ):
            continue

        if index < len(
            option_names
        ):
            name = (
                option_names[
                    index
                ]
            )

        else:
            name = (
                f"Seçenek {index + 1}"
            )

        # Türkçeleştirme / düzeltme.
        name_norm = (
            normalize_text(
                name
            )
        )

        label_map = {
            "surface":
                "Yüzey",

            "yuzey":
                "Yüzey",

            "size":
                "Boyut",

            "boyut":
                "Boyut",

            "color":
                "Renk",

            "colour":
                "Renk",

            "renk":
                "Renk",
        }

        label = (
            label_map.get(
                name_norm,
                name,
            )
        )

        pretty = (
            pretty_variant_value(
                label,
                value,
            )
        )

        if pretty:
            variants[
                label
            ] = pretty

    variant_text = (
        make_variant_text(
            variants
        )
    )

    # =====================================================
    # IMAGE
    # =====================================================

    image_url = None

    featured = (
        selected.get(
            "featured_image"
        )
    )

    if isinstance(
        featured,
        dict,
    ):
        image_url = (
            featured.get(
                "src"
            )
            or featured.get(
                "url"
            )
        )

    elif isinstance(
        featured,
        str,
    ):
        image_url = featured

    if not image_url:
        media = (
            selected.get(
                "featured_media"
            )
        )

        if isinstance(
            media,
            dict,
        ):
            preview_image = (
                media.get(
                    "preview_image"
                )
            )

            if isinstance(
                preview_image,
                dict,
            ):
                image_url = (
                    preview_image.get(
                        "src"
                    )
                )

    if not image_url:
        product_image = (
            product.get(
                "featured_image"
            )
        )

        if isinstance(
            product_image,
            str,
        ):
            image_url = (
                product_image
            )

        elif isinstance(
            product_image,
            dict,
        ):
            image_url = (
                product_image.get(
                    "src"
                )
            )

    if not image_url:
        images = (
            product.get(
                "images"
            )
            or []
        )

        if images:
            first = images[0]

            if isinstance(
                first,
                str,
            ):
                image_url = first

            elif isinstance(
                first,
                dict,
            ):
                image_url = (
                    first.get(
                        "src"
                    )
                )

    print(
        "WRAITH VARIANTS:",
        variants,
    )

    print(
        "WRAITH PRICE:",
        price,
    )

    return ScrapedProduct(
        title=(
            title
            or "Wraith Ürünü"
        )[:500],

        store="Wraith",

        # URL'yi aynen koru.
        # variant ID kaybolmasın.
        url=url,

        price=price,

        image_url=image_url,

        brand=brand,

        model=str(
            selected.get(
                "id"
            )
            or ""
        ),

        variants=variants,

        variant_text=variant_text,

        method="wraith-shopify",
    )


# =========================================================
# NORMAL HTML
# =========================================================

def extract_html_data(
    html,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    title = None
    price = None
    image_url = None
    brand = None
    model = None

    for script in (
        soup.find_all(
            "script",
            type="application/ld+json",
        )
    ):
        raw = (
            script.string
            or
            script.get_text(
                strip=True
            )
        )

        if not raw:
            continue

        try:
            payload = json.loads(
                raw
            )

        except Exception:
            continue

        product = (
            walk_for_product(
                payload
            )
        )

        if not product:
            continue

        if not title:
            title = (
                product.get(
                    "name"
                )
            )

        image = (
            product.get(
                "image"
            )
        )

        if isinstance(
            image,
            str,
        ):
            image_url = image

        elif (
            isinstance(
                image,
                list,
            )
            and image
        ):
            first = image[0]

            if isinstance(
                first,
                str,
            ):
                image_url = first

            elif isinstance(
                first,
                dict,
            ):
                image_url = (
                    first.get(
                        "url"
                    )
                    or first.get(
                        "contentUrl"
                    )
                )

        elif isinstance(
            image,
            dict,
        ):
            image_url = (
                image.get(
                    "url"
                )
                or image.get(
                    "contentUrl"
                )
            )

        brand_data = (
            product.get(
                "brand"
            )
        )

        if isinstance(
            brand_data,
            dict,
        ):
            brand = (
                brand_data.get(
                    "name"
                )
            )

        elif isinstance(
            brand_data,
            str,
        ):
            brand = brand_data

        model = (
            product.get(
                "model"
            )
            or product.get(
                "sku"
            )
            or product.get(
                "mpn"
            )
        )

        offers = (
            product.get(
                "offers"
            )
        )

        if isinstance(
            offers,
            dict,
        ):
            offers = [
                offers
            ]

        if isinstance(
            offers,
            list,
        ):
            for offer in offers:

                if not isinstance(
                    offer,
                    dict,
                ):
                    continue

                candidate = (
                    clean_price(
                        offer.get(
                            "price"
                        )
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
                )

                if candidate:
                    price = candidate
                    break

    if not title:

        node = (
            soup.find(
                "meta",
                property="og:title",
            )
        )

        if node:
            title = (
                node.get(
                    "content"
                )
            )

    if not image_url:

        node = (
            soup.find(
                "meta",
                property="og:image",
            )
        )

        if node:
            image_url = (
                node.get(
                    "content"
                )
            )

    if price is None:

        for selector in [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
        ]:
            node = (
                soup.select_one(
                    selector
                )
            )

            if not node:
                continue

            candidate = (
                clean_price(
                    node.get(
                        "content"
                    )
                    or node.get(
                        "value"
                    )
                )
            )

            if candidate:
                price = candidate
                break

    if (
        not title
        and soup.title
    ):
        title = (
            soup.title
            .get_text(
                " ",
                strip=True,
            )
        )

    return {
        "title": title,
        "price": price,

        "image_url":
            image_url,

        "brand": brand,
        "model": model,
    }


# =========================================================
# HTTP
# =========================================================

async def scrape_http(
    url,
):
    async with (
        httpx.AsyncClient(
            headers=HEADERS,

            follow_redirects=True,

            timeout=HTTP_TIMEOUT,
        )
    ) as client:

        response = (
            await client.get(
                url
            )
        )

        response.raise_for_status()

        final_url = str(
            response.url
        )

        data = (
            extract_html_data(
                response.text
            )
        )

        return (
            final_url,
            data,
        )


# =========================================================
# MEYER
# =========================================================

def get_meyer_variants(
    url,
):
    query = parse_qs(
        urlparse(
            url
        ).query
    )

    def get_value(
        name,
    ):
        value = (
            query.get(
                name,
                [None],
            )[0]
        )

        if value is None:
            return None

        return unquote(
            str(value)
        ).strip()

    raw = {
        "Yüzey":
            get_value(
                "yuzey"
            ),

        "Boyut":
            get_value(
                "boyut"
            ),

        "Renk":
            get_value(
                "renk"
            ),
    }

    variants = {}

    for key, value in (
        raw.items()
    ):
        if not value:
            continue

        pretty = (
            pretty_variant_value(
                key,
                value,
            )
        )

        if pretty:
            variants[
                key
            ] = pretty

    return variants


def get_meyer_raw_variants(
    url,
):
    query = parse_qs(
        urlparse(
            url
        ).query
    )

    def get_value(
        name,
    ):
        return (
            query.get(
                name,
                [None],
            )[0]
        )

    return {
        "yuzey":
            get_value(
                "yuzey"
            ),

        "boyut":
            get_value(
                "boyut"
            ),

        "renk":
            get_value(
                "renk"
            ),
    }


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

        "soft": [
            "soft",
        ],

        "xsoft": [
            "xsoft",
            "x-soft",
        ],

        "mid": [
            "mid",
        ],

        "xl": [
            "xl",
        ],

        "xxl": [
            "xxl",
        ],

        "l": [
            "l",
        ],

        "m": [
            "m",
        ],

        "s": [
            "s",
        ],
    }

    targets = (
        aliases.get(
            target,
            [target],
        )
    )

    result = (
        await page.evaluate(
            """
            (targets) => {

                function norm(value) {

                    return (
                        value || ""
                    )
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
                        document
                        .querySelectorAll(
                            'button,[role="button"],label'
                        )
                    );

                for (
                    const node
                    of nodes
                ) {

                    const rect =
                        node
                        .getBoundingClientRect();

                    if (
                        rect.width <= 0
                        ||
                        rect.height <= 0
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
                                        value
                                        ===
                                        target
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
                            ||
                            node.textContent
                            ||
                            ""
                    };
                }

                return {
                    success:false
                };
            }
            """,

            targets,
        )
    )

    if result.get(
        "success"
    ):
        print(
            "MEYER CLICKED:",
            result.get(
                "text"
            ),
        )

        await page.wait_for_timeout(
            200
        )

        return True

    print(
        "MEYER OPTION NOT FOUND:",
        value,
    )

    return False


async def meyer_get_title(
    page,
    original_url,
):
    try:
        result = (
            await page.evaluate(
                """
                () => {

                    const nodes =
                        Array.from(
                            document
                            .querySelectorAll(
                                "h1"
                            )
                        );

                    for (
                        const node
                        of nodes
                    ) {

                        const r =
                            node
                            .getBoundingClientRect();

                        if (
                            r.width <= 0
                            ||
                            r.height <= 0
                        ) {
                            continue;
                        }

                        const text =
                            (
                                node.innerText
                                ||
                                node.textContent
                                ||
                                ""
                            ).trim();

                        if (text) {
                            return text;
                        }
                    }

                    return null;
                }
                """
            )
        )

        if result:
            return (
                result.strip()
            )

    except Exception:
        pass

    try:
        title = (
            await page.locator(
                'meta[property="og:title"]'
            )
            .get_attribute(
                "content",

                timeout=1000,
            )
        )

        if title:
            return (
                title.strip()
            )

    except Exception:
        pass

    try:
        title = (
            await page.title()
        )

        if title:
            title = (
                re.sub(
                    r"\s*[-|]\s*Meyer.*$",
                    "",
                    title,
                    flags=re.I,
                )
                .strip()
            )

            if title:
                return title

    except Exception:
        pass

    try:
        path = (
            urlparse(
                original_url
            )
            .path
            .strip("/")
        )

        slug = (
            path
            .split("/")[-1]
        )

        slug = unquote(
            slug
        )

        slug = (
            slug.replace(
                "-",
                " ",
            )
        )

        slug = (
            re.sub(
                r"\s+",
                " ",
                slug,
            )
            .strip()
        )

        if slug:
            return (
                slug.title()
            )

    except Exception:
        pass

    return "Meyer Ürünü"


async def meyer_get_price(
    page,
):
    try:
        result = (
            await page.evaluate(
                """
                () => {

                    function visible(el) {

                        if (!el) {
                            return false;
                        }

                        const r =
                            el
                            .getBoundingClientRect();

                        return (
                            r.width > 0
                            &&
                            r.height > 0
                        );
                    }

                    const h1 =
                        Array.from(
                            document
                            .querySelectorAll(
                                "h1"
                            )
                        )
                        .find(visible);

                    const titleRect =
                        h1
                        ?
                        h1.getBoundingClientRect()
                        :
                        null;

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
                                document
                                .querySelectorAll(
                                    selector
                                );
                        }
                        catch {
                            continue;
                        }

                        for (
                            const el
                            of nodes
                        ) {

                            if (
                                seen.has(el)
                                ||
                                !visible(el)
                            ) {
                                continue;
                            }

                            seen.add(el);

                            const text =
                                (
                                    el.innerText
                                    ||
                                    el.textContent
                                    ||
                                    ""
                                ).trim();

                            if (
                                !/TL/i.test(text)
                                ||
                                !/\\d/.test(text)
                            ) {
                                continue;
                            }

                            const r =
                                el
                                .getBoundingClientRect();

                            let score = 0;

                            if (
                                el.classList
                                .contains(
                                    "text-xl"
                                )
                            ) {
                                score += 2000;
                            }

                            if (
                                el.classList
                                .contains(
                                    "font-medium"
                                )
                            ) {
                                score += 1500;
                            }

                            if (titleRect) {

                                const ty =
                                    titleRect.top
                                    +
                                    titleRect.height / 2;

                                const py =
                                    r.top
                                    +
                                    r.height / 2;

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
                            -
                            a.score
                    );

                    return (
                        results.length
                        ?
                        results[0]
                        :
                        null
                    );
                }
                """
            )
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
                )

                return price

    except Exception as e:
        print(
            "MEYER PRICE ERROR:",
            repr(e),
        )

    # Fallback.
    try:
        matches = (
            await page.evaluate(
                """
                () => {

                    const h1 =
                        document
                        .querySelector(
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
                            .test(
                                root.innerText
                            )
                        ) {
                            break;
                        }

                        root =
                            root.parentElement;
                    }

                    const text =
                        root
                        ?
                        root.innerText
                        :
                        document.body
                        .innerText;

                    return (
                        text.match(
                            /\\d[\\d.,]*\\s*TL/gi
                        )
                        ||
                        []
                    );
                }
                """
            )
        )

        for text in (
            matches[:15]
        ):
            price = clean_price(
                text
            )

            if (
                price is not None
                and
                price > 20
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
        result = (
            await page.evaluate(
                """
                (color) => {

                    function visible(el) {

                        const r =
                            el
                            .getBoundingClientRect();

                        return (
                            r.width > 0
                            &&
                            r.height > 0
                        );
                    }

                    const h1 =
                        Array.from(
                            document
                            .querySelectorAll(
                                "h1"
                            )
                        )
                        .find(visible);

                    const titleRect =
                        h1
                        ?
                        h1.getBoundingClientRect()
                        :
                        null;

                    const images =
                        Array.from(
                            document
                            .querySelectorAll(
                                'img[alt="Image"],img'
                            )
                        );

                    const results = [];

                    for (
                        const img
                        of images
                    ) {

                        if (!visible(img)) {
                            continue;
                        }

                        const src =
                            img.currentSrc
                            ||
                            img.src
                            ||
                            img.getAttribute(
                                "data-src"
                            )
                            ||
                            "";

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
                            img
                            .getBoundingClientRect();

                        const area =
                            r.width
                            *
                            r.height;

                        if (
                            area < 20000
                        ) {
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
                                +
                                r.height / 2;

                            const titleY =
                                titleRect.top
                                +
                                titleRect.height / 2;

                            const dy =
                                Math.abs(
                                    imageY
                                    -
                                    titleY
                                );

                            score +=
                                Math.max(
                                    0,
                                    2000 - dy
                                );

                            if (
                                r.left
                                <
                                titleRect.left
                            ) {
                                score += 700;
                            }
                        }

                        const lower =
                            src.toLowerCase();

                        if (
                            color
                            ===
                            "siyah"
                        ) {

                            if (
                                lower.includes(
                                    "-bk-"
                                )
                                ||
                                lower.includes(
                                    "_bk_"
                                )
                                ||
                                lower.includes(
                                    "black"
                                )
                                ||
                                lower.includes(
                                    "siyah"
                                )
                            ) {
                                score += 5000;
                            }
                        }

                        if (
                            color
                            ===
                            "turuncu"
                        ) {

                            if (
                                lower.includes(
                                    "orange"
                                )
                                ||
                                lower.includes(
                                    "daidai"
                                )
                                ||
                                lower.includes(
                                    "-or-"
                                )
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
                            -
                            a.score
                    );

                    return (
                        results.length
                        ?
                        results[0]
                        :
                        null
                    );
                }
                """,

                color,
            )
        )

        if result:

            src = (
                result.get(
                    "src"
                )
            )

            if src:
                print(
                    "MEYER IMAGE FOUND:",
                    src,
                )

                return src

    except Exception as e:
        print(
            "MEYER IMAGE ERROR:",
            repr(e),
        )

    try:
        return (
            await page.locator(
                'meta[property="og:image"]'
            )
            .get_attribute(
                "content",
                timeout=800,
            )
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
        "=========================="
    )

    print(
        "MEYER VARIANTS:",
        variants,
    )

    try:
        await page.wait_for_function(
            """
            () =>
                document.querySelector(
                    "h1"
                )
                ||
                document.querySelectorAll(
                    "button"
                ).length > 3
            """,

            timeout=3500,
        )

    except Exception:
        print(
            "MEYER DOM WAIT TIMEOUT -> CONTINUE"
        )

    if raw_variants.get(
        "yuzey"
    ):
        await meyer_click_variant(
            page,
            raw_variants[
                "yuzey"
            ],
        )

    if raw_variants.get(
        "boyut"
    ):
        await meyer_click_variant(
            page,
            raw_variants[
                "boyut"
            ],
        )

    if raw_variants.get(
        "renk"
    ):
        await meyer_click_variant(
            page,
            raw_variants[
                "renk"
            ],
        )

    await page.wait_for_timeout(
        400
    )

    title = (
        await meyer_get_title(
            page,
            original_url,
        )
    )

    price = (
        await meyer_get_price(
            page
        )
    )

    image_url = (
        await meyer_get_image(
            page,
            raw_variants.get(
                "renk"
            ),
        )
    )

    variant_text = (
        make_variant_text(
            variants
        )
    )

    print(
        "MEYER FINAL TITLE:",
        title,
    )

    print(
        "MEYER FINAL PRICE:",
        price,
    )

    print(
        "MEYER VARIANT TEXT:",
        variant_text,
    )

    return {
        "title":
            title,

        "price":
            price,

        "image_url":
            image_url,

        "brand":
            None,

        "model":
            None,

        "variants":
            variants,

        "variant_text":
            variant_text,
    }


# =========================================================
# GENERIC BROWSER
# =========================================================

def get_price_selectors(
    store,
):
    if (
        store
        == "Amazon Türkiye"
    ):
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
    selectors = (
        get_price_selectors(
            store
        )
    )

    try:
        results = (
            await page.evaluate(
                """
                (selectors) => {

                    const output = [];

                    for (
                        let priority=0;
                        priority<selectors.length;
                        priority++
                    ) {

                        const selector =
                            selectors[
                                priority
                            ];

                        let nodes;

                        try {
                            nodes =
                                document
                                .querySelectorAll(
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
                                node
                                .getBoundingClientRect();

                            if (
                                r.width <= 0
                                ||
                                r.height <= 0
                            ) {
                                continue;
                            }

                            const text =
                                (
                                    node.innerText
                                    ||
                                    node.textContent
                                    ||
                                    node.getAttribute(
                                        "content"
                                    )
                                    ||
                                    ""
                                ).trim();

                            if (
                                !text
                                ||
                                !/\\d/.test(
                                    text
                                )
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
                            -
                            b.priority
                    );

                    return output;
                }
                """,

                selectors,
            )
        )

        for item in results:

            price = (
                clean_price(
                    item.get(
                        "text"
                    )
                )
            )

            if price:
                return price

    except Exception:
        pass

    return None


async def browser_find_title(
    page,
):
    try:
        title = (
            await page.evaluate(
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
                            document
                            .querySelectorAll(
                                selector
                            );

                        for (
                            const node
                            of nodes
                        ) {

                            const r =
                                node
                                .getBoundingClientRect();

                            if (
                                r.width <= 0
                                ||
                                r.height <= 0
                            ) {
                                continue;
                            }

                            const text =
                                (
                                    node.innerText
                                    ||
                                    node.textContent
                                    ||
                                    ""
                                ).trim();

                            if (text) {
                                return text;
                            }
                        }
                    }

                    const meta =
                        document
                        .querySelector(
                            'meta[property="og:title"]'
                        );

                    if (
                        meta
                        &&
                        meta.content
                    ) {
                        return meta.content;
                    }

                    return (
                        document.title
                        ||
                        null
                    );
                }
                """
            )
        )

        if title:
            return title.strip()

    except Exception:
        pass

    return None


async def browser_find_image(
    page,
):
    try:
        return (
            await page.evaluate(
                """
                () => {

                    const image =
                        document
                        .querySelector(
                            "#landingImage"
                        )
                        ||
                        document
                        .querySelector(
                            'img[itemprop="image"]'
                        );

                    if (image) {

                        const src =
                            image.currentSrc
                            ||
                            image.src
                            ||
                            image.getAttribute(
                                "data-src"
                            );

                        if (src) {
                            return src;
                        }
                    }

                    const meta =
                        document
                        .querySelector(
                            'meta[property="og:image"]'
                        );

                    if (
                        meta
                        &&
                        meta.content
                    ) {
                        return meta.content;
                    }

                    return null;
                }
                """
            )
        )

    except Exception:
        return None


async def browser_find_variants(
    page,
):
    """
    Diğer mağazalarda seçili select/radio
    değerlerini yakalamaya çalışır.
    Gürültülü sonuçları filtreler.
    """

    try:
        data = (
            await page.evaluate(
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

                    // SELECT
                    for (
                        const select
                        of document
                        .querySelectorAll(
                            "select"
                        )
                    ) {

                        const option =
                            select
                            .options[
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
                            ||
                            value.toLowerCase()
                            === "seçiniz"
                        ) {
                            continue;
                        }

                        let label = "";

                        if (select.id) {

                            const l =
                                document
                                .querySelector(
                                    `label[for="${select.id}"]`
                                );

                            if (l) {
                                label =
                                    clean(
                                        l.textContent
                                    );
                            }
                        }

                        label =
                            label
                            ||
                            select.getAttribute(
                                "aria-label"
                            )
                            ||
                            select.name
                            ||
                            "";

                        result.push({
                            label,
                            value
                        });
                    }

                    // CHECKED RADIO
                    for (
                        const input
                        of document
                        .querySelectorAll(
                            'input[type="radio"]:checked'
                        )
                    ) {

                        let value =
                            input.value || "";

                        let label = "";

                        if (input.id) {

                            const l =
                                document
                                .querySelector(
                                    `label[for="${input.id}"]`
                                );

                            if (l) {
                                value =
                                    clean(
                                        l.textContent
                                    )
                                    ||
                                    value;
                            }
                        }

                        const fieldset =
                            input.closest(
                                "fieldset"
                            );

                        if (fieldset) {

                            const legend =
                                fieldset
                                .querySelector(
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
                            ||
                            input.name
                            ||
                            "";

                        result.push({
                            label,
                            value
                        });
                    }

                    return result;
                }
                """
            )
        )

    except Exception:
        return {}

    allowed_labels = {
        "renk": "Renk",

        "color": "Renk",

        "colour": "Renk",

        "boyut": "Boyut",

        "size": "Boyut",

        "beden": "Beden",

        "yuzey": "Yüzey",

        "surface": "Yüzey",

        "switch": "Switch",

        "layout": "Layout",

        "dongle": "Dongle",
    }

    variants = {}

    for item in data:

        label_norm = (
            normalize_text(
                item.get(
                    "label"
                )
            )
        )

        value = (
            item.get(
                "value"
            )
        )

        label = None

        # input adı bazen
        # product-options[Size] gibi.
        for candidate, pretty in (
            allowed_labels.items()
        ):
            if candidate in (
                label_norm
            ):
                label = pretty
                break

        if not label:
            continue

        pretty_value = (
            pretty_variant_value(
                label,
                value,
            )
        )

        if pretty_value:
            variants[
                label
            ] = (
                pretty_value
            )

    return variants


async def amazon_body_price(
    page,
):
    try:
        body = (
            await page.locator(
                "body"
            )
            .text_content(
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

            counts[
                value
            ] = (
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


# =========================================================
# BROWSER
# =========================================================

async def scrape_browser(
    url,
    retry=True,
):
    browser = (
        await get_browser()
    )

    context = None
    page = None

    try:
        context = (
            await browser
            .new_context(
                locale="tr-TR",

                timezone_id=
                    "Europe/Istanbul",

                user_agent=
                    HEADERS[
                        "User-Agent"
                    ],

                viewport={
                    "width":1365,
                    "height":900,
                },

                extra_http_headers={
                    "Accept-Language":
                        "tr-TR,tr;q=0.9,en;q=0.8"
                },
            )
        )

        page = (
            await context
            .new_page()
        )

        async def route_handler(
            route,
        ):
            resource_type = (
                route.request
                .resource_type
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
        )

        try:
            await page.goto(
                url,

                wait_until=
                    "domcontentloaded",

                timeout=
                    BROWSER_GOTO_TIMEOUT,
            )

        except PlaywrightTimeoutError as e:

            print(
                "BROWSER GOTO TIMEOUT -> CONTINUE:",
                repr(e),
            )

            try:
                await page.wait_for_timeout(
                    1000
                )

            except Exception:
                pass

        final_url = (
            page.url
            or url
        )

        store = (
            detect_store(
                final_url
            )
        )

        # =================================================
        # MEYER
        # =================================================

        if (
            store == "Meyer"
            or
            "meyergaming.com"
            in url.lower()
        ):
            data = (
                await scrape_meyer(
                    page,
                    url,
                )
            )

            return (
                final_url,
                data,
            )

        # =================================================
        # GENERIC
        # =================================================

        html = (
            await page.content()
        )

        data = (
            extract_html_data(
                html
            )
        )

        if not data.get(
            "title"
        ):
            data["title"] = (
                await browser_find_title(
                    page
                )
            )

        if (
            data.get(
                "price"
            )
            is None
        ):
            data["price"] = (
                await browser_find_price(
                    page,
                    store,
                )
            )

        if (
            store
            == "Amazon Türkiye"
            and
            data.get(
                "price"
            )
            is None
        ):
            data["price"] = (
                await amazon_body_price(
                    page
                )
            )

        if not data.get(
            "image_url"
        ):
            data["image_url"] = (
                await browser_find_image(
                    page
                )
            )

        # URL varyantlarını al.
        variants = (
            query_variants(
                url
            )
        )

        # DOM varyantları ile birleştir.
        try:
            dom_variants = (
                await browser_find_variants(
                    page
                )
            )

            for key, value in (
                dom_variants.items()
            ):
                variants[
                    key
                ] = value

        except Exception:
            pass

        data["variants"] = (
            variants
        )

        data["variant_text"] = (
            make_variant_text(
                variants
            )
        )

        return (
            final_url,
            data,
        )

    except Exception as e:

        print(
            "BROWSER ERROR:",
            repr(e),
        )

        message = (
            str(e).lower()
        )

        browser_dead = (
            "browser has been closed"
            in message
            or
            "target page"
            in message
            or
            "browser closed"
            in message
            or
            "connection closed"
            in message
        )

        if (
            retry
            and browser_dead
        ):
            await reset_browser()

            return (
                await scrape_browser(
                    url,
                    retry=False,
                )
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
# MAIN
# =========================================================

async def scrape_product(
    url,
):
    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):
        raise ValueError(
            "Geçerli bir ürün linki gir."
        )

    store = detect_store(
        url
    )

    print(
        "================================="
    )

    print(
        "SCRAPE START:",
        store,
    )

    # =====================================================
    # HEPSIBURADA
    # =====================================================

    if store == "Hepsiburada":

        return (
            await scrape_hepsiburada_api(
                url
            )
        )

    # =====================================================
    # MEYER
    # =====================================================

    if (
        store == "Meyer"
        or
        "meyergaming.com"
        in url.lower()
    ):

        final_url, data = (
            await scrape_browser(
                url
            )
        )

        title = (
            data.get(
                "title"
            )
            or
            "Meyer Ürünü"
        )

        price = (
            data.get(
                "price"
            )
        )

        if price is None:
            raise RuntimeError(
                "Meyer ürün fiyatı alınamadı."
            )

        return ScrapedProduct(
            title=title[:500],

            store="Meyer",

            # Query parametreleri
            # kaybolmasın.
            url=url,

            price=price,

            image_url=
                data.get(
                    "image_url"
                ),

            brand=
                data.get(
                    "brand"
                ),

            model=
                data.get(
                    "model"
                ),

            variants=
                data.get(
                    "variants"
                ),

            variant_text=
                data.get(
                    "variant_text"
                ),

            method=
                "meyer-fast-browser",
        )

    # =====================================================
    # WRAITH
    #
    # Önce Shopify exact variant.
    # =====================================================

    if is_wraith(
        store,
        url,
    ):
        try:
            return (
                await scrape_wraith_shopify(
                    url
                )
            )

        except Exception as e:

            print(
                "WRAITH SHOPIFY ERROR -> FALLBACK:",
                repr(e),
            )

            # Generic HTTP/browser'a
            # devam et.


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
            data.get(
                "price"
            )
            is not None
            and
            data.get(
                "title"
            )
        ):
            detected = (
                detect_store(
                    final_url
                )
            )

            variants = (
                query_variants(
                    url
                )
            )

            return ScrapedProduct(
                title=
                    data[
                        "title"
                    ][:500],

                store=
                    detected,

                # Kullanıcının variant query
                # parametrelerini koru.
                url=url,

                price=
                    data[
                        "price"
                    ],

                image_url=
                    data.get(
                        "image_url"
                    ),

                brand=
                    data.get(
                        "brand"
                    ),

                model=
                    data.get(
                        "model"
                    ),

                variants=
                    variants,

                variant_text=
                    make_variant_text(
                        variants
                    ),

                method="http",
            )

    except Exception as e:

        http_error = e

        print(
            "HTTP ERROR -> BROWSER:",
            repr(e),
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
            data.get(
                "title"
            )
            or
            "Ürün"
        )

        price = (
            data.get(
                "price"
            )
        )

        if price is None:
            raise RuntimeError(
                "Sayfa açıldı fakat fiyat bulunamadı."
            )

        return ScrapedProduct(
            title=
                title[:500],

            store=
                detect_store(
                    final_url
                ),

            url=url,

            price=
                price,

            image_url=
                data.get(
                    "image_url"
                ),

            brand=
                data.get(
                    "brand"
                ),

            model=
                data.get(
                    "model"
                ),

            variants=
                data.get(
                    "variants"
                ),

            variant_text=
                data.get(
                    "variant_text"
                ),

            method=
                "browser",
        )

    except Exception as browser_error:

        print(
            "FINAL BROWSER ERROR:",
            repr(
                browser_error
            ),
        )

        if http_error:
            raise RuntimeError(
                f"HTTP başarısız "
                f"({http_error}); "
                f"tarayıcı da başarısız "
                f"({browser_error})"
            )

        raise RuntimeError(
            f"Tarayıcı başarısız "
            f"({browser_error})"
        )
