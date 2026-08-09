# Tüm Sepetim v3 — Docker + Playwright

Bu sürüm klasör kullanmaz. GitHub web arayüzüne bütün dosyaları köke yükleyebilirsin.

## Neler değişti?

- Normal HTTP scraper devam ediyor.
- 403 / eksik fiyat durumunda Playwright + Chromium fallback devreye giriyor.
- Hepsiburada, Amazon Türkiye, Trendyol ve N11 için görünür fiyat selector'ları eklendi.
- Render deploy artık Docker runtime ile yapılmalı.
- PostgreSQL kullanılırsa redeploy sonrası sepet korunur.

## Render ayarı

Yeni Web Service açarken:

Language / Runtime:
Docker

Build Command:
BOŞ

Start Command:
BOŞ

Dockerfile Path:
./Dockerfile

Environment Variables:
DATABASE_URL=<PostgreSQL bağlantı adresin>

Health Check Path:
/health

## Not

Mağazalar bot korumalarını ve HTML yapılarını değiştirebilir. Bu sürüm normal HTTP ve normal headless Chromium ile erişilebilen içeriği okur; proxy/stealth veya koruma aşma mekanizması içermez.
