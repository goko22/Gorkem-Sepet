from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    normalized_title = Column(String(500), nullable=True, index=True)
    brand = Column(String(200), nullable=True)
    model = Column(String(200), nullable=True)
    image_url = Column(Text, nullable=True)
    source_store = Column(String(100), nullable=False)
    source_url = Column(Text, nullable=False, unique=True)

    # source_price ekranda/sepet toplamında kullanılan efektif fiyat.
    source_price = Column(Float, nullable=True)

    # Scraper'ın son gördüğü otomatik fiyat.
    auto_price = Column(Float, nullable=True)

    # Admin tarafından verilmişse source_price bununla kilitlenir.
    manual_price = Column(Float, nullable=True)

    quantity = Column(Integer, nullable=False, default=1)
    variant_text = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    offers = relationship(
        "Offer",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Offer(Base):
    __tablename__ = "offers"

    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "store",
            "url",
            name="uq_offer_product_store_url",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(
        Integer,
        ForeignKey(
            "products.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    store = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    price = Column(Float, nullable=True)
    url = Column(Text, nullable=False)
    image_url = Column(Text, nullable=True)
    match_score = Column(Float, nullable=True)
    checked_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    product = relationship(
        "Product",
        back_populates="offers",
    )
