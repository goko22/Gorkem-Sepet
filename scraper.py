import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from utils import detect_store, parse_price


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


@dataclass
class ScrapedProduct:
    title: str
    store: str
    url: str
    price: Optional[float] = None
    image_url: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    method: str = "unknown"


# =========================================================
# GENEL
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

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def clean_price(value):
    """
    TURKISH PRICE:

    4.799 TL      -> 4799.0
    12.499 TL     -> 12499.0
    4.799,90 TL   -> 4799.90
    899,99 TL     -> 899.99
    """

    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)

        if number > 0:
            return number

        return None

    text = str(value).strip()

    match = re.search(
        r"\d[\d.,]*",
        text,
    )

    if not match:
        return parse_price(text)

    raw = match.group(0)

    try:

        # 4.799,90
        if "." in raw and "," in raw:

            if raw.rfind(",") > raw.rfind("."):
                raw = (
                    raw
                    .replace(".", "")
                    .replace(",", ".")
                )

            else:
                raw = raw.replace(",", "")

        # 899,99
        elif "," in raw:

            parts = raw.split(",")

            if (
                len(parts) == 2
                and len(parts[1]) <= 2
            ):
                raw = raw.replace(",", ".")

            else:
                raw = raw.replace(",", "")

        # 4.799
        elif "." in raw:

            parts = raw.split(".")

            # 4.799 veya 12.499 veya 1.249.999
            if (
                len(parts) >= 2
                and all(
                    len(part) == 3
                    for part in parts[1:]
                )
            ):
                raw = raw.replace(".", "")

        number = float(raw)

        if 0 < number < 100_000_000:
            return number

    except Exception:
        pass

    return parse_price(text)


def similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


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


