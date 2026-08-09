import json
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from .base import BaseScraper, ScrapedProduct
from app.utils import detect_store, parse_price

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

class GenericScraper(BaseScraper):
    async def scrape(self, url: str) -> ScrapedProduct:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        store = detect_store(str(r.url))

        title = None
        image_url = None
        price = None
        brand = None
        model = None

        # JSON-LD is usually the most stable first target.
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "Product":
                    title = title or item.get("name")
                    image = item.get("image")
                    if isinstance(image, list) and image:
                        image_url = image[0]
                    elif isinstance(image, str):
                        image_url = image
                    brand_obj = item.get("brand")
                    if isinstance(brand_obj, dict):
                        brand = brand_obj.get("name")
                    elif isinstance(brand_obj, str):
                        brand = brand_obj
                    model = item.get("model") or item.get("mpn")
                    offers = item.get("offers")
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        price = parse_price(str(offers.get("price") or ""))
                    break

        if not title:
            meta = soup.find("meta", property="og:title")
            title = meta.get("content", "").strip() if meta else None

        if not image_url:
            meta = soup.find("meta", property="og:image")
            if meta and meta.get("content"):
                image_url = urljoin(str(r.url), meta["content"])

        if price is None:
            selectors = [
                'meta[property="product:price:amount"]',
                'meta[itemprop="price"]',
                '[itemprop="price"]',
                '.price',
                '[class*="price"]',
            ]
            for selector in selectors:
                node = soup.select_one(selector)
                if not node:
                    continue
                candidate = node.get("content") or node.get_text(" ", strip=True)
                price = parse_price(candidate)
                if price:
                    break

        if not title:
            title = soup.title.get_text(strip=True) if soup.title else "Ürün"

        return ScrapedProduct(
            title=title[:500],
            store=store,
            url=str(r.url),
            price=price,
            image_url=image_url,
            brand=brand,
            model=model,
        )
