# Görkem Sepeti

Görkem Sepeti, farklı e-ticaret sitelerindeki ürünleri tek bir sepet içinde takip etmeyi, güncel fiyatlarını karşılaştırmayı ve uygun alternatifleri görmeyi amaçlayan bir fiyat karşılaştırma uygulamasıdır.

## Özellikler

- Ürün linki ile sepete ürün ekleme
- Ürün adı, mağaza, fiyat, görsel ve varyant bilgisi gösterimi
- Sepet verilerinin PostgreSQL üzerinde kalıcı tutulması
- Sayfa yenileme ve yeniden deploy sonrası verilerin korunması
- Ürün adedi artırma / azaltma
- Sepet toplamı ve ürün sayısı hesaplama
- Aynı ürünün farklı mağazalardaki fiyatlarını karşılaştırma
- Stokta olmayan karşılaştırma sonuçlarını filtreleme
- Benzer ürünleri ayrı bölümde gösterme
- Kritik varyant kontrolü:
  - ürün boyutu
  - uzunluk
  - renk
  - kapasite
  - radyatör ölçüsü
  - ekran boyutu
- Yanlış ürün eşleşmelerini engelleyen doğrulama katmanı
- Admin paneli
- Manuel fiyat düzenleme
- Ürün fiyatını yeniden kontrol etme
- Admin üzerinden ürün silme
- Mobil uyumlu arayüz
- Dinamik koyu mavi / cam efektli tasarım

## Desteklenen / Hedeflenen Mağazalar

Sistem Türk e-ticaret mağazalarındaki ürünleri bulmaya ve karşılaştırmaya odaklanır.

Örnek mağazalar:

- Amazon Türkiye
- Trendyol
- Hepsiburada
- N11
- PttAVM
- Pazarama
- İdefix
- İtopya
- Vatan Bilgisayar
- Teknosa
- MediaMarkt Türkiye
- İncehesap
- Sinerji
- Gaming.Gen.TR
- Tebilon
- Inventus
- GameGaraj
- Wraith Esports
- Meyer Gaming
- Neeko

Mağaza desteği; sitelerin sayfa yapısı, erişim politikaları ve arama indekslerinde bulunan verilere göre değişebilir.

## Fiyat Karşılaştırma Mantığı

Görkem Sepeti, benzer isimli ürünleri doğrudan aynı ürün kabul etmez.

Karşılaştırmada mümkün olduğunca şu bilgiler dikkate alınır:

- Marka
- Model
- Üretici parça numarası / MPN
- Ürün kodu
- Kapasite
- Renk
- Boyut
- Uzunluk
- Seçili varyant
- Teknik ürün özellikleri

Örneğin:

- `5 metre` ile `10 metre` LED şerit aynı varyant değildir.
- `240mm` ile `360mm` sıvı soğutucu aynı varyant değildir.
- `1TB` ile `2TB` SSD aynı ürün olarak gösterilmez.
- Farklı Ray-Ban model veya renk kodları aynı ürün kabul edilmez.

Amaç, çok fazla sonuç göstermek yerine yalnızca güvenilir eşleşmeleri göstermektir.

> Yalnızca doğrulanmış eşleşmeler gösterilir.

## Benzer Ürünler

Fiyat karşılaştırması ile benzer ürün önerileri birbirinden ayrıdır.

**Fiyatları Karşılaştır** bölümü yalnızca aynı ürün / aynı varyant için sonuç üretmeye çalışır.

**Benzer Ürünleri Gör** bölümü ise aynı ürün kategorisindeki alternatifleri gösterebilir.

Stokta olmadığı açıkça tespit edilen ürünler önerilerden çıkarılır.

## Teknolojiler

### Backend

- Python
- FastAPI
- SQLAlchemy
- PostgreSQL
- Pydantic
- HTTPX
- BeautifulSoup
- Playwright *(yalnızca gerekli ürün okuma işlemlerinde)*

### Frontend

- HTML
- CSS
- Vanilla JavaScript

### Dağıtım

- Render
- Docker
- PostgreSQL

## Proje Yapısı

```text
.
├── main.py
├── services.py
├── scraper.py
├── comparison.py
├── database.py
├── models.py
├── schemas.py
├── utils.py
├── index.html
├── requirements.txt
├── Dockerfile
├── render.yaml
└── env.example
```

Repo yapısı kullanılan sürüme göre küçük farklılıklar gösterebilir.

## Ortam Değişkenleri

Uygulama bazı özellikler için environment variable kullanır.

Örnek:

```env
DATABASE_URL=
PARSE_API_KEY=
TAVILY_API_KEY=

ADMIN_USERNAME=
ADMIN_PASSWORD=
ADMIN_SESSION_SECRET=

OPENAI_API_KEY=
OPENAI_MATCH_MODEL=
```

> Gerçek API anahtarlarını veya şifreleri GitHub reposuna yüklemeyin.

Render üzerinde bunları **Environment Variables** bölümünden tanımlayın.

## Çalıştırma

### Lokal

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Ardından:

```text
http://127.0.0.1:8000
```

### Render

Render üzerinde Web Service oluşturulduktan sonra proje Docker veya mevcut start command üzerinden çalıştırılabilir.

Örnek çalışma komutu:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Sağlık kontrolü:

```text
/health
```

## Veritabanı

Production ortamında PostgreSQL kullanılması önerilir.

Sepetteki ürünlerin yeniden deploy sonrasında kaybolmaması için `DATABASE_URL` kalıcı PostgreSQL veritabanını göstermelidir.

## Admin Paneli

Admin sistemi ana arayüze entegredir.

Admin yetkileri ile:

- Manuel fiyat girilebilir
- Manuel fiyat kaldırılabilir
- Ürün yeniden kontrol edilebilir
- Ürün silinebilir

Admin kullanıcı adı ve şifresi kaynak koda yazılmamalıdır.

```env
ADMIN_USERNAME=
ADMIN_PASSWORD=
```

## Önemli Notlar

E-ticaret siteleri HTML yapılarını, API davranışlarını ve güvenlik sistemlerini değiştirebilir. Bu nedenle bazı mağazalarda ürün veya fiyat okuma yöntemlerinin zaman içinde güncellenmesi gerekebilir.

Görkem Sepeti fiyat doğruluğunu önceliklendirir. Sistem emin olmadığı sonuçları göstermek yerine gizlemeyi tercih eder.

## Durum

Proje aktif geliştirme aşamasındadır.

Şu anki odak:

- ürün eşleştirme doğruluğunu artırmak
- varyant eşleştirmesini geliştirmek
- stok kontrolünü iyileştirmek
- fiyat çıkarma doğruluğunu artırmak
- daha fazla Türk mağazayı güvenilir biçimde desteklemek

---

**Görkem Sepeti**  
Akıllı karşılaştır, en iyi fiyatı yakala.