async def scrape_hepsiburada_api(url):

    api_key = os.getenv(
        "PARSE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "PARSE_API_KEY bulunamadı."
        )

    product_code = (
        extract_hepsiburada_code(url)
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
        timeout=45,
        follow_redirects=True,
    ) as client:

        # =================================================
        # DETAY
        # =================================================

        response = await client.get(
            f"{base_url}/get_product_details",
            headers=headers,
            params={
                "url": url
            },
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

        if isinstance(data, list):

            if not data:
                raise RuntimeError(
                    "Hepsiburada boş cevap verdi."
                )

            data = data[0]

        if not isinstance(data, dict):
            raise RuntimeError(
                "Hepsiburada cevabı geçersiz."
            )

        title = (
            data.get("product_name")
            or data.get("name")
            or data.get("title")
        )

        price = clean_price(
            data.get("unit_price")
            or data.get("price")
            or data.get("current_price")
            or data.get("sale_price")
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

        if not title:
            raise RuntimeError(
                "Hepsiburada ürün adı alınamadı."
            )

        if price is None:
            raise RuntimeError(
                "Hepsiburada fiyat alınamadı."
            )

        # =================================================
        # RESIM
        # =================================================

        image_url = None

        search_response = await client.get(
            f"{base_url}/search_products",
            headers=headers,
            params={
                "page": 1,
                "query": title,
            },
        )

        if search_response.status_code == 200:

            try:

                search_payload = (
                    search_response.json()
                )

                search_data = (
                    search_payload.get(
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
                        search_data.get(
                            "products",
                            [],
                        )
                    )

                best_item = None
                best_score = -1

                detail_product_id = str(
                    data.get("product_id")
                    or data.get("productId")
                    or ""
                ).upper()

                for item in products:

                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue

                    score = 0

                    item_name = (
                        item.get("name")
                        or item.get("product_name")
                        or ""
                    )

                    score += (
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

                    if (
                        item_sku
                        == product_code
                    ):
                        score += 100

                    if (
                        detail_product_id
                        and item_product_id
                        == detail_product_id
                    ):
                        score += 100

                    item_price = clean_price(
                        item.get("price")
                    )

                    if (
                        item_price is not None
                        and abs(
                            item_price - price
                        ) < 0.01
                    ):
                        score += 30

                    if score > best_score:
                        best_score = score
                        best_item = item

                if best_item:

                    image_url = (
                        best_item.get("imageUrl")
                        or best_item.get("image_url")
                        or best_item.get("image")
                    )

            except Exception as e:

                print(
                    "HB IMAGE ERROR:",
                    repr(e),
                )

        print(
            "HEPSIBURADA FINAL PRICE:",
            price,
        )

        print(
            "HEPSIBURADA FINAL IMAGE:",
            image_url,
        )

        return ScrapedProduct(
            title=title[:500],
            store="Hepsiburada",
            url=url,
            price=price,
            image_url=image_url,
            brand=brand,
            model=model,
            method="parse-api",
        )


# =========================================================
# HTML
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
            title = product.get("name")

        image = product.get("image")

        if isinstance(image, str):

            image_url = image

        elif (
            isinstance(image, list)
            and image
        ):

            image_url = image[0]

        elif isinstance(image, dict):

            image_url = image.get("url")

        brand_data = product.get("brand")

        if isinstance(
            brand_data,
            dict,
        ):

            brand = brand_data.get(
                "name"
            )

        elif isinstance(
            brand_data,
            str,
        ):

            brand = brand_data

        model = (
            product.get("model")
            or product.get("sku")
            or product.get("mpn")
        )

        offers = product.get("offers")

        if isinstance(
            offers,
            dict,
        ):

            offers = [offers]

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

                candidate = clean_price(
                    offer.get("price")
                    or offer.get("lowPrice")
                    or offer.get("salePrice")
                )

                if candidate:
                    price = candidate
                    break

    if not title:

        node = soup.find(
            "meta",
            property="og:title",
        )

        if node:

            title = node.get(
                "content"
            )

    if not image_url:

        node = soup.find(
            "meta",
            property="og:image",
        )

        if node:

            image_url = node.get(
                "content"
            )

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

    if not title and soup.title:

        title = soup.title.get_text(
            " ",
            strip=True,
        )

    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "brand": brand,
        "model": model,
    }


# =========================================================
# HTTP
# =========================================================

async def scrape_http(url):

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=20,
    ) as client:

        response = await client.get(url)

        response.raise_for_status()

        final_url = str(
            response.url
        )

        data = extract_html_data(
            response.text
        )

        return final_url, data


# =========================================================
# MEYER
# =========================================================

def get_meyer_variants(url):

    parsed = urlparse(url)

    query = parse_qs(
        parsed.query
    )

    def value(name):

        result = query.get(
            name,
            [None],
        )[0]

        if result:
            return str(result).strip()

        return None

    return {
        "yuzey": value("yuzey"),
        "boyut": value("boyut"),
        "renk": value("renk"),
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
    }

    wanted = aliases.get(
        target,
        [target],
    )

    result = await page.evaluate(
        """
        (targets) => {

            function normalize(value) {
                return (value || "")
                    .toLowerCase()
                    .replaceAll("ı", "i")
                    .replaceAll("ğ", "g")
                    .replaceAll("ü", "u")
                    .replaceAll("ş", "s")
                    .replaceAll("ö", "o")
                    .replaceAll("ç", "c")
                    .replace(/\\s+/g, " ")
                    .trim();
            }

            const normalizedTargets =
                targets.map(normalize);

            const buttons =
                Array.from(
                    document.querySelectorAll(
                        "button"
                    )
                );

            for (const button of buttons) {

                const values = [
                    button.innerText,
                    button.textContent,
                    button.getAttribute(
                        "aria-label"
                    ),
                    button.getAttribute(
                        "title"
                    ),
                    button.getAttribute(
                        "value"
                    ),
                    button.getAttribute(
                        "data-value"
                    ),
                ]
                .filter(Boolean)
                .map(normalize);

                const match =
                    normalizedTargets.some(
                        target =>
                            values.some(
                                value =>
                                    value === target
                            )
                    );

                if (!match) {
                    continue;
                }

                button.scrollIntoView({
                    block: "center",
                    inline: "center"
                });

                button.click();

                return {
                    success: true,
                    text:
                        button.innerText
                        || button.textContent
                        || "",
                    className:
                        button.className
                        || ""
                };
            }

            return {
                success: false
            };
        }
        """,
        wanted,
    )

    if result.get(
        "success"
    ):

        print(
            "MEYER CLICKED:",
            result.get("text"),
        )

        await page.wait_for_timeout(
            900
        )

        return True

    print(
        "MEYER OPTION NOT FOUND:",
        value,
    )

    return False


async def meyer_get_title(page):

    try:

        h1 = page.locator(
            "h1"
        ).first

        if await h1.count():

            text = (
                await h1.inner_text(
                    timeout=2500
                )
            ).strip()

            if text:
                return text

    except Exception:
        pass

    return None


async def meyer_get_price(page):

    """
    h1'e en yakın görünen
    div.text-xl.font-medium içinden
    TL fiyatını al.
    """

    title_box = None

    try:

        h1 = page.locator(
            "h1"
        ).first

        if await h1.count():
            title_box = (
                await h1.bounding_box()
            )

    except Exception:
        pass

    candidates = []

    try:

        nodes = page.locator(
            "div.text-xl.font-medium"
        )

        count = await nodes.count()

        for i in range(
            min(count, 100)
        ):

            node = nodes.nth(i)

            try:

                if not await node.is_visible():
                    continue

                text = (
                    await node.inner_text(
                        timeout=1200
                    )
                ).strip()

                if "TL" not in text.upper():
                    continue

                price = clean_price(text)

                if price is None:
                    continue

                box = await node.bounding_box()

                if not box:
                    continue

                distance = 999999

                if title_box:

                    tx = (
                        title_box["x"]
                        + title_box["width"] / 2
                    )

                    ty = (
                        title_box["y"]
                        + title_box["height"] / 2
                    )

                    px = (
                        box["x"]
                        + box["width"] / 2
                    )

                    py = (
                        box["y"]
                        + box["height"] / 2
                    )

                    distance = (
                        abs(px - tx)
                        + abs(py - ty)
                    )

                candidates.append(
                    (
                        distance,
                        price,
                        text,
                    )
                )

            except Exception:
                continue

    except Exception as e:

        print(
            "MEYER PRICE ERROR:",
            repr(e),
        )

    if candidates:

        candidates.sort(
            key=lambda x: x[0]
        )

        print(
            "MEYER PRICE ELEMENT:",
            candidates[0][2],
        )

        return candidates[0][1]

    # =====================================================
    # FALLBACK
    # =====================================================

    try:

        nodes = page.locator(
            "div"
        )

        count = await nodes.count()

        fallback = []

        for i in range(
            min(count, 500)
        ):

            node = nodes.nth(i)

            try:

                if not await node.is_visible():
                    continue

                text = (
                    await node.inner_text(
                        timeout=300
                    )
                ).strip()

                if not re.fullmatch(
                    r"\d[\d.,]*\s*TL",
                    text,
                    re.I,
                ):
                    continue

                price = clean_price(text)

                box = await node.bounding_box()

                if (
                    price is not None
                    and box
                ):
                    fallback.append(
                        (
                            box["y"],
                            price,
                            text,
                        )
                    )

            except Exception:
                continue

        if fallback:

            fallback.sort(
                key=lambda x: x[0]
            )

            print(
                "MEYER PRICE FALLBACK:",
                fallback[0][2],
            )

            return fallback[0][1]

    except Exception:
        pass

    return None


async def meyer_get_image(
    page,
    renk=None,
):

    """
    Ana ürün alanındaki
    büyük img[alt="Image"] görselini
    seçer.

    Başlığa yakınlık + boyut
    üzerinden puanlar.
    """

    title_box = None

    try:

        h1 = page.locator(
            "h1"
        ).first

        if await h1.count():
            title_box = (
                await h1.bounding_box()
            )

    except Exception:
        pass

    nodes = page.locator(
        'img[alt="Image"]'
    )

    try:
        count = await nodes.count()

    except Exception:
        return None

    candidates = []

    normalized_color = (
        normalize_text(renk)
        if renk
        else ""
    )

    for i in range(
        min(count, 150)
    ):

        node = nodes.nth(i)

        try:

            if not await node.is_visible():
                continue

            src = (
                await node.get_attribute(
                    "src"
                )
            )

            if not src:
                continue

            box = await node.bounding_box()

            if not box:
                continue

            width = box.get(
                "width",
                0,
            )

            height = box.get(
                "height",
                0,
            )

            area = (
                width * height
            )

            # Küçük thumbnail dışarı
            if area < 40_000:
                continue

            score = 0

            # Büyük görsel avantaj
            score += min(
                area / 1000,
                1000,
            )

            if title_box:

                image_center_y = (
                    box["y"]
                    + box["height"] / 2
                )

                title_center_y = (
                    title_box["y"]
                    + title_box["height"] / 2
                )

                vertical_distance = abs(
                    image_center_y
                    - title_center_y
                )

                # Başlıkla aynı ürün bölümündeyse
                # büyük avantaj.
                score += max(
                    0,
                    1000
                    - vertical_distance
                )

            lower_src = (
                src.lower()
            )

            # Siyah ürün görsellerinde
            # Meyer'in dosya adında bk görüyoruz.
            if (
                normalized_color
                == "siyah"
            ):

                if (
                    "-bk-" in lower_src
                    or "_bk_" in lower_src
                    or "black" in lower_src
                    or "siyah" in lower_src
                ):
                    score += 3000

            if (
                normalized_color
                == "turuncu"
            ):

                if (
                    "orange" in lower_src
                    or "daidai" in lower_src
                    or "-or-" in lower_src
                ):
                    score += 3000

            candidates.append(
                (
                    score,
                    src,
                )
            )

        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: -x[0]
    )

    selected = candidates[0][1]

    print(
        "MEYER IMAGE FOUND:",
        selected,
    )

    return selected


async def scrape_meyer(page, url):

    variants = (
        get_meyer_variants(url)
    )

    print(
        "=========================="
    )

    print(
        "MEYER VARIANT MODE"
    )

    print(
        "MEYER VARIANTS:",
        variants,
    )

    # Sayfa tamamen otursun
    await page.wait_for_timeout(
        1500
    )

    # =====================================================
    # YUZEY
    # =====================================================

    if variants["yuzey"]:

        await meyer_click_variant(
            page,
            variants["yuzey"],
        )

    # =====================================================
    # BOYUT
    # =====================================================

    if variants["boyut"]:

        await meyer_click_variant(
            page,
            variants["boyut"],
        )

    # =====================================================
    # RENK
    # =====================================================

    if variants["renk"]:

        await meyer_click_variant(
            page,
            variants["renk"],
        )

    # JS state + resim + fiyat değişsin
    await page.wait_for_timeout(
        1800
    )

    title = await meyer_get_title(
        page
    )

    price = await meyer_get_price(
        page
    )

    image_url = await meyer_get_image(
        page,
        variants.get("renk"),
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
        "MEYER FINAL IMAGE:",
        image_url,
    )

    print(
        "=========================="
    )

    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "brand": None,
        "model": None,
    }


