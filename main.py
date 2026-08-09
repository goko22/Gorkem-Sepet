import base64
import hashlib
import hmac
import json
import os
import time
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
from comparison import find_similar_products


BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
ADMIN_FILE = BASE_DIR / "admin-price.html"


# =========================================================
# DATABASE
# =========================================================

Base.metadata.create_all(bind=engine)


def ensure_database_columns():
    """Eski PostgreSQL tablosunu veri kaybetmeden yeni alanlara taşır."""
    inspector = inspect(engine)

    try:
        columns = {column["name"] for column in inspector.get_columns("products")}
    except Exception as e:
        print("DATABASE INSPECT ERROR:", repr(e), flush=True)
        return

    migrations = []

    if "variant_text" not in columns:
        migrations.append("ALTER TABLE products ADD COLUMN variant_text TEXT")
    if "auto_price" not in columns:
        migrations.append("ALTER TABLE products ADD COLUMN auto_price FLOAT")
    if "manual_price" not in columns:
        migrations.append("ALTER TABLE products ADD COLUMN manual_price FLOAT")

    for sql in migrations:
        try:
            with engine.begin() as connection:
                connection.execute(text(sql))
            print("DATABASE MIGRATION OK:", sql, flush=True)
        except Exception as e:
            print("DATABASE MIGRATION WARNING:", repr(e), flush=True)

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

app = FastAPI(
    title="Görkem Sepeti",
    version="7.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/")
def home():
    return FileResponse(INDEX_FILE)


# Eski gizli sayfa uyumluluk için tutuluyor. Yeni admin paneli ana sayfanın içinde.
@app.get("/fiyat-yonetimi", include_in_schema=False)
def admin_price_page():
    return FileResponse(ADMIN_FILE)


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "gorkem-sepeti",
        "version": "7.0.0",
    }


# =========================================================
# PREVIEW
# =========================================================

@app.post("/api/preview", response_model=schemas.PreviewOut)
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
        raise HTTPException(status_code=400, detail=f"Ürün okunamadı: {e}")


# =========================================================
# ADD / LIST / DELETE / QUANTITY
# =========================================================

@app.post("/api/products", response_model=schemas.ProductOut)
async def add_product(
    payload: schemas.ProductCreate,
    db: Session = Depends(get_db),
):
    try:
        product = await create_product_from_url(db, payload.url, payload.quantity)
        return (
            db.query(models.Product)
            .options(selectinload(models.Product.offers))
            .filter(models.Product.id == product.id)
            .first()
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Ürün eklenemedi: {e}")


@app.get("/api/products", response_model=list[schemas.ProductOut])
def list_products(db: Session = Depends(get_db)):
    return (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .order_by(models.Product.created_at.desc())
        .all()
    )


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=400, detail="Adet 1-99 arasında olmalı.")

    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    product.quantity = quantity
    db.commit()
    db.refresh(product)
    return {"ok": True, "quantity": product.quantity}


# =========================================================
# PRICE COMPARISON
# =========================================================

@app.post("/api/products/{product_id}/compare", response_model=schemas.ProductOut)
async def compare_prices(product_id: int, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=400, detail=f"Fiyat karşılaştırması başarısız: {e}")



@app.get("/api/products/{product_id}/similar", include_in_schema=False)
async def similar_products(product_id: int, limit: int = 8, db: Session = Depends(get_db)):
    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    try:
        results = await find_similar_products(
            title=product.title,
            source_url=product.source_url,
            source_price=product.source_price,
            brand=product.brand,
            model=product.model,
            limit=limit,
        )
        return [
            {
                "store": x.store,
                "title": x.title,
                "price": x.price,
                "url": x.url,
                "image_url": x.image_url,
                "score": x.score,
                "reason": x.reason,
            }
            for x in results
        ]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Benzer ürünler bulunamadı: {e}")



# =========================================================
# ADMIN AUTH
# =========================================================


def _admin_credentials():
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    return username, password


def _admin_signing_secret() -> bytes:
    # Ayrı secret verilirse onu kullan; verilmezse ADMIN_PASSWORD ile imzala.
    # Böylece ek env zorunlu değil ama istenirse oturum anahtarı ayrı tutulabilir.
    secret = os.getenv("ADMIN_SESSION_SECRET") or os.getenv("ADMIN_PASSWORD", "")
    return secret.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _create_admin_token(username: str, remember: bool) -> tuple[str, int]:
    ttl = 60 * 60 * 24 * 30 if remember else 60 * 60 * 12
    expires_at = int(time.time()) + ttl
    payload = {"u": username, "exp": expires_at}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_admin_signing_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}", expires_at


