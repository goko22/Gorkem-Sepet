from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, selectinload

from app.database import Base, engine, get_db
from app import models, schemas
from app.services import preview_product, create_product_from_url

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tüm Sepetim", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/health")
def health():
    return {"ok": True}

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
        return await create_product_from_url(db, payload.url, payload.quantity)
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
