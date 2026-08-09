# Tüm Sepetim v1

Farklı e-ticaret sitelerindeki ürünleri tek sepette toplamak ve fiyat karşılaştırma altyapısı kurmak için başlangıç projesi.

## İlk sürümde çalışanlar

- FastAPI backend
- PostgreSQL / Render persistent DB
- Ürün linkinden genel ürün bilgisi çekme
- JSON-LD / OpenGraph fallback
- Sepete ürün ekleme
- Kalıcı ürün listeleme
- Adet değiştirme
- Ürün silme
- Mağaza tespiti
- Modüler scraper registry
- Yatay ürün kartları

## Desteklenen domain tanıma

- Trendyol
- Hepsiburada
- Amazon Türkiye
- N11
- idefix
- Wraith
- Meyer
- İtopya
- Vatan
- MediaMarkt
- Teknosa

Not: Domain tanınması ile kusursuz scraper aynı şey değildir. Büyük mağazalar dinamik HTML / bot koruması kullandığı için sonraki sürümde mağazaya özel adaptörler eklenecek.

## Local çalıştırma

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn main:app --reload
```

Sonra:
http://127.0.0.1:8000

## Render deploy

En kolay yol:

1. Bu projeyi GitHub'a yükle.
2. Render'da `New +` > `Blueprint` seç.
3. Repoyu bağla.
4. `render.yaml` otomatik olarak Web Service + PostgreSQL oluşturur.
5. Deploy et.

`DATABASE_URL` Render tarafından otomatik bağlanır.

## Mimari

app/
- database.py
- models.py
- schemas.py
- services.py
- utils.py
- scrapers/
  - base.py
  - generic.py
  - registry.py

Bir mağazaya özel scraper eklemek için:

```python
class TrendyolScraper(BaseScraper):
    store_name = "Trendyol"
    ...
```

sonra `registry.py`:

```python
SCRAPER_REGISTRY = {
    "Trendyol": TrendyolScraper,
}
```

## Sonraki aşama

1. Trendyol özel scraper
2. Hepsiburada özel scraper
3. Amazon Türkiye özel scraper
4. N11 / idefix
5. Wraith / Meyer
6. Aynı ürünü diğer mağazalarda arama
7. Match score
8. En ucuz teklif
9. Periyodik fiyat güncelleme
10. Fiyat geçmişi