def _verify_admin_token(token: str | None):
    username, password = _admin_credentials()
    if not username or not password:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_USERNAME / ADMIN_PASSWORD sunucuda ayarlanmamış.",
        )

    if not token or "." not in token:
        raise HTTPException(status_code=401, detail="Admin oturumu gerekli.")

    try:
        body, supplied_sig = token.split(".", 1)
        expected_sig = hmac.new(
            _admin_signing_secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        supplied_sig_bytes = _b64url_decode(supplied_sig)

        if not hmac.compare_digest(expected_sig, supplied_sig_bytes):
            raise ValueError("bad signature")

        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        if payload.get("u") != username:
            raise ValueError("bad user")
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise HTTPException(status_code=401, detail="Admin oturumunun süresi doldu.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Geçersiz admin oturumu.")


@app.post("/api/admin/login", include_in_schema=False)
def admin_login(payload: schemas.AdminLoginIn):
    expected_username, expected_password = _admin_credentials()

    if not expected_username or not expected_password:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_USERNAME / ADMIN_PASSWORD sunucuda ayarlanmamış.",
        )

    username_ok = hmac.compare_digest(payload.username, expected_username)
    password_ok = hmac.compare_digest(payload.password, expected_password)

    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre yanlış.")

    token, expires_at = _create_admin_token(expected_username, payload.remember)
    return {
        "ok": True,
        "token": token,
        "expires_at": expires_at,
        "remember": payload.remember,
    }


@app.get("/api/admin/session", include_in_schema=False)
def admin_session(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _verify_admin_token(x_admin_token)
    return {"ok": True}



def _sync_effective_price_to_offers(db: Session, product: models.Product):
    """Bir ürünün efektif fiyatını o URL'yi kullanan TÜM karşılaştırma satırlarına yayar."""
    if not product.source_url:
        return
    base = product.source_url.rstrip("/")
    variants = {product.source_url, base, base + "/"}
    (
        db.query(models.Offer)
        .filter(models.Offer.url.in_(variants))
        .update(
            {models.Offer.price: product.source_price},
            synchronize_session=False,
        )
    )

# =========================================================
# EMBEDDED ADMIN ACTIONS
# =========================================================

@app.patch(
    "/api/admin/products/{product_id}/manual-price",
    response_model=schemas.ProductOut,
    include_in_schema=False,
)
def set_manual_price_v2(
    product_id: int,
    payload: schemas.ManualPriceIn,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):
    _verify_admin_token(x_admin_token)

    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    if payload.price is None:
        product.manual_price = None
        product.source_price = product.auto_price
    else:
        product.manual_price = float(payload.price)
        product.source_price = float(payload.price)

    # Sadece bu kartın kendi offer'ı değil, başka ürün kartlarında bu URL'yi
    # referanslayan karşılaştırma satırları da aynı anda güncellensin.
    _sync_effective_price_to_offers(db, product)

    db.commit()
    return (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .filter(models.Product.id == product.id)
        .first()
    )


@app.post(
    "/api/admin/products/{product_id}/refresh",
    response_model=schemas.ProductOut,
    include_in_schema=False,
)
async def admin_refresh_product(
    product_id: int,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):
    _verify_admin_token(x_admin_token)

    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    try:
        scraped = await preview_product(product.source_url)
        product.auto_price = scraped.price
        if product.manual_price is None:
            product.source_price = scraped.price

        product.title = scraped.title or product.title
        product.image_url = scraped.image_url or product.image_url
        product.brand = scraped.brand or product.brand
        product.model = scraped.model or product.model
        product.variant_text = scraped.variant_text or product.variant_text

        # Efektif fiyatı bütün kartlardaki aynı URL tekliflerine yay.
        _sync_effective_price_to_offers(db, product)

        # Bu ürünün kendi kaynak teklifinin başlık/görselini de tazele.
        source_offer = (
            db.query(models.Offer)
            .filter(
                models.Offer.product_id == product.id,
                models.Offer.url.in_({
                    product.source_url,
                    product.source_url.rstrip("/"),
                    product.source_url.rstrip("/") + "/",
                }),
            )
            .first()
        )
        if source_offer:
            source_offer.title = product.title
            source_offer.image_url = product.image_url

        db.commit()
        return (
            db.query(models.Product)
            .options(selectinload(models.Product.offers))
            .filter(models.Product.id == product.id)
            .first()
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Fiyat yenilenemedi: {e}")


@app.delete("/api/admin/products/{product_id}", include_in_schema=False)
def admin_delete_product(
    product_id: int,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):
    _verify_admin_token(x_admin_token)
    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")
    db.delete(product)
    db.commit()
    return {"ok": True}


# =========================================================
# LEGACY ADMIN KEY ENDPOINT (geriye dönük uyumluluk)
# =========================================================


def verify_admin_key(key: str | None):
    expected = os.getenv("ADMIN_PRICE_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_PRICE_KEY sunucuda ayarlanmamış.")
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=404, detail="Not Found")


@app.patch(
    "/api/admin/internal/products/{product_id}/manual-price",
    response_model=schemas.ProductOut,
    include_in_schema=False,
)
def set_manual_price_legacy(
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
        product.manual_price = None
        product.source_price = product.auto_price
    else:
        product.manual_price = float(payload.price)
        product.source_price = float(payload.price)

    # Sadece bu kartın kendi offer'ı değil, başka ürün kartlarında bu URL'yi
    # referanslayan karşılaştırma satırları da aynı anda güncellensin.
    _sync_effective_price_to_offers(db, product)

    db.commit()
    return (
        db.query(models.Product)
        .options(selectinload(models.Product.offers))
        .filter(models.Product.id == product.id)
        .first()
    )
