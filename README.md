# Tüm Sepetim — Flat v2

Bu sürüm GitHub web arayüzünden kolay yüklenmesi için klasör kullanmaz.

Repo kökünde olması gereken dosyalar:

- main.py
- database.py
- models.py
- schemas.py
- services.py
- scraper.py
- utils.py
- index.html
- requirements.txt
- render.yaml
- .env.example

## Render

Build Command:

    pip install -r requirements.txt

Start Command:

    uvicorn main:app --host 0.0.0.0 --port $PORT

Environment Variables:

    PYTHON_VERSION=3.13.5
    DATABASE_URL=<PostgreSQL bağlantı adresi>

`DATABASE_URL` girilmezse uygulama SQLite ile açılır fakat Render redeploy sonrası veriler silinebilir.
Kalıcı sepet için PostgreSQL kullanılmalıdır.

## İlk sürüm

Bu sürüm:
- ürün linkini okur
- başlık/resim/fiyat yakalamaya çalışır
- mağazayı tanır
- sepete kaydeder
- adet değiştirir
- siler
- PostgreSQL ile kalıcı veri destekler

Büyük mağazalarda bot koruması/dinamik sayfalar nedeniyle her ürünün fiyatı generic scraper ile alınamayabilir.
Sonraki sürüm mağaza özel adaptörler ve fiyat karşılaştırma motorudur.
