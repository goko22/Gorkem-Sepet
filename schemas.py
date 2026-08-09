from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    url: str

    quantity: int = Field(
        default=1,
        ge=1,
        le=99,
    )


class OfferOut(BaseModel):
    id: int
    store: str
    title: str
    price: Optional[float] = None
    url: str
    image_url: Optional[str] = None
    match_score: Optional[float] = None

    model_config = {
        "from_attributes": True
    }


class ProductOut(BaseModel):
    id: int
    title: str

    brand: Optional[str] = None
    model: Optional[str] = None
    image_url: Optional[str] = None

    source_store: str
    source_url: str
    source_price: Optional[float] = None

    quantity: int

    # Yeni
    variant_text: Optional[str] = None

    created_at: datetime

    offers: List[OfferOut] = Field(
        default_factory=list
    )

    model_config = {
        "from_attributes": True
    }


class PreviewOut(BaseModel):
    title: str
    store: str
    price: Optional[float] = None
    image_url: Optional[str] = None
    url: str
    variant_text: Optional[str] = None
