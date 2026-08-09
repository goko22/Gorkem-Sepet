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
# GENEL YARDIMCILAR
# =========================================================

def clean_price(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None

    text = str(value).strip()

    if not text:
        return None

    patterns = [
        r"(\d{1,3}(?:\.\d{3})+,\d{1,2})",
        r"(\d+,\d{1,2})",
        r"(\d{1,3}(?:,\d{3})+\.\d{1,2})",
        r"(\d+\.\d{1,2})",
        r"(\d{1,3}(?:\.\d{3})+)",
        r"(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if not match:
            continue

        raw = match.group(1)

        try:
            if "." in raw and "," in raw:
                if raw.rfind(",") > raw.rfind("."):
                    raw = raw.replace(".", "").replace(",", ".")
                else:
                    raw = raw.replace(",", "")

            elif "," in raw:
                raw = raw.replace(".", "").replace(",", ".")

            elif raw.count(".") >= 1:
                parts = raw.split(".")

                if len(parts[-1]) == 3:
                    raw = raw.replace(".", "")

            number = float(raw)

            if 0 < number < 100_000_000:
                return number

        except Exception:
            pass

    return parse_price(text)


def normalize_text(text):
    text = str(text or "").lower()

    text = (
        text.replace("ı", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
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
    ).strip()

    return text


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


def extract_hepsiburada_code(url: str):
    match = re.search(
        r"(HBCV[A-Z0-9]+|HBC[A-Z0-9]+)",
        url,
        re.I,
    )

    if match:
        return match.group(1).upper()

    return None


# =========================================================
# HEPSIBURADA
# =========================================================

async def scrape_hepsiburada_api(url: str) -> ScrapedProduct:
    api_key = os.getenv("PARSE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "PARSE_API_KEY Render Environment içinde yok."
        )

    product_code = extract_hepsiburada_code(url)

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

    details_endpoint = (
        f"{base_url}/get_product_details"
    )

    async with httpx.AsyncClient(
        timeout=45,
        follow_redirects=True,
    ) as client:

        response = await client.get(
            details_endpoint,
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

        price = clean_price(
            data.get("unit_price")
            or data.get("price")
            or data.get("current_price")
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
        # RESIM ICIN SEARCH
        # =================================================

        search_endpoint = (
            f"{base_url}/search_products"
        )

        search_response = await client.get(
            search_endpoint,
            headers=headers,
            params={
                "page": 1,
                "query": title,
            },
        )

        image_url = None

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

                if not isinstance(
                    products,
                    list,
                ):
                    products = []

                best_item = None
                best_score = -1

                details_product_id = str(
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

                    item_sku = str(
                        item.get("sku")
                        or ""
                    ).upper()

                    item_product_id = str(
                        item.get("productId")
                        or item.get(
                            "product_id"
                        )
                        or ""
                    ).upper()

                    item_name = (
                        item.get("name")
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

                    if (
                        item_sku
                        == product_code
                    ):
                        score += 100

                    if (
                        details_product_id
                        and item_product_id
                        == details_product_id
                    ):
                        score += 100

                    item_brand = (
                        normalize_text(
                            item.get("brand")
                        )
                    )

                    target_brand = (
                        normalize_text(
                            brand
                        )
                    )

                    if (
                        item_brand
                        and target_brand
                        and item_brand
                        == target_brand
                    ):
                        score += 20

                    item_price = clean_price(
                        item.get("price")
                    )

                    if (
                        item_price is not None
                        and abs(
                            item_price
                            - price
                        ) < 0.01
                    ):
                        score += 30

                    if score > best_score:
                        best_score = score
                        best_item = item

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
                    "HB IMAGE ERROR:",
                    repr(e),
                )

        print(
            "HEPSIBURADA PRICE:",
            price,
        )

        print(
            "HEPSIBURADA IMAGE:",
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
# HTML PARSER
# =========================================================

def extract_html_data(html: str):
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

        if isinstance(
            image,
            str,
        ):
            image_url = image

        elif (
            isinstance(image, list)
            and image
        ):
            image_url = image[0]

        elif isinstance(
            image,
            dict,
        ):
            image_url = image.get(
                "url"
            )

        brand_data = product.get(
            "brand"
        )

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

        offers = product.get(
            "offers"
        )

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
                    or offer.get(
                        "lowPrice"
                    )
                    or offer.get(
                        "salePrice"
                    )
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
# HTTP SCRAPER
# =========================================================

async def scrape_http(url: str):
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=20,
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
# MEYER YARDIMCILARI
# =========================================================

def get_meyer_variants(url):
    parsed = urlparse(url)

    query = parse_qs(
        parsed.query
    )

    yuzey = (
        query.get(
            "yuzey",
            [None],
        )[0]
    )

    boyut = (
        query.get(
            "boyut",
            [None],
        )[0]
    )

    renk = (
        query.get(
            "renk",
            [None],
        )[0]
    )

    return {
        "yuzey": yuzey,
        "boyut": boyut,
        "renk": renk,
    }


async def click_text_exact(
    page,
    text,
):
    if not text:
        return False

    wanted = str(
        text
    ).strip()

    candidates = [
        wanted,
        wanted.upper(),
        wanted.capitalize(),
    ]

    # Turkish color names
    special = {
        "siyah": "Siyah",
        "turuncu": "Daidai Orange",
        "orange": "Daidai Orange",
        "daidai orange":
            "Daidai Orange",
        "soft": "SOFT",
        "xsoft": "XSOFT",
        "mid": "MID",
        "xl": "XL",
        "xxl": "XXL",
        "m": "M",
        "l": "L",
        "s": "S",
    }

    normalized = (
        wanted.lower()
        .replace("ı", "i")
    )

    if normalized in special:
        candidates.insert(
            0,
            special[normalized],
        )

    seen = set()

    for candidate in candidates:
        if candidate in seen:
            continue

        seen.add(candidate)

        try:
            locator = (
                page.get_by_text(
                    candidate,
                    exact=True,
                )
            )

            count = (
                await locator.count()
            )

            for i in range(
                min(count, 10)
            ):
                node = (
                    locator.nth(i)
                )

                try:
                    if (
                        await node.is_visible()
                    ):
                        await node.click(
                            force=True,
                            timeout=2500,
                        )

                        await page.wait_for_timeout(
                            900
                        )

                        print(
                            "MEYER CLICKED:",
                            candidate,
                        )

                        return True

                except Exception:
                    continue

        except Exception:
            pass

    print(
        "MEYER OPTION NOT FOUND:",
        wanted,
    )

    return False


async def meyer_main_price(
    page,
    title,
):
    try:
        body = (
            await page.locator(
                "body"
            ).inner_text(
                timeout=5000
            )
            or ""
        )

    except Exception:
        body = ""

    if not body:
        return None

    # Ürün başlığından SONRA gelen ilk TL fiyatı.
    # Böylece üst banner / öneri ürün fiyatlarını
    # alma ihtimalimiz azalıyor.

    start = 0

    if title:
        index = body.find(
            title
        )

        if index >= 0:
            start = (
                index
                + len(title)
            )

    nearby = body[
        start:start + 1500
    ]

    patterns = [
        r"(\d{1,3}(?:\.\d{3})+,\d{1,2})\s*TL",
        r"(\d+,\d{1,2})\s*TL",
        r"(\d{1,3}(?:\.\d{3})+)\s*TL",
        r"(\d+)\s*TL",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            nearby,
            re.I,
        )

        if match:
            value = clean_price(
                match.group(1)
            )

            if value:
                return value

    return None


async def meyer_selected_image(
    page,
):
    """
    Seçili renk tıklandıktan sonra görünür büyük
    ürün görselini seçmeye çalışır.
    """

    try:
        images = page.locator(
            "img"
        )

        count = await images.count()

        candidates = []

        for i in range(
            min(count, 150)
        ):
            node = images.nth(i)

            try:
                if not await node.is_visible():
                    continue

                src = await node.get_attribute(
                    "src"
                )

                if not src:
                    continue

                if "cdn.myikas.com" not in src:
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

                # Küçük thumbnail / icon'ları atla.
                if (
                    width < 180
                    or height < 180
                ):
                    continue

                area = width * height

                candidates.append(
                    (
                        area,
                        box.get(
                            "y",
                            99999,
                        ),
                        src,
                    )
                )

            except Exception:
                continue

        if candidates:
            # Önce büyük alan, sonra sayfanın üst kısmı.
            candidates.sort(
                key=lambda x: (
                    -x[0],
                    x[1],
                )
            )

            print(
                "MEYER IMAGE FOUND:",
                candidates[0][2],
            )

            return candidates[0][2]

    except Exception as e:
        print(
            "MEYER IMAGE ERROR:",
            repr(e),
        )

    return None


# =========================================================
# CHROMIUM
# =========================================================

async def scrape_browser(url: str):
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
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=45000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10000,
                )

            except PlaywrightTimeoutError:
                await page.wait_for_timeout(
                    3000
                )

            final_url = page.url

            store = detect_store(
                final_url
            )

            # =================================================
            # MEYER OZEL
            # =================================================

            if store == "Meyer":
                print(
                    "=========================="
                )

                print(
                    "MEYER VARIANT MODE"
                )

                variants = (
                    get_meyer_variants(
                        url
                    )
                )

                print(
                    "MEYER VARIANTS:",
                    variants,
                )

                # Önce yüzey
                if variants[
                    "yuzey"
                ]:
                    await click_text_exact(
                        page,
                        variants[
                            "yuzey"
                        ],
                    )

                # Sonra boyut
                if variants[
                    "boyut"
                ]:
                    await click_text_exact(
                        page,
                        variants[
                            "boyut"
                        ],
                    )

                # En son renk
                if variants[
                    "renk"
                ]:
                    await click_text_exact(
                        page,
                        variants[
                            "renk"
                        ],
                    )

                # Variant JS güncellensin.
                await page.wait_for_timeout(
                    1500
                )

                html = await page.content()

                data = extract_html_data(
                    html
                )

                # Başlığı doğrudan h1'den al.
                try:
                    h1 = (
                        page.locator(
                            "h1"
                        ).first
                    )

                    if await h1.count():
                        h1_text = (
                            await h1.inner_text(
                                timeout=2000
                            )
                        ).strip()

                        if h1_text:
                            data[
                                "title"
                            ] = h1_text

                except Exception:
                    pass

                # KRITIK:
                # JSON-LD fiyatını kullanmıyoruz.
                # Seçimlerden sonraki görünür fiyatı alıyoruz.

                selected_price = (
                    await meyer_main_price(
                        page,
                        data.get(
                            "title"
                        ),
                    )
                )

                if selected_price:
                    data[
                        "price"
                    ] = selected_price

                selected_image = (
                    await meyer_selected_image(
                        page
                    )
                )

                if selected_image:
                    data[
                        "image_url"
                    ] = selected_image

                print(
                    "MEYER FINAL PRICE:",
                    data.get(
                        "price"
                    ),
                )

                print(
                    "MEYER FINAL IMAGE:",
                    data.get(
                        "image_url"
                    ),
                )

                print(
                    "=========================="
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

            # =================================================
            # AMAZON
            # =================================================

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

            # =================================================
            # TRENDYOL
            # =================================================

            elif store == "Trendyol":
                selectors = [
                    ".prc-dsc",
                    ".prc-slg",
                    '[class*="price"]',
                ]

            # =================================================
            # N11
            # =================================================

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

            # =================================================
            # FIYAT
            # =================================================

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
                            min(
                                count,
                                20,
                            )
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
            # AMAZON BODY FALLBACK
            # =================================================

            if (
                store
                == "Amazon Türkiye"
                and data["price"]
                is None
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

                    parsed_prices = [
                        clean_price(x)
                        for x in tl_prices
                    ]

                    parsed_prices = [
                        x
                        for x in parsed_prices
                        if x is not None
                    ]

                    if parsed_prices:
                        counts = {}

                        for value in (
                            parsed_prices
                        ):
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

            # =================================================
            # TITLE
            # =================================================

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

                        if (
                            await node.count()
                        ):
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

            # =================================================
            # IMAGE
            # =================================================

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

                        if (
                            await node.count()
                        ):
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
# ANA SCRAPER
# =========================================================

async def scrape_product(
    url: str
) -> ScrapedProduct:

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

    # =====================================================
    # HEPSIBURADA
    # =====================================================

    if store == "Hepsiburada":
        print(
            "HEPSIBURADA -> PARSE API"
        )

        return (
            await scrape_hepsiburada_api(
                url
            )
        )

    # =====================================================
    # MEYER
    #
    # KRITIK:
    # Meyer'i normal HTTP'den GECIRMIYORUZ.
    # Çünkü HTTP bize varsayılan varyant fiyatı verir.
    # Direkt Chromium'a gönderiyoruz.
    # =====================================================

    if store == "Meyer":
        print(
            "MEYER -> VARIANT BROWSER"
        )

        final_url, data = (
            await scrape_browser(
                url
            )
        )

        if not data.get(
            "title"
        ):
            raise RuntimeError(
                "Meyer ürün adı alınamadı."
            )

        if data.get(
            "price"
        ) is None:
            raise RuntimeError(
                "Meyer seçili varyant fiyatı alınamadı."
            )

        return ScrapedProduct(
            title=data[
                "title"
            ][:500],

            store="Meyer",

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

            method=(
                "meyer-variant-browser"
            ),
        )

    # =====================================================
    # DIGER SITELER - HTTP
    # =====================================================

    http_error = None

    try:
        final_url, data = (
            await scrape_http(
                url
            )
        )

        if (
            data["price"]
            is not None
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

                price=data[
                    "price"
                ],

                image_url=data[
                    "image_url"
                ],

                brand=data[
                    "brand"
                ],

                model=data[
                    "model"
                ],

                method="http",
            )

    except Exception as e:
        http_error = e

        print(
            "HTTP ERROR:",
            repr(e),
        )

    # =====================================================
    # DIGER SITELER - CHROMIUM
    # =====================================================

    try:
        final_url, data = (
            await scrape_browser(
                url
            )
        )

        if not data[
            "title"
        ]:
            data[
                "title"
            ] = "Ürün"

        if data[
            "price"
        ] is None:
            raise RuntimeError(
                "Sayfa açıldı fakat fiyat bulunamadı."
            )

        return ScrapedProduct(
            title=data[
                "title"
            ][:500],

            store=detect_store(
                final_url
            ),

            url=final_url,

            price=data[
                "price"
            ],

            image_url=data[
                "image_url"
            ],

            brand=data[
                "brand"
            ],

            model=data[
                "model"
            ],

            method="browser",
        )

    except Exception as browser_error:
        print(
            "BROWSER ERROR:",
            repr(
                browser_error
            ),
        )

        if http_error:
            raise RuntimeError(
                f"HTTP başarısız ({http_error}); "
                f"Chromium da başarısız ({browser_error})"
            )

        raise RuntimeError(
            f"Chromium başarısız ({browser_error})"
        )
