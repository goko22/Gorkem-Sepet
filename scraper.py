import json
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Optional
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

def _walk_json_for_product(data):
    if isinstance(data, dict):
        if data.get("@type") == "Product":
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

async def scrape_product(url: str) -> ScrapedProduct:
    if not url.startswith(("http://", "https://")):
        raise ValueError("Geçerli bir http/https ürün linki gir.")

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=25.0,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "html.parser")
    store = detect_store(final_url)

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

        if title:
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
        selectors = [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[itemprop="price"]',
            '[itemprop="price"]',
            '[data-testid*="price"]',
            '[class*="price"]',
        ]
        for selector in selectors:
            for node in soup.select(selector)[:15]:
                candidate = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
                parsed = parse_price(candidate)
                if parsed is not None:
                    price = parsed
                    break
            if price is not None:
                break

    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    if not title:
        title = "Ürün"

    return ScrapedProduct(
        title=title[:500],
        store=store,
        url=final_url,
        price=price,
        image_url=image_url,
        brand=brand,
        model=model,
    )
