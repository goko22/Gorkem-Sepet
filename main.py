import hmac
import os
from pathlib import Path

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Header,
)
from fastapi.responses import FileResponse
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, selectinload

from database import Base, engine, get_db
import models
import schemas
from services import (
    preview_product,
    create_product_from_url,
    compare_and_save_offers,
)


BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
ADMIN_FILE = BASE_DIR / "admin-price.html"


# =========================================================
# DATABASE
# =========================================================

Base.metadata.create_all(bind=engine)


def ensure_database_columns():
    """
    Eski PostgreSQL tablosunu veri kaybetmeden yeni alanlara taşır.
    """

    inspector = inspect(engine)

    try:
        columns = {
            column["name"]
            for column in inspector.get_columns("products")
        }
    except Exception as e:
        print("DATABASE INSPECT ERROR:", repr(e), flush=True)
        return

    migrations = []

    if "variant_text" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN variant_text TEXT"
        )

    if "auto_price" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN auto_price FLOAT"
        )

    if "manual_price" not in columns:
        migrations.append(
            "ALTER TABLE products ADD COLUMN manual_price FLOAT"
        )

    for sql in migrations:
        try:
            with engine.begin() as connection:
                connection.execute(text(sql))
            print("DATABASE MIGRATION OK:", sql, flush=True)
        except Exception as e:
            print("DATABASE MIGRATION WARNING:", repr(e), flush=True)

    # Eski ürünlerde auto_price boşsa mevcut source_price ile doldur.
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE products
                    SET auto_price = source_price
                    WHERE auto_price IS NULL
                    """
                )
            )
    except Exception as e:
        print("DATABASE AUTO PRICE BACKFILL WARNING:", repr(e), flush=True)


ensure_database_columns()


# =========================================================
# APP
# =========================================================

# Admin endpointlerini basit /docs menüsünde göstermemek için docs kapalı.
app = FastAPI(
    title="Tüm Sepetim",
    version="6.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/")
def home():
    return FileResponse(INDEX_FILE)


@app.get("/fiyat-yonetimi", include_in_schema=False)
def admin_price_page():
    return FileResponse(ADMIN_FILE)


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "tum-sepetim",
        "version": "6.2.0",
    }


# =========================================================
# PREVIEW
# =========================================================

@app.post(
    "/api/preview",
    response_model=schemas.PreviewOut,
)
async def preview(payload: schemas.ProductCreate):
    try:
        p = await preview_product(payload.url)

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
            detail=f"Ürün okunamadı: {e}",
        )


# =========================================================
# ADD / LIST / DELETE / QUANTITY
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
        product = await create_product_from_url(
            db,
            payload.url,
            payload.quantity,
        )

        return (
            db.query(models.Product)
            .options(selectinload(models.Product.offers))
            .filter(models.Product.id == product.id)
            .first()
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Ürün eklenemedi: {e}",
        )


@app.get(
    "/api/products",
    response_model=list[schemas.ProductOut],
)
def list_products(db: Session = Depends(get_db)):
    return (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .order_by(models.Product.created_at.desc())
        .all()
    )


@app.delete("/api/products/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    product = db.get(models.Product, product_id)

    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    db.delete(product)
    db.commit()
    return {"ok": True}


@app.patch("/api/products/{product_id}/quantity")
def update_quantity(
    product_id: int,
    quantity: int,
    db: Session = Depends(get_db),
):
    if quantity < 1 or quantity > 99:
        raise HTTPException(
            status_code=400,
            detail="Adet 1-99 arasında olmalı.",
        )

    product = db.get(models.Product, product_id)

    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    product.quantity = quantity
    db.commit()
    db.refresh(product)

    return {
        "ok": True,
        "quantity": product.quantity,
    }


# =========================================================
# PRICE COMPARISON
# =========================================================

@app.post(
    "/api/products/{product_id}/compare",
    response_model=schemas.ProductOut,
)
async def compare_prices(
    product_id: int,
    db: Session = Depends(get_db),
):
    product = (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .filter(models.Product.id == product_id)
        .first()
    )

    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    try:
        await compare_and_save_offers(db, product)

        return (
            db.query(models.Product)
            .options(selectinload(models.Product.offers))
            .filter(models.Product.id == product_id)
            .first()
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Fiyat karşılaştırması başarısız: {e}",
        )


# =========================================================
# HIDDEN ADMIN MANUAL PRICE
# =========================================================


def verify_admin_key(key: str | None):
    expected = os.getenv("ADMIN_PRICE_KEY", "")

    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PRICE_KEY sunucuda ayarlanmamış.",
        )

    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(
            status_code=404,
            detail="Not Found",
        )


@app.patch(
    "/api/admin/internal/products/{product_id}/manual-price",
    response_model=schemas.ProductOut,
    include_in_schema=False,
)
def set_manual_price(
    product_id: int,
    payload: schemas.ManualPriceIn,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    verify_admin_key(x_admin_key)

    product = db.get(models.Product, product_id)

    if not product:
        raise HTTPException(status_code=404, detail="Not Found")

    if payload.price is None:
        # Manuel kilidi kaldır; son otomatik fiyata dön.
        product.manual_price = None
        product.source_price = product.auto_price
    else:
        product.manual_price = float(payload.price)
        product.source_price = float(payload.price)

    # Kaynak offer da sepet fiyatıyla aynı kalsın.
    source_offer = (
        db.query(models.Offer)
        .filter(
            models.Offer.product_id == product.id,
            models.Offer.url == product.source_url,
        )
        .first()
    )

    if source_offer:
        source_offer.price = product.source_price

    db.commit()

    return (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .filter(models.Product.id == product.id)
        .first()
    )
