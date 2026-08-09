import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

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


# ---------------------------------------------------------
# YARDIMCI FONKSIYONLAR
# ---------------------------------------------------------

def extract_product_code(url: str):
    # Hepsiburada
    match = re.search(r"(HBCV[A-Z0-9]+)", url, re.I)

    if match:
        return match.group(1).upper()

    # Amazon ASIN
    match = re.search(
        r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        url,
        re.I
    )

    if match:
        return match.group(1).upper()

    return None


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


def get_price_from_product_json(product):
    if not isinstance(product, dict):
        return None

    offers = product.get("offers")

    if isinstance(offers, dict):
        offers = [offers]

    if not isinstance(offers, list):
        return None

    for offer in offers:

        if not isinstance(offer, dict):
            continue

        for key in [
            "price",
            "lowPrice",
            "highPrice",
            "salePrice",
            "sellingPrice",
        ]:

            price = parse_price(offer.get(key))

            if price is not None:
                return price

    return None


def find_prices_in_json(data, product_code=None):
    results = []

    interesting_keys = [
        "price",
        "currentprice",
        "saleprice",
        "sellingprice",
        "discountedprice",
        "finalprice",
        "buyboxprice",
        "merchantprice",
        "unitprice",
        "lowprice",
    ]

    try:
        serialized = json.dumps(
            data,
            ensure_ascii=False
        ).lower()
    except Exception:
        serialized = ""

    # Ürün kodumuz varsa JSON gerçekten bu ürüne ait olsun.
    if (
        product_code
        and product_code.lower() not in serialized
    ):
        return []

    def walk(obj, path=""):

        if isinstance(obj, dict):

            for key, value in obj.items():

                normalized = re.sub(
                    r"[^a-z]",
                    "",
                    str(key).lower()
                )

                new_path = (
                    f"{path}.{key}"
                    if path
                    else str(key)
                )

                if any(
                    word in normalized
                    for word in interesting_keys
                ):

                    if isinstance(
                        value,
                        (str, int, float)
                    ):

                        price = parse_price(value)

                        if (
                            price is not None
                            and 1 <= price <= 50_000_000
                        ):

                            score = 10

                            if any(
                                x in normalized
                                for x in [
                                    "current",
                                    "selling",
                                    "sale",
                                    "discount",
                                    "final",
                                    "buybox",
                                ]
                            ):
                                score += 10

                            results.append(
                                (
                                    score,
                                    price,
                                    new_path
                                )
                            )

                walk(
                    value,
                    new_path
                )

        elif isinstance(obj, list):

            for i, item in enumerate(obj[:1000]):

                walk(
                    item,
                    f"{path}[{i}]"
                )

    walk(data)

    return results


def extract_html_data(html: str, url: str):
    soup = BeautifulSoup(
        html,
        "html.parser"
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
        type="application/ld+json"
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
            data = json.loads(raw)
        except Exception:
            continue

        product = walk_for_product(data)

        if not product:
            continue

        title = (
            product.get("name")
            or title
        )

        price = (
            get_price_from_product_json(
                product
            )
            or price
        )

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
            dict
        ):
            brand = brand_data.get("name")

        elif isinstance(
            brand_data,
            str
        ):
            brand = brand_data

        model = (
            product.get("model")
            or product.get("mpn")
            or product.get("sku")
        )

    # -----------------------------------------------------
    # OPEN GRAPH
    # -----------------------------------------------------

    if not title:

        node = soup.find(
            "meta",
            property="og:title"
        )

        if node:
            title = node.get("content")

    if not image_url:

        node = soup.find(
            "meta",
            property="og:image"
        )

        if node:
            image_url = node.get("content")

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

            price = parse_price(
                node.get("content")
                or node.get("value")
            )

            if price:
                break

    if not title and soup.title:

        title = soup.title.get_text(
            " ",
            strip=True
        )

    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "brand": brand,
        "model": model,
    }


# ---------------------------------------------------------
# NORMAL HTTP
# ---------------------------------------------------------

