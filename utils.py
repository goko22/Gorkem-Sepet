import re
from urllib.parse import urlparse

STORE_DOMAINS = {
    "trendyol.com": "Trendyol",
    "hepsiburada.com": "Hepsiburada",
    "amazon.com.tr": "Amazon Türkiye",
    "n11.com": "N11",
    "idefix.com": "idefix",
    "wraithesports.com": "Wraith",
    "wraith.com.tr": "Wraith",
    "meyergaming.com": "Meyer",
    "meyergaming.com.tr": "Meyer",
    "itopya.com": "İtopya",
    "vatanbilgisayar.com": "Vatan",
    "mediamarkt.com.tr": "MediaMarkt",
    "teknosa.com": "Teknosa",
}

def detect_store(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for domain, name in STORE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "Bilinmeyen"

def normalize_title(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9çğıöşü\\s-]", " ", s)
    s = re.sub(r"\\s+", " ", s).strip()
    return s

def parse_price(text: str):
    if text is None:
        return None

    raw = str(text).strip()
    if not raw:
        return None

    # Prefer a number that looks like a Turkish price.
    matches = re.findall(r"\\d{1,3}(?:\\.\\d{3})*(?:,\\d{1,2})?|\\d+(?:[.,]\\d{1,2})?", raw)
    if not matches:
        return None

    for cleaned in matches:
        try:
            if "," in cleaned and "." in cleaned:
                cleaned = cleaned.replace(".", "").replace(",", ".")
            elif "," in cleaned:
                cleaned = cleaned.replace(".", "").replace(",", ".")
            elif cleaned.count(".") > 1:
                cleaned = cleaned.replace(".", "")
            elif "." in cleaned:
                left, right = cleaned.rsplit(".", 1)
                if len(right) == 3 and len(left) >= 1:
                    cleaned = left + right

            value = float(cleaned)
            if value > 0:
                return value
        except Exception:
            pass
    return None
