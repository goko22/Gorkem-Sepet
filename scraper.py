import asyncio
import json
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass
from typing import Optional, Any
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from utils import detect_store, parse_price

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    method: str = "http"

def _walk_json_for_product(data):
    if isinstance(data, dict):
        t = data.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return data
        for value in data.values():
            found = _walk_json_for_product(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _walk_json_for_product(item)
            if found:
                return found
    return None

def _extract_product_code(url: str):
    m = re.search(r"(HBCV[A-Z0-9]+)", url, re.I)
    if m:
        return m.group(1).upper()

    m = re.search(r"/dp/([A-Z0-9]{8,})", url, re.I)
    if m:
        return m.group(1).upper()

    return None

PRICE_KEYS = {
    "price", "currentprice", "saleprice", "sellingprice", "discountedprice",
    "finalprice", "actualprice", "unitprice", "amount", "lowprice",
    "merchantprice", "listingprice", "buyboxprice"
}

def _key_norm(key: Any):
    return re.sub(r"[^a-z]", "", str(key).lower())

def _collect_price_candidates(obj, path="", candidates=None):
    if candidates is None:
        candidates = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            np = _key_norm(key)
            child_path = f"{path}.{key}" if path else str(key)

            if any(pk in np for pk in PRICE_KEYS):
                if isinstance(value, (int, float, str)):
                    p = parse_price(value)
                    if p is not None and 1 <= p <= 50_000_000:
                        score = 10
                        if "current" in np or "selling" in np or "sale" in np or "discount" in np:
                            score += 5
                        if "original" in np or "old" in np or "list" in np:
                            score -= 3
                        candidates.append((score, p, child_path))

            _collect_price_candidates(value, child_path, candidates)

    elif isinstance(obj, list):
        for i, value in enumerate(obj[:500]):
            _collect_price_candidates(value, f"{path}[{i}]", candidates)

    return candidates

def _best_price_from_json(obj, required_token=None):
    try:
        dumped = json.dumps(obj, ensure_ascii=False)
    except Exception:
        dumped = ""

    if required_token and required_token.lower() not in dumped.lower():
        return None

    candidates = _collect_price_candidates(obj)
    if not candidates:
        return None

    # Highest semantic score first; for ties choose the smaller positive price,
    # which usually corresponds to the current/sale price instead of old/list price.
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]

def _extract_from_html(html: str, final_url: str):
    soup = BeautifulSoup(html, "html.parser")
    title = None
    image_url = None
    price = None
    brand = None
    model = None

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        item = _walk_json_for_product(data)
        if not item:
            continue

        title = item.get("name") or title

        image = item.get("image")
        if isinstance(image, list) and image:
            image_url = image[0]
        elif isinstance(image, dict):
            image_url = image.get("url")
        elif isinstance(image, str):
            image_url = image

        brand_obj = item.get("brand")
        if isinstance(brand_obj, dict):
            brand = brand_obj.get("name")
        elif isinstance(brand_obj, str):
            brand = brand_obj

        model = item.get("model") or item.get("mpn") or item.get("sku")

        offers = item.get("offers")
        offer_candidates = offers if isinstance(offers, list) else [offers]
        for offer in offer_candidates:
            if isinstance(offer, dict):
                parsed = (
                    parse_price(offer.get("price"))
                    or parse_price(offer.get("lowPrice"))
                )
                if parsed is not None:
                    price = parsed
                    break

    # Scan embedded JSON/state scripts too, not only JSON-LD.
    if price is None:
        code = _extract_product_code(final_url)
        for script in soup.find_all("script"):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw or len(raw) < 20:
                continue

            if code and code.lower() not in raw.lower():
                continue

            # First try direct JSON.
            try:
                data = json.loads(raw)
                candidate = _best_price_from_json(data, required_token=code)
                if candidate:
                    price = candidate
                    break
            except Exception:
                pass

            # Then inspect likely price fields in serialized JS state.
            patterns = [
                r'"(?:currentPrice|salePrice|sellingPrice|discountedPrice|finalPrice|price)"\s*:\s*"?(?P<p>\d[\d.,]*)"?',
                r"'(?:currentPrice|salePrice|sellingPrice|discountedPrice|finalPrice|price)'\s*:\s*'?(?P<p>\d[\d.,]*)'?",
            ]
            for pat in patterns:
                m = re.search(pat, raw, re.I)
                if m:
                    candidate = parse_price(m.group("p"))
                    if candidate:
                        price = candidate
                        break
            if price is not None:
                break

    if not title:
        meta = soup.find("meta", property="og:title")
        if meta and meta.get("content"):
            title = meta["content"].strip()

    if not image_url:
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            image_url = urljoin(final_url, meta["content"])

    if price is None:
        for selector in [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
            '[itemprop="price"]',
            '[data-testid*="price"]',
            '[data-test-id*="price"]',
            '[class*="price"]',
            '[class*="Price"]',
        ]:
            for node in soup.select(selector)[:50]:
                candidate = (
                    node.get("content")
                    or node.get("value")
                    or node.get_text(" ", strip=True)
                )
                parsed = parse_price(candidate)
                if parsed is not None:
                    price = parsed
                    break
            if price is not None:
                break

    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    return {
        "title": title,
        "image_url": image_url,
        "price": price,
        "brand": brand,
        "model": model,
    }

