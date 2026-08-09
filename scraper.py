import json
import os
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

            number = float(raw)

            if 0 < number < 100_000_000:
                return number

        except Exception:
            pass

    return parse_price(text)


def walk_for_product(data):
    if isinstance(data, dict):
        product_type = data.get("@type")

        if product_type == "Product":
            return data

        if isinstance(product_type, list) and "Product" in product_type:
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
# HEPSIBURADA - PARSE API
# =========================================================

async def scrape_hepsiburada_api(url: str) -> ScrapedProduct:
    api_key = os.getenv("PARSE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "PARSE_API_KEY Render Environment Variables içinde tanımlı değil."
        )

    product_code = extract_hepsiburada_code(url)

    if not product_code:
        raise RuntimeError(
            "Hepsiburada ürün kodu URL içinden bulunamadı."
        )

    endpoint = (
        "https://api.parse.bot/scraper/"
        "a42a78b8-347f-4c69-8e83-135320d2b001/"
        "get_product_details"
    )

    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
    }

    param_candidates = [
        {
            "product_id": product_code
        },
        {
            "sku": product_code
        },
        {
            "url": url
        },
        {
            "product_url": url
        },
    ]

    payload = None
    last_status = None
    last_body = None

    async with httpx.AsyncClient(
        timeout=45,
        follow_redirects=True,
    ) as client:

        for params in param_candidates:
            print(
                "HEPSIBURADA PARSE TRY:",
                params,
            )

            response = await client.get(
                endpoint,
                headers=headers,
                params=params,
            )

            last_status = response.status_code
            last_body = response.text[:3000]

            print(
                "HEPSIBURADA PARSE STATUS:",
                response.status_code,
            )

            print(
                "HEPSIBURADA PARSE BODY:",
                last_body,
            )

            if response.status_code == 200:
                try:
                    payload = response.json()
                    break
                except Exception as e:
                    raise RuntimeError(
                        f"Parse API JSON cevabı okunamadı: {e}"
                    )

            if response.status_code == 422:
                continue

            if response.status_code == 429:
                raise RuntimeError(
                    "Parse API rate limit doldu. Birkaç dakika sonra tekrar dene."
                )

            if response.status_code == 401:
                raise RuntimeError(
                    "PARSE_API_KEY geçersiz veya yetkisiz."
                )

            if response.status_code == 403:
                raise RuntimeError(
                    "Parse API erişimi reddetti. API key/plan kontrol edilmeli."
                )

            response.raise_for_status()

    if payload is None:
        raise RuntimeError(
            "Hepsiburada API hiçbir parametreyi kabul etmedi. "
            f"Son durum: {last_status}. "
            f"Cevap: {last_body}"
        )

    print(
        "HEPSIBURADA PARSE SUCCESS:",
        json.dumps(
            payload,
            ensure_ascii=False,
        )[:5000],
    )

    data = payload.get("data", payload)

    if isinstance(data, list):
        if not data:
            raise RuntimeError(
                "Hepsiburada API boş sonuç döndürdü."
            )

        data = data[0]

    if not isinstance(data, dict):
        raise RuntimeError(
            "Hepsiburada API beklenmeyen veri tipi döndürdü."
        )

    title = (
        data.get("product_name")
        or data.get("name")
        or data.get("title")
    )

    price = clean_price(
        data.get("unit_price")
        or data.get("price")
        or data.get("priceText")
        or data.get("current_price")
        or data.get("sale_price")
    )

    image_url = (
        data.get("imageUrl")
        or data.get("image_url")
        or data.get("image")
    )

    if not image_url:
        images = data.get("images")

        if isinstance(images, list) and images:
            first = images[0]

            if isinstance(first, str):
                image_url = first

            elif isinstance(first, dict):
                image_url = (
                    first.get("url")
                    or first.get("imageUrl")
                    or first.get("src")
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
            "Hepsiburada API ürün adını döndürmedi. "
            f"Veri: {json.dumps(data, ensure_ascii=False)[:2000]}"
        )

    if price is None:
        raise RuntimeError(
            "Hepsiburada API fiyat döndürmedi. "
            f"Veri: {json.dumps(data, ensure_ascii=False)[:2000]}"
        )

    print(
        "HEPSIBURADA FINAL PRICE:",
        price,
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
# NORMAL HTML PARSER
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

        if isinstance(image, str):
            image_url = image

        elif isinstance(image, list) and image:
            image_url = image[0]

        elif isinstance(image, dict):
            image_url = image.get(
                "url"
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
                    or offer.get("lowPrice")
                    or offer.get("salePrice")
                    or offer.get("sellingPrice")
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

    if price is None:
        amazon_selectors = [
            ".priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
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

        return final_url, data


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
                    3000
                )

            final_url = page.url
            store = detect_store(
                final_url
            )

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

                        count = await locator.count()

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

                            candidate = clean_price(
                                text
                            )

                            if candidate:
                                data["price"] = candidate
                                break

                        if data["price"]:
                            break

                    except Exception:
                        pass

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

                        data["price"] = sorted(
                            counts.items(),
                            key=lambda x: (
                                -x[1],
                                x[0],
                            ),
                        )[0][0]

                except Exception:
                    pass

            if not data["title"]:
                for selector in [
                    "#productTitle",
                    "h1",
                    '[data-test-id="product-name"]',
                ]:
                    try:
                        node = page.locator(
                            selector
                        ).first

                        if await node.count():
                            text = (
                                await node
                                .text_content(
                                    timeout=1200
                                )
                                or ""
                            ).strip()

                            if text:
                                data["title"] = text
                                break

                    except Exception:
                        pass

            if not data["image_url"]:
                for selector in [
                    "#landingImage",
                    'img[itemprop="image"]',
                ]:
                    try:
                        node = page.locator(
                            selector
                        ).first

                        if await node.count():
                            src = await node.get_attribute(
                                "src"
                            )

                            if src:
                                data["image_url"] = src
                                break

                    except Exception:
                        pass

            print(
                "BROWSER STORE:",
                store,
            )

            print(
                "BROWSER FINAL PRICE:",
                data["price"],
            )

            return final_url, data

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
        ("http://", "https://")
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

        return await scrape_hepsiburada_api(
            url
        )

    # =====================================================
    # DIGER MAGAZALAR - HTTP
    # =====================================================

    http_error = None

    try:
        final_url, data = await scrape_http(
            url
        )

        if (
            data["price"] is not None
            and data["title"]
        ):
            print(
                "HTTP SUCCESS:",
                detect_store(final_url),
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
    # DIGER MAGAZALAR - CHROMIUM
    # =====================================================

    try:
        final_url, data = await scrape_browser(
            url
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
