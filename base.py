from dataclasses import dataclass
from typing import Optional

@dataclass
class ScrapedProduct:
    title: str
    store: str
    url: str
    price: Optional[float] = None
    image_url: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None

class BaseScraper:
    store_name = "Generic"

    async def scrape(self, url: str) -> ScrapedProduct:
        raise NotImplementedError
