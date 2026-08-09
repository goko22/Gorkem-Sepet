from .generic import GenericScraper

# İlk sürüm: bütün siteler GenericScraper üzerinden çalışır.
# Sonraki adımda mağaza bazlı özel scraper'lar bu registry'e eklenecek.
SCRAPER_REGISTRY = {}

def get_scraper(store_name: str):
    scraper_cls = SCRAPER_REGISTRY.get(store_name, GenericScraper)
    return scraper_cls()
