from sqlalchemy.orm import Session

import models

from comparison import (
    MatchResult,
    compare_product,
    deterministic_score,
    hard_conflict,
    identity_words,
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

    # 2) Sepette zaten bulunan ürünleri de gerçek teklif adayı olarak kullan.
    # Böylece aynı ürün Amazon + Wraith gibi iki farklı linkten sepete
    # eklenmişse, iki kart da birbirinin fiyatını görebilir.
    peers = (
        db.query(models.Product)
        .filter(models.Product.id != product.id)
        .all()
    )

    source_identity = identity_words(product.title)

    for peer in peers:
        conflict, conflict_reason = hard_conflict(
            product.title,
            peer.title,
            product.model,
            product.variant_text,
            peer.variant_text,
        )

        if conflict:
            print(
                "COMPARE CART PEER REJECT:",
                peer.source_store,
                peer.title,
                conflict_reason,
                flush=True,
            )
            continue

        score, reason = deterministic_score(
            product.title,
            peer.title,
            product.brand,
            product.model,
            product.variant_text,
            peer.variant_text,
        )

        shared_identity = source_identity & identity_words(peer.title)

        # Sepet içi eşleştirmede yanlış pozitiften kaçınmak için:
        # - normal katı eşik, veya
        # - en az 3 ayırt edici ortak kimlik kelimesi + orta eşik gerekir.
        # hard_conflict zaten 360/240, 1TB/2TB, SE/non-SE gibi
        # kritik çelişkileri yukarıda reddeder.
        peer_verified = (
            score >= 0.72
            or (len(shared_identity) >= 3 and score >= 0.45)
        )

        if not peer_verified:
            continue

        # Kullanıcıya gösterilen güven değeri, yalnızca ham F1 değil;
        # katı çelişki kontrolü + kimlik ortaklığı da hesaba katılır.
        display_score = min(0.99, max(score, 0.90 if len(shared_identity) >= 3 else score))

        results.append(
            MatchResult(
                store=peer.source_store,
                title=peer.title,
                price=peer.source_price,
                url=peer.source_url,
                image_url=peer.image_url,
                score=display_score,
                reason="Sepette doğrulandı: " + reason,
            )
        )

    # Aynı mağaza + URL iki farklı kaynaktan geldiyse tekilleştir.
    unique = {}
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

    # Eski comparison tekliflerini temizle. Kaynak teklif de yeniden yazılır.
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
