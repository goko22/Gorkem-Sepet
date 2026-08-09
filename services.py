from sqlalchemy.orm import Session

import models

from comparison import (
    MatchResult,
    compare_product,
)
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
    """
    Fiyat karşılaştırması yalnızca comparison.py tarafından doğrulanmış
    dış mağaza sonuçlarını kaydeder.

    ÖNEMLİ:
    Eski sürüm sepetteki bütün diğer ürünleri de "peer" olarak karşılaştırıyordu.
    Bu, Ray-Ban ile Logitech gibi tamamen alakasız iki kartın yanlışlıkla
    birbirinin fiyatı gibi görünmesine yol açabiliyordu.

    Benzer ürünler artık ayrı /similar endpointinde çalışıyor; fiyat
    karşılaştırmasına ASLA karışmıyor.
    """
    results = await compare_product(
        title=product.title,
        source_store=product.source_store,
        source_url=product.source_url,
        source_price=product.source_price,
        brand=product.brand,
        model=product.model,
        variant_text=product.variant_text,
    )

    # Kaynak URL hiçbir zaman "karşılaştırma teklifi" olarak tekrar gösterilmesin.
    source_url = (product.source_url or "").rstrip("/")
    verified = []
    seen = set()

    for result in results:
        result_url = (result.url or "").rstrip("/")
        if not result_url or result_url == source_url:
            continue

        # Aynı mağaza+URL tekrarını temizle.
        key = (str(result.store or "").strip().lower(), result_url)
        if key in seen:
            continue
        seen.add(key)

        # Güvensiz / fiyatsız satırları kaydetme.
        if result.price is None or float(result.price) <= 0:
            continue
        if float(result.score or 0) < 0.84:
            continue

        verified.append(result)

    verified.sort(
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        )
    )

    # Eski yanlış teklifler dahil bu ürünün comparison havuzunu tamamen temizle.
    (
        db.query(models.Offer)
        .filter(models.Offer.product_id == product.id)
        .delete(synchronize_session=False)
    )

    for result in verified:
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

    print(
        "COMPARE SAVE VERIFIED:",
        product.id,
        product.title[:90],
        "offers=",
        len(verified),
        flush=True,
    )

    return verified