# =========================================================
# BROWSER
# =========================================================

async def scrape_browser(url):

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=HEADERS[
                "User-Agent"
            ],
            viewport={
                "width": 1440,
                "height": 1200,
            },
            extra_http_headers={
                "Accept-Language":
                "tr-TR,tr;q=0.9,en;q=0.8"
            },
        )

        page = await context.new_page()

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            try:

                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10000,
                )

            except PlaywrightTimeoutError:

                await page.wait_for_timeout(
                    2500
                )

            final_url = page.url

            store = detect_store(
                final_url
            )

            # =================================================
            # MEYER
            # =================================================

            if store == "Meyer":

                data = await scrape_meyer(
                    page,
                    url,
                )

                return (
                    final_url,
                    data,
                )

            # =================================================
            # DIGER SITELER
            # =================================================

            html = await page.content()

            data = extract_html_data(
                html
            )

            if store == "Amazon Türkiye":

                selectors = [
                    ".priceToPay .a-offscreen",
                    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                    "#corePrice_feature_div .a-price .a-offscreen",
                    ".apexPriceToPay .a-offscreen",
                    ".a-price .a-offscreen",
                    "#priceblock_ourprice",
                    "#priceblock_dealprice",
                ]

            elif store == "Trendyol":

                selectors = [
                    ".prc-dsc",
                    ".prc-slg",
                    '[class*="price"]',
                ]

            elif store == "N11":

                selectors = [
                    ".newPrice ins",
                    ".price",
                    '[class*="price"]',
                ]

            else:

                selectors = [
                    '[itemprop="price"]',
                    '[class*="price"]',
                ]

            if data["price"] is None:

                for selector in selectors:

                    try:

                        locator = page.locator(
                            selector
                        )

                        count = (
                            await locator.count()
                        )

                        for i in range(
                            min(count, 20)
                        ):

                            text = (
                                await locator
                                .nth(i)
                                .text_content(
                                    timeout=1200
                                )
                                or ""
                            )

                            candidate = (
                                clean_price(
                                    text
                                )
                            )

                            if candidate:

                                data[
                                    "price"
                                ] = candidate

                                break

                        if data[
                            "price"
                        ]:
                            break

                    except Exception:
                        pass

            # =================================================
            # AMAZON FALLBACK
            # =================================================

            if (
                store == "Amazon Türkiye"
                and data["price"] is None
            ):

                try:

                    body = (
                        await page.locator(
                            "body"
                        ).text_content(
                            timeout=5000
                        )
                        or ""
                    )

                    tl_prices = re.findall(
                        r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*TL",
                        body,
                        re.I,
                    )

                    parsed = [
                        clean_price(x)
                        for x in tl_prices
                    ]

                    parsed = [
                        x
                        for x in parsed
                        if x is not None
                    ]

                    if parsed:

                        counts = {}

                        for value in parsed:

                            counts[value] = (
                                counts.get(
                                    value,
                                    0,
                                )
                                + 1
                            )

                        data["price"] = (
                            sorted(
                                counts.items(),
                                key=lambda x: (
                                    -x[1],
                                    x[0],
                                ),
                            )[0][0]
                        )

                except Exception:
                    pass

            if not data["title"]:

                for selector in [
                    "#productTitle",
                    "h1",
                    '[data-test-id="product-name"]',
                ]:

                    try:

                        node = (
                            page.locator(
                                selector
                            ).first
                        )

                        if await node.count():

                            text = (
                                await node
                                .text_content(
                                    timeout=1200
                                )
                                or ""
                            ).strip()

                            if text:

                                data[
                                    "title"
                                ] = text

                                break

                    except Exception:
                        pass

            if not data[
                "image_url"
            ]:

                for selector in [
                    "#landingImage",
                    'img[itemprop="image"]',
                ]:

                    try:

                        node = (
                            page.locator(
                                selector
                            ).first
                        )

                        if await node.count():

                            src = (
                                await node
                                .get_attribute(
                                    "src"
                                )
                            )

                            if src:

                                data[
                                    "image_url"
                                ] = src

                                break

                    except Exception:
                        pass

            print(
                "BROWSER STORE:",
                store,
            )

            print(
                "BROWSER PRICE:",
                data["price"],
            )

            return (
                final_url,
                data,
            )

        finally:

            await context.close()
            await browser.close()