async def _scrape_http(url: str):
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        final_url = str(response.url)
        data = _extract_from_html(response.text, final_url)
        return final_url, data

async def _first_text(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                txt = (await locator.inner_text(timeout=1800)).strip()
                if txt:
                    return txt
        except Exception:
            pass
    return None

async def _first_attr(page, selectors, attr):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                value = await locator.get_attribute(attr, timeout=1800)
                if value:
                    return value
        except Exception:
            pass
    return None

async def _scrape_browser(url: str):
    product_code = _extract_product_code(url)
    network_prices = []
    network_debug = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            },
        )
        page = await context.new_page()

        async def inspect_response(response):
            try:
                ctype = (response.headers.get("content-type") or "").lower()
                rurl = response.url.lower()

                if "json" not in ctype and not any(x in rurl for x in [
                    "product", "price", "listing", "merchant", "offer", "buybox"
                ]):
                    return

                body = await response.body()
                if not body or len(body) > 5_000_000:
                    return

                text = body.decode("utf-8", errors="ignore")

                # Require product token when we have one. This avoids accidentally
                # taking prices from recommendation widgets or unrelated products.
                if product_code and product_code.lower() not in text.lower():
                    return

                try:
                    payload = json.loads(text)
                except Exception:
                    return

                candidate = _best_price_from_json(
                    payload,
                    required_token=product_code,
                )
                if candidate is not None:
                    network_prices.append(candidate)
                    network_debug.append(response.url)
            except Exception:
                pass

        def response_handler(response):
            asyncio.create_task(inspect_response(response))

        page.on("response", response_handler)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Give product APIs/XHR time to finish.
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(5000)

            final_url = page.url
            html = await page.content()
            data = _extract_from_html(html, final_url)
            store = detect_store(final_url)

            selector_map = {
                "Hepsiburada": [
                    '[data-test-id="price-current-price"]',
                    '[data-test-id*="current-price"]',
                    '[data-test-id*="price"]',
                    '[class*="currentPrice"]',
                    '[class*="price"]',
                ],
                "Amazon Türkiye": [
                    '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
                    '#corePrice_feature_div .a-price .a-offscreen',
                    '.priceToPay .a-offscreen',
                    '#priceblock_ourprice',
                    '#priceblock_dealprice',
                    '.a-price .a-offscreen',
                ],
                "Trendyol": [
                    '.prc-dsc',
                    '.prc-slg',
                    '[class*="price"]',
                ],
                "N11": [
                    '.newPrice ins',
                    '.price',
                    '[class*="price"]',
                ],
            }

            if data.get("price") is None:
                txt = await _first_text(
                    page,
                    selector_map.get(store, ['[class*="price"]'])
                )
                data["price"] = parse_price(txt)

            # Network JSON is preferred over broad generic price-class scraping
            # when it is tied to the exact product code.
            if network_prices:
                data["price"] = sorted(network_prices)[0]

            if not data.get("title"):
                data["title"] = await _first_text(page, [
                    'h1',
                    '#productTitle',
                    '[data-test-id="product-name"]',
                ])

            if not data.get("image_url"):
                data["image_url"] = await _first_attr(page, [
                    '#landingImage',
                    'img[itemprop="image"]',
                ], "src")
                if not data["image_url"]:
                    data["image_url"] = await _first_attr(
                        page,
                        ['meta[property="og:image"]'],
                        "content",
                    )

            data["_network_sources"] = network_debug[:5]
            return final_url, data

        finally:
            await page.wait_for_timeout(300)
            await context.close()
            await browser.close()

async def scrape_product(url: str) -> ScrapedProduct:
    if not url.startswith(("http://", "https://")):
        raise ValueError("Geçerli bir http/https ürün linki gir.")

    http_error = None

    try:
        final_url, data = await _scrape_http(url)
        if data.get("price") is not None and data.get("title"):
            return ScrapedProduct(
                title=data["title"][:500],
                store=detect_store(final_url),
                url=final_url,
                price=data.get("price"),
                image_url=data.get("image_url"),
                brand=data.get("brand"),
                model=data.get("model"),
                method="http",
            )
    except Exception as e:
        http_error = e

    try:
        final_url, data = await _scrape_browser(url)

        if not data.get("title"):
            data["title"] = "Ürün"

        if data.get("price") is None:
            raise ValueError(
                "Sayfa açıldı fakat DOM, gömülü JSON ve network cevaplarında "
                "bu ürüne bağlı fiyat bulunamadı."
            )

        return ScrapedProduct(
            title=data["title"][:500],
            store=detect_store(final_url),
            url=final_url,
            price=data.get("price"),
            image_url=data.get("image_url"),
            brand=data.get("brand"),
            model=data.get("model"),
            method="browser+network",
        )
    except Exception as browser_error:
        if http_error:
            raise RuntimeError(
                f"HTTP başarısız ({http_error}); "
                f"Chromium/network denemesi de başarısız ({browser_error})"
            )
        raise RuntimeError(
            f"Chromium/network ile fiyat alınamadı: {browser_error}"
        )
