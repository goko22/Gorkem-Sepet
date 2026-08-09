import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional

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
# URUN KODU
# =========================================================

def extract_product_code(url: str):

    # Hepsiburada
    match = re.search(
        r"(HBCV[A-Z0-9]+)",
        url,
        re.I,
    )

    if match:
        return match.group(1).upper()

    # Amazon ASIN
    match = re.search(
        r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        url,
        re.I,
    )

    if match:
        return match.group(1).upper()

    return None


# =========================================================
# FIYAT PARSER
# =========================================================

def clean_price(text):
    """
    898,99 TL
    1.299,90 TL
    1299.90
    gibi degerleri float'a cevirir.
    """

    if text is None:
        return None

    text = str(text)

    patterns = [
        r"(\d{1,3}(?:\.\d{3})+,\d{2})",
        r"(\d+,\d{2})",
        r"(\d{1,3}(?:\.\d{3})+)",
        r"(\d+\.\d{2})",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
        )

        if not match:
            continue

        value = match.group(1)

        try:

            if "," in value:

                value = (
                    value
                    .replace(".", "")
                    .replace(",", ".")
                )

            elif value.count(".") > 1:

                value = value.replace(
                    ".",
                    "",
                )

            result = float(value)

            if (
                result > 0
                and result < 100_000_000
            ):
                return result

        except Exception:
            pass

    return parse_price(text)


# =========================================================
# BODY ICINDEN TL FIYATI BUL
# =========================================================

def find_tl_prices(text: str):

    if not text:
        return []

    patterns = [
        r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*TL",
        r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*₺",
        r"₺\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
    ]

    prices = []

    for pattern in patterns:

        for match in re.findall(
            pattern,
            text,
            re.I,
        ):

            price = clean_price(match)

            if (
                price is not None
                and price >= 1
                and price <= 50_000_000
            ):
                prices.append(price)

    return prices


# =========================================================
# JSON-LD
# =========================================================

def walk_for_product(data):

    if isinstance(data, dict):

        product_type = data.get(
            "@type"
        )

        if product_type == "Product":
            return data

        if (
            isinstance(
                product_type,
                list,
            )
            and "Product" in product_type
        ):
            return data

        for value in data.values():

            result = walk_for_product(
                value
            )

            if result:
                return result

    elif isinstance(data, list):

        for item in data:

            result = walk_for_product(
                item
            )

            if result:
                return result

    return None


def product_json_price(product):

    if not isinstance(
        product,
        dict,
    ):
        return None

    offers = product.get(
        "offers"
    )

    if isinstance(
        offers,
        dict,
    ):
        offers = [offers]

    if not isinstance(
        offers,
        list,
    ):
        return None

    for offer in offers:

        if not isinstance(
            offer,
            dict,
        ):
            continue

        for key in [
            "price",
            "lowPrice",
            "salePrice",
            "sellingPrice",
            "currentPrice",
        ]:

            value = clean_price(
                offer.get(key)
            )

            if value is not None:
                return value

    return None


# =========================================================
# HTML PARSER
# =========================================================

def extract_html_data(
    html,
    url,
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

    # -----------------------------------------------------
    # JSON-LD
    # -----------------------------------------------------

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

            payload = json.loads(
                raw
            )

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

        if price is None:

            price = product_json_price(
                product
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
            isinstance(
                image,
                list,
            )
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
            or product.get("mpn")
            or product.get("sku")
        )

    # -----------------------------------------------------
    # TITLE
    # -----------------------------------------------------

    if not title:

        node = soup.find(
            "meta",
            property="og:title",
        )

        if node:

            title = node.get(
                "content"
            )

    # -----------------------------------------------------
    # IMAGE
    # -----------------------------------------------------

    if not image_url:

        node = soup.find(
            "meta",
            property="og:image",
        )

        if node:

            image_url = node.get(
                "content"
            )

    # -----------------------------------------------------
    # META PRICE
    # -----------------------------------------------------

    if price is None:

        selectors = [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
        ]

        for selector in selectors:

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

    # -----------------------------------------------------
    # AMAZON HTML OZEL
    # -----------------------------------------------------

    if price is None:

        amazon_selectors = [
            ".priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
            "#corePrice_feature_div .a-offscreen",
            ".apexPriceToPay .a-offscreen",
            ".a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
        ]

        for selector in amazon_selectors:

            nodes = soup.select(
                selector
            )

            for node in nodes[:20]:

                candidate = clean_price(
                    node.get_text(
                        " ",
                        strip=True,
                    )
                )

                if candidate:

                    price = candidate
                    break

            if price:
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
# NORMAL HTTP
# =========================================================

async def scrape_http(url):

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
            response.text,
            final_url,
        )

        return (
            final_url,
            data,
        )