# =========================================================
# ANA
# =========================================================

async def scrape_product(url):

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):

        raise ValueError(
            "Geçerli bir ürün linki gir."
        )

    store = detect_store(url)

    # =====================================================
    # HEPSIBURADA
    # =====================================================

    if store == "Hepsiburada":

        print(
            "HEPSIBURADA -> PARSE API"
        )

        return await scrape_hepsiburada_api(
            url
        )

    # =====================================================
    # MEYER
    # =====================================================

    if store == "Meyer":

        print(
            "MEYER -> DOM VARIANT BROWSER"
        )

        final_url, data = (
            await scrape_browser(url)
        )

        if not data.get("title"):

            raise RuntimeError(
                "Meyer ürün adı alınamadı."
            )

        if data.get("price") is None:

            raise RuntimeError(
                "Meyer varyant fiyatı alınamadı."
            )

        return ScrapedProduct(
            title=data["title"][:500],
            store="Meyer",
            url=url,
            price=data["price"],
            image_url=data.get(
                "image_url"
            ),
            brand=data.get("brand"),
            model=data.get("model"),
            method="meyer-dom-variant",
        )

    # =====================================================
    # DIGER SITELER - HTTP
    # =====================================================

    http_error = None

    try:

        final_url, data = (
            await scrape_http(url)
        )

        if (
            data["price"] is not None
            and data["title"]
        ):

            print(
                "HTTP SUCCESS:",
                detect_store(
                    final_url
                ),
                data["price"],
            )

            return ScrapedProduct(
                title=data[
                    "title"
                ][:500],
                store=detect_store(
                    final_url
                ),
                url=final_url,
                price=data["price"],
                image_url=data[
                    "image_url"
                ],
                brand=data["brand"],
                model=data["model"],
                method="http",
            )

    except Exception as e:

        http_error = e

        print(
            "HTTP ERROR:",
            repr(e),
        )

    # =====================================================
    # DIGER SITELER - BROWSER
    # =====================================================

    try:

        final_url, data = (
            await scrape_browser(url)
        )

        if not data["title"]:

            data["title"] = "Ürün"

        if data["price"] is None:

            raise RuntimeError(
                "Sayfa açıldı fakat fiyat bulunamadı."
            )

        return ScrapedProduct(
            title=data["title"][:500],
            store=detect_store(
                final_url
            ),
            url=final_url,
            price=data["price"],
            image_url=data[
                "image_url"
            ],
            brand=data["brand"],
            model=data["model"],
            method="browser",
        )

    except Exception as browser_error:

        print(
            "BROWSER ERROR:",
            repr(browser_error),
        )

        if http_error:

            raise RuntimeError(
                f"HTTP başarısız ({http_error}); "
                f"Chromium da başarısız ({browser_error})"
            )

        raise RuntimeError(
            f"Chromium başarısız ({browser_error})"
        )
