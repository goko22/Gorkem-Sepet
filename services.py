from sqlalchemy.orm import Session

import models

from comparison import compare_product
from scraper import scrape_product
from utils import normalize_title


async def preview_product(url: str):
    return await scrape_product(url)


async def create_product_from_url(
    db: Session,
    url: str,
    quantity: int = 1,
):
    scraped = await preview_product(url)

    existing = (
        db.query(models.Product)
        .filter(models.Product.source_url == scraped.url)
        .first()
    )

    if existing:
        existing.quantity = min(
            99,
            existing.quantity + quantity,
        )

        existing.auto_price = scraped.price

        # Manuel override varsa scraper fiyatı ekrandaki fiyatı ezemez.
        if existing.manual_price is None:
            existing.source_price = scraped.price

        existing.title = scraped.title
        existing.normalized_title = normalize_title(scraped.title)

        if scraped.image_url:
            existing.image_url = scraped.image_url
        if scraped.brand:
            existing.brand = scraped.brand
        if scraped.model:
            existing.model = scraped.model
        if scraped.variant_text:
            existing.variant_text = scraped.variant_text

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
        auto_price=scraped.price,
        manual_price=None,
        quantity=quantity,
        variant_text=scraped.variant_text,
    )

    db.add(product)
    db.flush()

    db.add(
        models.Offer(
            product_id=product.id,
            store=scraped.store,
            title=scraped.title,
            price=scraped.price,
            url=scraped.url,
            image_url=scraped.image_url,
            match_score=1.0,
        )
    )

    db.commit()
    db.refresh(product)
    return product


async def compare_and_save_offers(
    db: Session,
    product: models.Product,
):
    results = await compare_product(
        title=product.title,
        source_store=product.source_store,
        source_url=product.source_url,
        source_price=product.source_price,
        brand=product.brand,
        model=product.model,
        variant_text=product.variant_text,
    )

    # Eski comparison tekliflerini temizle. Kaynak teklif de yeniden yazılır.
    (
        db.query(models.Offer)
        .filter(models.Offer.product_id == product.id)
        .delete(synchronize_session=False)
    )

    for result in results:
        db.add(
            models.Offer(
                product_id=product.id,
                store=result.store,
                title=result.title,
                price=result.price,
                url=result.url,
                image_url=result.image_url,
                match_score=result.score,
            )
        )

    db.commit()
    db.refresh(product)
    return results
