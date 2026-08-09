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

## v6 - Fiyat karşılaştırma + korumalı manuel fiyat

- Ürün kartındaki **Fiyatları karşılaştır** düğmesi mağaza aramalarını tarar.
- Eşleştirme önce model/teknik değer/varyant çelişkilerini sert şekilde eler.
- `OPENAI_API_KEY` verilirse yalnız orta-yüksek güvenli adaylarda ikinci AI doğrulaması kullanılır. AI tek başına ürün kabul edemez.
- Manuel fiyat için ana ekranda menü yoktur. Render Environment'a `ADMIN_PRICE_KEY` eklenmelidir.
- Manuel fiyat endpointi: `PATCH /api/admin/internal/products/{id}/manual-price`
- Header: `X-Admin-Key: <ADMIN_PRICE_KEY>`
- Body örneği: `{"price": 6048}`
- Manuel kilidi kaldırmak için body: `{"price": null}`
