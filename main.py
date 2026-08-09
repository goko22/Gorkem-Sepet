from pathlib import Path
import hmac
import os


from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
)

from fastapi.responses import (
    FileResponse,
)

from sqlalchemy import (
    inspect,
    text,
)

from sqlalchemy.orm import (
    Session,
    selectinload,
)

from database import (
    Base,
    engine,
    get_db,
)

import models
import schemas

from services import (
    preview_product,
    create_product_from_url,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

INDEX_FILE = (
    BASE_DIR
    / "index.html"
)

ADMIN_FILE = (
    BASE_DIR
    / "admin-price.html"
)


# =========================================================
# DATABASE
# =========================================================

Base.metadata.create_all(
    bind=engine
)


def ensure_database_columns():
    """
    Eski PostgreSQL tablosu varsa gerekli yeni kolonlari
    otomatik ekler. Mevcut urunler silinmez.
    """

    inspector = inspect(
        engine
    )

    try:
        columns = {
            column["name"]
            for column
            in inspector.get_columns(
                "products"
            )
        }

    except Exception as e:
        print(
            "DATABASE INSPECT ERROR:",
            repr(e),
        )

        return

    migrations = []

    if "variant_text" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN variant_text TEXT"
        )

    if "auto_price" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN auto_price DOUBLE PRECISION"
        )

    if "manual_price" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN manual_price DOUBLE PRECISION"
        )

    if not migrations:
        print("DATABASE columns OK")
        return

    try:
        with engine.begin() as connection:
            for sql in migrations:
                print("DATABASE MIGRATION:", sql)
                connection.execute(text(sql))

            # Eski urunlerde otomatik fiyati mevcut fiyattan baslat.
            connection.execute(
                text(
                    "UPDATE products SET auto_price = source_price "
                    "WHERE auto_price IS NULL"
                )
            )

        print("DATABASE migrations DONE")

    except Exception as e:
        print(
            "DATABASE MIGRATION WARNING:",
            repr(e),
        )


ensure_database_columns()


# =========================================================
# APP
# =========================================================

app = FastAPI(
    title="Tüm Sepetim",
    version="6.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
def home():
    return FileResponse(
        INDEX_FILE
    )


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "tum-sepetim",
        "version": "6.1.0",
    }


# =========================================================
# PREVIEW
# =========================================================

@app.post(
    "/api/preview",
    response_model=schemas.PreviewOut,
)
async def preview(
    payload: schemas.ProductCreate,
):
    try:
        p = await preview_product(
            payload.url
        )

        return schemas.PreviewOut(
            title=p.title,

            store=p.store,

            price=p.price,

            image_url=p.image_url,

            url=p.url,

            variant_text=p.variant_text,
        )

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ürün okunamadı: {e}"
            ),
        )


# =========================================================
# ADD
# =========================================================

@app.post(
    "/api/products",
    response_model=schemas.ProductOut,
)
async def add_product(
    payload: schemas.ProductCreate,
    db: Session = Depends(get_db),
):
    try:
        product = (
            await create_product_from_url(
                db,
                payload.url,
                payload.quantity,
            )
        )

        return (
            db.query(models.Product)
            .options(
                selectinload(
                    models.Product.offers
                )
            )
            .filter(
                models.Product.id
                == product.id
            )
            .first()
        )

    except Exception as e:
        db.rollback()

        raise HTTPException(
            status_code=400,
            detail=(
                f"Ürün eklenemedi: {e}"
            ),
        )


# =========================================================
# LIST
# =========================================================

@app.get(
    "/api/products",
    response_model=list[
        schemas.ProductOut
    ],
)
def list_products(
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Product)
        .options(
            selectinload(
                models.Product.offers
            )
        )
        .order_by(
            models.Product
            .created_at
            .desc()
        )
        .all()
    )


# =========================================================
# DELETE
# =========================================================

@app.delete(
    "/api/products/{product_id}"
)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    product = db.get(
        models.Product,
        product_id,
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail="Ürün bulunamadı.",
        )

    db.delete(
        product
    )

    db.commit()

    return {
        "ok": True
    }


# =========================================================
# QUANTITY
# =========================================================

@app.patch(
    "/api/products/{product_id}/quantity"
)
def update_quantity(
    product_id: int,
    quantity: int,
    db: Session = Depends(get_db),
):
    if (
        quantity < 1
        or quantity > 99
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Adet 1-99 arasında olmalı."
            ),
        )

    product = db.get(
        models.Product,
        product_id,
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail="Ürün bulunamadı.",
        )

    product.quantity = (
        quantity
    )

    db.commit()
    db.refresh(
        product
    )

    return {
        "ok": True,
        "quantity": product.quantity,
    }


# =========================================================
# GIZLI MANUEL FIYAT YONETIMI
# =========================================================

def _require_admin_key(key: str | None):
    expected = os.getenv("ADMIN_PRICE_KEY", "").strip()

    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PRICE_KEY ayarlanmamis.",
        )

    supplied = (key or "").strip()

    if not supplied or not hmac.compare_digest(
        supplied,
        expected,
    ):
        raise HTTPException(
            status_code=401,
            detail="Yetkisiz islem.",
        )


@app.get("/fiyat-yonetimi", include_in_schema=False)
def admin_price_page():
    # Ana sayfada bu adrese hicbir link yoktur.
    return FileResponse(
        ADMIN_FILE
    )


@app.post("/api/admin/products/{product_id}/manual-price", include_in_schema=False)
def set_manual_price(
    product_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    _require_admin_key(
        payload.get("admin_key")
    )

    product = db.get(
        models.Product,
        product_id,
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail="Urun bulunamadi.",
        )

    raw_price = payload.get("price")

    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Gecerli bir fiyat gir.",
        )

    if price <= 0 or price > 100_000_000:
        raise HTTPException(
            status_code=400,
            detail="Fiyat gecersiz.",
        )

    # Eski kayitlarda auto_price bos olabilir.
    if product.auto_price is None:
        product.auto_price = product.source_price

    product.manual_price = price
    product.source_price = price

    db.commit()
    db.refresh(product)

    return {
        "ok": True,
        "product_id": product.id,
        "source_price": product.source_price,
        "auto_price": product.auto_price,
        "manual_price": product.manual_price,
    }


@app.delete("/api/admin/products/{product_id}/manual-price", include_in_schema=False)
def clear_manual_price(
    product_id: int,
    admin_key: str,
    db: Session = Depends(get_db),
):
    _require_admin_key(
        admin_key
    )

    product = db.get(
        models.Product,
        product_id,
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail="Urun bulunamadi.",
        )

    product.manual_price = None

    if product.auto_price is not None:
        product.source_price = product.auto_price

    db.commit()
    db.refresh(product)

    return {
        "ok": True,
        "product_id": product.id,
        "source_price": product.source_price,
        "auto_price": product.auto_price,
        "manual_price": None,
    }
