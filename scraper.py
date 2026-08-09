import json
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Optional
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
        if "@graph" in data:
            found = _walk_json_for_product(data["@graph"])
            if found:
                return found
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
            if not isinstance(offer, dict):
                continue
            candidate = offer.get("price") or offer.get("lowPrice")
            parsed = parse_price(candidate)
            if parsed is not None:
                price = parsed
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
        meta_selectors = [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
        ]
        for selector in meta_selectors:
            node = soup.select_one(selector)
            if node:
                price = parse_price(node.get("content") or node.get("value"))
                if price:
                    break

    if price is None:
        selectors = [
            '[itemprop="price"]',
            '[data-testid*="price"]',
            '[data-test-id*="price"]',
            '[class*="price"]',
            '[class*="Price"]',
        ]
        for selector in selectors:
            for node in soup.select(selector)[:40]:
                parsed = parse_price(
                    node.get("content")
                    or node.get("value")
                    or node.get_text(" ", strip=True)
                )
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
                txt = (await locator.inner_text(timeout=1500)).strip()
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
                value = await locator.get_attribute(attr, timeout=1500)
                if value:
                    return value
        except Exception:
            pass
    return None

async def _scrape_browser(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="tr-TR",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 1200},
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            final_url = page.url
            html = await page.content()
            data = _extract_from_html(html, final_url)
            store = detect_store(final_url)

            # Store-specific visible selectors. No stealth/proxy bypassing:
            # we simply read content a normal headless browser can access.
            selector_map = {
                "Hepsiburada": [
                    '[data-test-id="price-current-price"]',
                    '[data-test-id*="price"]',
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
                txt = await _first_text(page, selector_map.get(store, ['[class*="price"]']))
                data["price"] = parse_price(txt)

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
                    'meta[property="og:image"]',
                ], "src")
                if not data["image_url"]:
                    data["image_url"] = await _first_attr(
                        page, ['meta[property="og:image"]'], "content"
                    )

            return final_url, data

        finally:
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
            raise ValueError("Sayfa açıldı fakat fiyat bulunamadı.")

        return ScrapedProduct(
            title=data["title"][:500],
            store=detect_store(final_url),
            url=final_url,
            price=data.get("price"),
            image_url=data.get("image_url"),
            brand=data.get("brand"),
            model=data.get("model"),
            method="browser",
        )
    except Exception as browser_error:
        if http_error:
            raise RuntimeError(
                f"HTTP denemesi başarısız ({http_error}); "
                f"tarayıcı denemesi de başarısız ({browser_error})"
            )
        raise RuntimeError(f"Tarayıcıyla fiyat alınamadı: {browser_error}")