async def scrape_http(url: str):

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=20
    ) as client:

        response = await client.get(url)

        response.raise_for_status()

        final_url = str(
            response.url
        )

        data = extract_html_data(
            response.text,
            final_url
        )

        return final_url, data


# ---------------------------------------------------------
# PLAYWRIGHT / CHROMIUM
# ---------------------------------------------------------

async def scrape_browser(url: str):

    product_code = (
        extract_product_code(url)
    )

    captured_prices = []

    network_urls = []

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
            user_agent=HEADERS["User-Agent"],
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

        # -------------------------------------------------
        # NETWORK RESPONSE DINLE
        # -------------------------------------------------

        async def inspect_response(
            response
        ):

            try:

                url_lower = (
                    response.url.lower()
                )

                content_type = (
                    response.headers.get(
                        "content-type",
                        ""
                    ).lower()
                )

                if any(
                    keyword in url_lower
                    for keyword in [
                        "product",
                        "price",
                        "offer",
                        "listing",
                        "merchant",
                        "buybox",
                    ]
                ):
                    network_urls.append(
                        f"{response.status} "
                        f"{response.url}"
                    )

                if (
                    "json"
                    not in content_type
                ):
                    return

                body = (
                    await response.body()
                )

                if (
                    not body
                    or len(body)
                    > 5_000_000
                ):
                    return

                text = body.decode(
                    "utf-8",
                    errors="ignore"
                )

                if (
                    product_code
                    and
                    product_code.lower()
                    not in text.lower()
                ):
                    return

                try:
                    payload = json.loads(
                        text
                    )
                except Exception:
                    return

                candidates = (
                    find_prices_in_json(
                        payload,
                        product_code
                    )
                )

                for candidate in candidates:

                    captured_prices.append(
                        candidate
                    )

            except Exception:

                pass

        def on_response(response):

            asyncio.create_task(
                inspect_response(
                    response
                )
            )

        page.on(
            "response",
            on_response
        )

        # -------------------------------------------------
        # SAYFAYI AC
        # -------------------------------------------------

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000
            )

            try:

                await page.wait_for_load_state(
                    "networkidle",
                    timeout=12000
                )

            except PlaywrightTimeoutError:

                await page.wait_for_timeout(
                    5000
                )

            final_url = page.url

            store = detect_store(
                final_url
            )

            html = await page.content()

            data = extract_html_data(
                html,
                final_url
            )

            # ---------------------------------------------
            # DEBUG
            # ---------------------------------------------

            print(
                "\n"
                + "=" * 80
            )

            print(
                "STORE:",
                store
            )

            print(
                "FINAL URL:",
                final_url
            )

            print(
                "PAGE TITLE:",
                await page.title()
            )

            print(
                "HTML LENGTH:",
                len(html)
            )

            try:

                body_text = (
                    await page.locator(
                        "body"
                    ).inner_text(
                        timeout=5000
                    )
                )

            except Exception as e:

                body_text = (
                    f"BODY ERROR: {e}"
                )

            print(
                "BODY START:"
            )

            print(
                body_text[:5000]
            )

            # ---------------------------------------------
            # DOM SELECTORLARI
            # ---------------------------------------------

            selectors = []

            if store == "Hepsiburada":

                selectors = [
                    '[data-test-id="price-current-price"]',
                    '[data-test-id*="price"]',
                    '[class*="currentPrice"]',
                    '[class*="price"]',
                    '[class*="Price"]',
                ]

            elif store == "Amazon Türkiye":

                selectors = [
                    '.priceToPay .a-offscreen',
                    '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
                    '#corePrice_feature_div .a-price .a-offscreen',
                    '#priceblock_ourprice',
                    '#priceblock_dealprice',
                    '.a-price .a-offscreen',
                ]

            elif store == "Trendyol":

                selectors = [
                    '.prc-dsc',
                    '.prc-slg',
                    '[class*="price"]',
                ]

            elif store == "N11":

                selectors = [
                    '.newPrice ins',
                    '.price',
                    '[class*="price"]',
                ]

            else:

                selectors = [
                    '[itemprop="price"]',
                    '[class*="price"]',
                ]

            # ---------------------------------------------
            # DOM FIYAT
            # ---------------------------------------------

            if data["price"] is None:

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
                                20
                            )
                        ):

                            node = (
                                locator.nth(i)
                            )

                            text = (
                                await node.inner_text(
                                    timeout=1000
                                )
                            )

                            candidate = (
                                parse_price(
                                    text
                                )
                            )

                            if candidate:

                                print(
                                    "DOM PRICE:",
                                    candidate,
                                    selector
                                )

                                data["price"] = (
                                    candidate
                                )

                                break

                        if (
                            data["price"]
                            is not None
                        ):
                            break

                    except Exception:

                        continue

            # ---------------------------------------------
            # NETWORK FIYAT
            # ---------------------------------------------

            if captured_prices:

                captured_prices.sort(
                    key=lambda x: (
                        -x[0],
                        x[1]
                    )
                )

                print(
                    "NETWORK PRICE CANDIDATES:"
                )

                for candidate in (
                    captured_prices[:20]
                ):

                    print(candidate)

                # Network JSON exact product code ile
                # eşleştiği için generic DOM'dan daha
                # güvenilir.
                data["price"] = (
                    captured_prices[0][1]
                )

            # ---------------------------------------------
            # TITLE
            # ---------------------------------------------

            if not data["title"]:

                for selector in [
                    "h1",
                    "#productTitle",
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
                                await node.inner_text(
                                    timeout=1500
                                )
                            )

                            if text.strip():

                                data["title"] = (
                                    text.strip()
                                )

                                break

                    except Exception:

                        pass

            # ---------------------------------------------
            # IMAGE
            # ---------------------------------------------

            if not data["image_url"]:

                try:

                    image = (
                        page.locator(
                            "#landingImage"
                        ).first
                    )

                    if (
                        await image.count()
                    ):

                        data["image_url"] = (
                            await image.get_attribute(
                                "src"
                            )
                        )

                except Exception:

                    pass

            # ---------------------------------------------
            # LOG NETWORK
            # ---------------------------------------------

            print(
                "NETWORK URLS:"
            )

            for item in (
                network_urls[-100:]
            ):

                print(item)

            print(
                "FINAL PRICE:",
                data["price"]
            )

            print(
                "=" * 80
                + "\n"
            )

            return (
                final_url,
                data
            )

        finally:

            await page.wait_for_timeout(
                500
            )

            await context.close()

            await browser.close()


