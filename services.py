from sqlalchemy.orm import Session
from . import models
from .scrapers.registry import get_scraper
from .utils import detect_store, normalize_title

async def preview_product(url: str):
    store = detect_store(url)
    scraper = get_scraper(store)
    return await scraper.scrape(url)

async def create_product_from_url(db: Session, url: str, quantity: int = 1):
    scraped = await preview_product(url)

    existing = db.query(models.Product).filter(models.Product.source_url == scraped.url).first()
    if existing:
        existing.quantity += quantity
        db.commit()
        db.refresh(existing)
        return existing

    product = models.Product(
        title=scraped.title,
        normalized_title=normalize_title(scraped.title),
        brand=scraped.brand,
        model=scraped.model,
        image_url=scraped.image_url,
        source_store=scraped.store,
        source_url=scraped.url,
        source_price=scraped.price,
        quantity=quantity,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    # Kaynak mağazayı ilk teklif olarak da kaydet.
    if scraped.price is not None:
        offer = models.Offer(
            product_id=product.id,
            store=scraped.store,
            title=scraped.title,
            price=scraped.price,
            url=scraped.url,
            image_url=scraped.image_url,
            match_score=1.0,
        )
        db.add(offer)
        db.commit()
        db.refresh(product)

    return product