# =========================================================
# PLAYWRIGHT
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
                    timeout=12000,
                )

            except PlaywrightTimeoutError:

                await page.wait_for_timeout(
                    4000
                )

            final_url = page.url

            store = detect_store(
                final_url
            )

            html = await page.content()

            data = extract_html_data(
                html,
                final_url,
            )

            # -------------------------------------------------
            # BODY TEXT
            # -------------------------------------------------

            try:

                body_text = (
                    await page.locator(
                        "body"
                    ).text_content(
                        timeout=5000
                    )
                    or ""
                )

            except Exception:

                body_text = ""

            print(
                "=" * 70
            )

            print(
                "STORE:",
                store
            )

            print(
                "PAGE TITLE:",
                await page.title()
            )

            print(
                "HTML LENGTH:",
                len(html)
            )

            # =================================================
            # AMAZON
            # =================================================

            if store == "Amazon Türkiye":

                print(
                    "AMAZON MODE"
                )

                amazon_selectors = [
                    ".priceToPay .a-offscreen",
                    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                    "#corePrice_feature_div .a-price .a-offscreen",
                    ".apexPriceToPay .a-offscreen",
                    ".a-price .a-offscreen",
                    "#priceblock_ourprice",
                    "#priceblock_dealprice",
                ]

                for selector in (
                    amazon_selectors
                ):

                    try:

                        locator = (
                            page.locator(
                                selector
                            )
                        )

                        count = (
                            await locator.count()
                        )

                        print(
                            "SELECTOR:",
                            selector,
                            "COUNT:",
                            count,
                        )

                        for i in range(
                            min(
                                count,
                                15,
                            )
                        ):

                            node = (
                                locator.nth(i)
                            )

                            # BURASI ONEMLI:
                            # inner_text degil
                            # text_content kullaniyoruz.

                            text = (
                                await node.text_content(
                                    timeout=1500
                                )
                                or ""
                            )

                            print(
                                "PRICE TEXT:",
                                repr(text),
                            )

                            candidate = clean_price(
                                text
                            )

                            if candidate:

                                data["price"] = (
                                    candidate
                                )

                                print(
                                    "AMAZON PRICE FOUND:",
                                    candidate,
                                )

                                break

                        if (
                            data["price"]
                            is not None
                        ):
                            break

                    except Exception as e:

                        print(
                            "AMAZON SELECTOR ERROR:",
                            selector,
                            repr(e),
                        )

                # ---------------------------------------------
                # AMAZON BODY FALLBACK
                # ---------------------------------------------

                if data["price"] is None:

                    body_prices = (
                        find_tl_prices(
                            body_text
                        )
                    )

                    print(
                        "BODY TL PRICES:",
                        body_prices[:30],
                    )

                    if body_prices:

                        # En cok tekrar eden fiyat genelde
                        # ana urun fiyatidir.
                        counts = {}

                        for p in body_prices:

                            counts[p] = (
                                counts.get(
                                    p,
                                    0,
                                )
                                + 1
                            )

                        sorted_prices = sorted(
                            counts.items(),
                            key=lambda x: (
                                -x[1],
                                x[0],
                            ),
                        )

                        data["price"] = (
                            sorted_prices[0][0]
                        )

                        print(
                            "AMAZON BODY PRICE:",
                            data["price"],
                        )

            # =================================================
            # HEPSIBURADA
            # =================================================

            elif store == "Hepsiburada":

                print(
                    "HEPSIBURADA MODE"
                )

                hb_selectors = [
                    '[data-test-id="price-current-price"]',
                    '[data-test-id*="current-price"]',
                    '[data-test-id*="price"]',
                    '[class*="currentPrice"]',
                    '[class*="price"]',
                    '[class*="Price"]',
                ]

                for selector in (
                    hb_selectors
                ):

                    try:

                        locator = (
                            page.locator(
                                selector
                            )
                        )

                        count = (
                            await locator.count()
                        )

                        for i in range(
                            min(
                                count,
                                30,
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

                            candidate = clean_price(
                                text
                            )

                            if candidate:

                                data["price"] = (
                                    candidate
                                )

                                print(
                                    "HB PRICE FOUND:",
                                    candidate,
                                )

                                break

                        if data["price"]:
                            break

                    except Exception:
                        continue

                # Hepsiburada body fallback
                if data["price"] is None:

                    body_prices = (
                        find_tl_prices(
                            body_text
                        )
                    )

                    print(
                        "HB BODY PRICES:",
                        body_prices[:30],
                    )

                    if body_prices:

                        counts = {}

                        for p in body_prices:

                            counts[p] = (
                                counts.get(
                                    p,
                                    0,
                                )
                                + 1
                            )

                        ordered = sorted(
                            counts.items(),
                            key=lambda x: (
                                -x[1],
                                x[0],
                            ),
                        )

                        data["price"] = (
                            ordered[0][0]
                        )

            # =================================================
            # TRENDYOL
            # =================================================

            elif store == "Trendyol":

                selectors = [
                    ".prc-dsc",
                    ".prc-slg",
                    '[class*="price"]',
                ]

                for selector in selectors:

                    try:

                        locator = (
                            page.locator(
                                selector
                            )
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
                                    timeout=1000
                                )
                                or ""
                            )

                            candidate = clean_price(
                                text
                            )

                            if candidate:

                                data["price"] = (
                                    candidate
                                )

                                break

                        if data["price"]:
                            break

                    except Exception:
                        pass

            # =================================================
            # N11 / DIGER
            # =================================================

            else:

                selectors = [
                    '[itemprop="price"]',
                    '.newPrice',
                    '.price',
                    '[class*="price"]',
                ]

                for selector in selectors:

                    try:

                        locator = (
                            page.locator(
                                selector
                            )
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
                                    timeout=1000
                                )
                                or ""
                            )

                            candidate = clean_price(
                                text
                            )

                            if candidate:

                                data["price"] = (
                                    candidate
                                )

                                break

                        if data["price"]:
                            break

                    except Exception:
                        pass

            # =================================================
            # TITLE
            # =================================================

            if not data["title"]:

                title_selectors = [
                    "#productTitle",
                    "h1",
                    '[data-test-id="product-name"]',
                ]

                for selector in (
                    title_selectors
                ):

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
                                    timeout=1500
                                )
                                or ""
                            )

                            text = text.strip()

                            if text:

                                data["title"] = (
                                    text
                                )

                                break

                    except Exception:
                        pass

            # =================================================
            # IMAGE
            # =================================================

            if not data["image_url"]:

                image_selectors = [
                    "#landingImage",
                    "#imgTagWrapperId img",
                    'img[itemprop="image"]',
                ]

                for selector in (
                    image_selectors
                ):

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
                "FINAL PRICE:",
                data["price"],
            )

            print(
                "FINAL TITLE:",
                data["title"],
            )

            print(
                "=" * 70
            )

            return (
                final_url,
                data,
            )

        finally:

            await context.close()

            await browser.close()


# =========================================================
# ANA FONKSIYON
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

    http_error = None

    # =====================================================
    # 1 - HTTP
    # =====================================================

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
                "HTTP PRICE FOUND:",
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
    # 2 - CHROMIUM
    # =====================================================

    try:

        final_url, data = (
            await scrape_browser(
                url
            )
        )

        if not data["title"]:

            data["title"] = "Ürün"

        if data["price"] is None:

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

    except Exception as e:

        print(
            "BROWSER ERROR:",
            repr(e),
        )

        if http_error:

            raise RuntimeError(
                f"HTTP başarısız ({http_error}); "
                f"Chromium da başarısız ({e})"
            )

        raise RuntimeError(
            f"Chromium başarısız ({e})"
        )
