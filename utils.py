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
    s = re.sub(r"[^a-z0-9çğıöşü\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_price(text: str):
    if not text:
        return None

    cleaned = re.sub(r"[^0-9,.]", "", str(text))
    if not cleaned:
        return None

    try:
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            left, right = cleaned.rsplit(",", 1)
            if len(right) in (1, 2):
                cleaned = left.replace(".", "") + "." + right
            else:
                cleaned = cleaned.replace(",", "")
        elif cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "")
        elif "." in cleaned:
            left, right = cleaned.rsplit(".", 1)
            if len(right) == 3 and len(left) >= 1:
                cleaned = left + right

        value = float(cleaned)
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None