# ---------------------------------------------------------
# ANA SCRAPER
# ---------------------------------------------------------

async def scrape_product(
    url: str
) -> ScrapedProduct:

    if not url.startswith(
        ("http://", "https://")
    ):

        raise ValueError(
            "Geçerli bir ürün linki gir."
        )

    http_error = None

    # -----------------------------------------------------
    # 1. NORMAL HTTP DENE
    # -----------------------------------------------------

    try:

        final_url, data = (
            await scrape_http(url)
        )

        if (
            data["price"]
            is not None
            and data["title"]
        ):

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
                method="http",
            )

    except Exception as e:

        http_error = e

        print(
            "HTTP SCRAPER ERROR:",
            repr(e)
        )

    # -----------------------------------------------------
    # 2. CHROMIUM DENE
    # -----------------------------------------------------

    try:

        final_url, data = (
            await scrape_browser(url)
        )

        if not data["title"]:

            data["title"] = "Ürün"

        if data["price"] is None:

            raise RuntimeError(
                "Sayfa açıldı ancak fiyat "
                "DOM veya network içinde "
                "bulunamadı. Render loglarını "
                "kontrol et."
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
            "BROWSER SCRAPER ERROR:",
            repr(browser_error)
        )

        if http_error:

            raise RuntimeError(
                "HTTP başarısız: "
                f"{http_error}; "
                "Chromium da başarısız: "
                f"{browser_error}"
            )

        raise RuntimeError(
            "Chromium başarısız: "
            f"{browser_error}"
        )
