# Tüm Sepetim v4 — Network Price Capture

v3'e ek olarak:

- Playwright sayfasının XHR/fetch/network response'larını dinler.
- JSON cevaplarında ürüne bağlı price/currentPrice/sellingPrice/salePrice vb. alanları arar.
- Hepsiburada ürün kodu (HBCV...) ile response eşleştirerek önerilen/başka ürün fiyatlarını almamaya çalışır.
- DOM + JSON-LD + embedded script + network JSON şeklinde dört aşamalı fiyat çıkarımı yapar.
- Amazon için DOM/JSON fallback devam eder.

## GitHub

ZIP'i çıkar ve tüm dosyaları repo köküne yükle.

## Render

Runtime: Docker

Dockerfile Path:
./Dockerfile

Build Command:
boş

Start Command:
boş

Environment:
DATABASE_URL=<PostgreSQL internal URL>

Health Check:
/health

Deploy sonrası `/health` sürümü `4.0.0` göstermeli.
