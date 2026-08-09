from pathlib import Path

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


# =========================================================
# DATABASE
# =========================================================

Base.metadata.create_all(
    bind=engine
)


def ensure_database_columns():
    """
    Eski PostgreSQL tablosu varsa
    variant_text kolonunu otomatik ekler.

    Mevcut ürünler silinmez.
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

    if "variant_text" in columns:
        print(
            "DATABASE variant_text OK"
        )

        return

    print(
        "DATABASE adding variant_text..."
    )

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    ALTER TABLE products
                    ADD COLUMN variant_text TEXT
                    """
                )
            )

        print(
            "DATABASE variant_text ADDED"
        )

    except Exception as e:
        # Aynı anda başka instance eklemiş
        # olabilir. Servisi gereksiz yere
        # düşürmeyelim.
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
    version="5.0.0",
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
        "version": "5.0.0",
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
