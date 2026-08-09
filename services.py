from sqlalchemy.orm import Session

import models

from comparison import (
    MatchResult,
    compare_product,
    same_product_pair_score,
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
        existing.quantity = min(99, existing.quantity + quantity)
        existing.auto_price = scraped.price

        # Manuel override varsa scraper fiyatı ekrandaki efektif fiyatı ezemez.
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

        # Aynı URL daha önce başka ürünlerin karşılaştırma sonuçlarında yer aldıysa
        # efektif fiyatı oralarda da güncel tut.
        (
            db.query(models.Offer)
            .filter(models.Offer.url == existing.source_url)
            .update(
                {models.Offer.price: existing.source_price},
                synchronize_session=False,
            )
        )

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


def _cart_product_by_url(db: Session) -> dict[str, models.Product]:
    """Sepetteki kaynak URL -> ürün haritası.

    Buradaki amaç manual_price varsa karşılaştırma teklifinin scraper fiyatını değil
    kullanıcının kilitlediği efektif fiyatı göstermesidir.
    """
    return {
        p.source_url.rstrip("/"): p
        for p in db.query(models.Product).all()
        if p.source_url
    }


async def compare_and_save_offers(
    db: Session,
    product: models.Product,
):
    # 1) Dış mağaza aramalarını çalıştır.
    results = await compare_product(
        title=product.title,
        source_store=product.source_store,
        source_url=product.source_url,
        source_price=product.source_price,
        brand=product.brand,
        model=product.model,
        variant_text=product.variant_text,
    )

    # 2) Sepette zaten bulunan ürünleri yön-bağımsız eşleştir.
    # Uzun Amazon başlığı ↔ kısa Wraith başlığı gibi durumlar artık iki yönde de
    # aynı sonucu verir.
    peers = (
        db.query(models.Product)
        .filter(models.Product.id != product.id)
        .all()
    )

    for peer in peers:
        score, reason = same_product_pair_score(
            product.title,
            peer.title,
            product.brand,
            peer.brand,
            product.model,
            peer.model,
            product.variant_text,
            peer.variant_text,
        )

        # Sepet içi eşleşmede yüksek güven eşiği. Şüpheli sonucu göstermektense
        # hiç göstermemek daha doğru.
        if score < 0.82:
            # Unrelated basket products are expected to be rejected.
            # Do not flood Render logs unless deep matcher debugging is enabled.
            import os
            if os.getenv("COMPARE_DEBUG_REJECTS", "").strip() == "1":
                print(
                    "COMPARE CART PEER REJECT:",
                    peer.source_store,
                    peer.title,
                    "score=",
                    round(score, 3),
                    reason,
                    flush=True,
                )
            continue

        results.append(
            MatchResult(
                store=peer.source_store,
                title=peer.title,
                price=peer.source_price,  # manual override dahil efektif fiyat
                url=peer.source_url,
                image_url=peer.image_url,
                score=score,
                reason="Sepette doğrulandı: " + reason,
            )
        )

    # 3) Sepette bulunan bir URL dış aramadan da geldiyse daima sepetteki efektif
    # fiyatı kullan. Böylece manuel fiyat karşılaştırma satırlarında da anında geçer.
    cart_by_url = _cart_product_by_url(db)
    for result in results:
        cart_product = cart_by_url.get(result.url.rstrip("/"))
        if cart_product is not None:
            result.price = cart_product.source_price
            result.title = cart_product.title
            result.image_url = cart_product.image_url

    # 4) Aynı mağaza + URL iki farklı kaynaktan geldiyse tekilleştir.
    unique: dict[tuple[str, str], MatchResult] = {}
    for result in results:
        key = (result.store, result.url.rstrip("/"))
        old = unique.get(key)
        if old is None or result.score > old.score:
            unique[key] = result

    final_results = sorted(
        unique.values(),
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )

    # 5) Bu ürünün eski karşılaştırma tekliflerini atomik biçimde yenile.
    (
        db.query(models.Offer)
        .filter(models.Offer.product_id == product.id)
        .delete(synchronize_session=False)
    )

    for result in final_results:
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
    return final_results
