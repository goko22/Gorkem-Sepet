from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, selectinload

from database import Base, engine, get_db
import models
import schemas
from services import preview_product, create_product_from_url

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tüm Sepetim", version="3.0.0")

@app.get("/")
def home():
    return FileResponse(INDEX_FILE)

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "tum-sepetim",
        "version": "3.0.0",
    }

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
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ürün okunamadı: {e}")

@app.post("/api/products", response_model=schemas.ProductOut)
async def add_product(payload: schemas.ProductCreate, db: Session = Depends(get_db)):
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
def update_quantity(product_id: int, quantity: int, db: Session = Depends(get_db)):
    if quantity < 1 or quantity > 99:
        raise HTTPException(status_code=400, detail="Adet 1-99 arasında olmalı.")

    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı.")

    product.quantity = quantity
    db.commit()
    return {"ok": True}
