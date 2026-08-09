import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx


# =========================================================
# GÖRKEM SEPETİ - COMPARISON V21 FAMILY-GUARDED
#
# Amaç:
# - Playwright / scraper YOK
# - 403 / Cloudflare YOK
# - 512 MB RAM patlaması YOK
# - Tavily sadece web indeksinden Türk mağaza ürünlerini bulur
# - Fiyat yalnızca TL/TRY/₺ olarak indeks içeriğinde görünüyorsa kabul edilir
# - Güçlü ürün kimliği yoksa şüpheli sonucu göstermez
#
# services.py ile uyumlu public API:
#   MatchResult
#   compare_product(...)
#   same_product_pair_score(...)
# =========================================================

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 22.0
MIN_ACCEPT_SCORE = 0.84
MAX_FINAL_OFFERS = 12

TR_MAP = str.maketrans({
    "ı": "i", "ğ": "g", "ü": "u", "ş": "s", "ö": "o", "ç": "c",
})

STOP_WORDS = {
    "ve", "ile", "icin", "the", "a", "an", "of", "urun", "urunu",
    "fiyat", "fiyatlari", "fiyati", "satinal", "satin", "al",
    "gaming", "oyuncu", "kablosuz", "turkiye",
    "amazon", "com", "tr", "moda",
}

ACCESSORY_PHRASES = {
    "ekran koruyucu", "screen protector", "koruyucu cam", "koruyucu film",
    "kilif", "case", "tasima cantasi", "stand", "bracket", "duvar aparati",
    "yedek parca", "replacement", "cover",
}

KNOWN_TURKISH_SHOPS = {
    "trendyol.com": "Trendyol",
    "hepsiburada.com": "Hepsiburada",
    "amazon.com.tr": "Amazon Türkiye",
    "n11.com": "N11",
    "itopya.com": "İtopya",
    "vatanbilgisayar.com": "Vatan",
    "teknosa.com": "Teknosa",
    "mediamarkt.com.tr": "MediaMarkt",
    "idefix.com": "idefix",
    "sinerji.gen.tr": "Sinerji",
    "incehesap.com": "İncehesap",
    "gaming.gen.tr": "Gaming.Gen.TR",
    "tebilon.com": "Tebilon",
    "inventus.com.tr": "Inventus",
    "gamegaraj.com": "GameGaraj",
    "wraithesports.com": "Wraith Esports",
    "meyergaming.com": "Meyer Gaming",
    "neeko.com.tr": "Neeko",
    "pazarama.com": "Pazarama",
    "pttavm.com": "PttAVM",
    "gencergaming.com": "Gençer Gaming",
    "qp.com.tr": "QP Bilişim",
}

# Bunlar keşif için yararlı olabilir ama final mağaza teklifi değildir.
AGGREGATORS = {
    "akakce.com",
    "cimri.com",
    "epey.com",
    "ucuzacebin.de",
}

TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "wt_pc", "adj_t", "adj_campaign", "mwebtoapp",
}


@dataclass
class MatchResult:
    store: str
    title: str
    price: Optional[float]
    url: str
    image_url: Optional[str]
    score: float
    reason: str


# =========================================================
# NORMALIZATION
# =========================================================

def norm(text: str) -> str:
    s = str(text or "").lower().translate(TR_MAP)
    s = s.replace("×", "x")
    s = re.sub(r"[^a-z0-9.+x/\-\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


def words(text: str) -> set[str]:
    return {
        w for w in norm(text).split()
        if len(w) >= 2 and w not in STOP_WORDS
    }


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return ""


def canonical_url(url: str) -> str:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().replace("www.", "")
        q = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in TRACKING_QUERY_KEYS
        ]
        path = re.sub(r"/+$", "", p.path or "/")
        return urlunparse(("https", host, path, "", urlencode(q), ""))
    except Exception:
        return str(url or "").rstrip("/")


def shop_name(url: str) -> str:
    host = host_of(url)
    for domain, name in KNOWN_TURKISH_SHOPS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host.split(".")[0].replace("-", " ").title() if host else "X Mağaza"


def is_turkish_shop(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False

    for domain in KNOWN_TURKISH_SHOPS:
        if host == domain or host.endswith("." + domain):
            return True

    # Bilinmeyen ama açıkça Türkiye alan adı kullanan mağazalar da kabul edilebilir.
    if host.endswith((".com.tr", ".net.tr", ".gen.tr")):
        return True

    return False


def is_aggregator(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in AGGREGATORS)


def probable_product_page(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    query = (urlparse(url).query or "").lower()

    if not path or path == "/":
        return False

    blocked = (
        "/search", "/arama", "/kategori", "/category", "/brand", "/marka",
        "/magaza", "/seller", "/blog", "/haber", "/forum", "/cart", "/sepet",
    )
    if any(x in path for x in blocked):
        return False

    if re.search(r"/[^/]+-x-b\d+", path):
        return False

    if "pi=" in query and "-p-" not in path:
        return False

    return True


# =========================================================
# IDENTITY
# =========================================================

def model_tokens(text: str) -> set[str]:
    """
    Extract real manufacturer/model-like identifiers.

    IMPORTANT:
    Values such as 50x50cm, 360mm, 24inch, 310hz, 1ms, 32gb are SPECS,
    not model numbers. Older versions treated some of these as strong IDs and
    then rejected perfectly valid search results because that "model" was absent.
    """
    n = norm(text)
    out: set[str] = set()

    spec_units = {
        "mm", "cm", "m", "inch", "inc", "hz", "khz", "mhz", "ghz",
        "ms", "dpi", "w", "kw", "v", "mah", "gb", "tb", "mb",
    }

    for t in re.findall(r"\b[a-z0-9][a-z0-9._+-]{3,}\b", n):
        if not (re.search(r"[a-z]", t) and re.search(r"\d", t)):
            continue

        # dimensions: 50x50cm, 120x60, 24x36...
        if re.fullmatch(r"\d+(?:[.,]\d+)?x\d+(?:[.,]\d+)?(?:mm|cm|m|inch|inc)?", t):
            continue

        # simple spec values: 360mm, 310hz, 1ms, 32000dpi, 32gb, 1500ml...
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([a-z]+)", t)
        if m and m.group(2) in (spec_units | {"ml", "lt", "cl"}):
            continue

        # Common marketing/spec tokens that are not identity codes.
        if t in {"3d", "4k", "8k", "2k", "5g", "2.4g", "2.4ghz"}:
            continue

        out.add(t)

    # Manufacturer part numbers such as 910-006631.
    for t in re.findall(r"\b\d{2,6}[-/]\d{3,10}\b", n):
        out.add(t)

    return out


def usable_model(model: Optional[str], title: str = "") -> Optional[str]:
    m = norm(model or "").strip()
    if not m:
        return None

    c = compact(m)

    # Shopify / storefront variant IDs: long all-digit IDs are not product models.
    if c.isdigit() and len(c) >= 9:
        return None

    if c.startswith(("hbcv", "hbc")):
        return None

    if len(c) < 4 or len(c) > 40:
        return None

    # Prefer a model that also appears in title, unless it has a manufacturer-like separator.
    if c not in compact(title) and not re.search(r"[-/]", m):
        if not (re.search(r"[a-z]", m) and re.search(r"\d", m)):
            return None

    return m


def strong_ids(title: str, model: Optional[str] = None) -> list[str]:
    out = set(model_tokens(title))
    m = usable_model(model, title)
    if m:
        out.add(m)

    def rank(x: str):
        return (
            bool(re.search(r"[a-z]", x) and re.search(r"\d", x)),
            bool(re.search(r"[-/]", x)),
            len(x),
        )

    return sorted(out, key=rank, reverse=True)


def capacities(text: str) -> set[str]:
    n = norm(text)
    return {
        f"{value.replace(',', '.')}{unit}"
        for value, unit in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*(tb|gb|mb)\b", n)
    }


def is_accessory(text: str) -> bool:
    n = norm(text)
    return any(x in n for x in ACCESSORY_PHRASES)


def rayban_identity(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    n = norm(text)
    mm = re.search(r"\b0?(rb\d{4}[a-z]?)\b", n)
    if not mm:
        return None, None, None

    model = mm.group(1)
    after = n[mm.end():].strip()

    cm = re.search(r"\b(\d{3,4}(?:/[a-z0-9]{1,3})?)\b", after)
    color = cm.group(1) if cm else None

    size = None
    if cm:
        sm = re.search(r"\b(\d{2})\b", after[cm.end():])
        size = sm.group(1) if sm else None

    return model, color, size


def hard_conflict(source_title: str, candidate_title: str) -> tuple[bool, str]:
    if is_accessory(candidate_title) and not is_accessory(source_title):
        return True, "aksesuar / ana ürün farklı"

    # Capacity is a hard variant.
    a = capacities(source_title)
    b = capacities(candidate_title)
    if a and b and a.isdisjoint(b):
        return True, "kapasite farklı"

    # Ray-Ban: same exact frame model + color code. Size is allowed to differ.
    sm, sc, _ss = rayban_identity(source_title)
    cm, cc, _cs = rayban_identity(candidate_title)
    if sm:
        if not cm:
            return True, "gözlük modeli bulunamadı"
        if sm != cm:
            return True, "gözlük modeli farklı"
        if sc and cc and sc != cc:
            return True, "gözlük kodu farklı"

    # Superlight 2 vs Superlight-style generation guard.
    s = norm(source_title)
    c = norm(candidate_title)
    if "superlight 2" in s and "superlight 2" not in c:
        return True, "ürün nesli farklı"

    return False, ""


def identity_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str] = None,
    source_model: Optional[str] = None,
) -> tuple[float, str]:
    conflict, why = hard_conflict(source_title, candidate_title)
    if conflict:
        return 0.0, why

    source_ids = strong_ids(source_title, source_model)
    candidate_blob = compact(candidate_title)

    if source_ids:
        matched = [x for x in source_ids if compact(x) and compact(x) in candidate_blob]
        if not matched:
            return 0.0, f"model kodu adayda yok: {source_ids[0]}"

        # Exact manufacturer/model code is strong enough when no hard conflict exists.
        score = 0.94
        if source_brand and compact(source_brand) in candidate_blob:
            score += 0.03

        # Some model tokens can be noisy; require at least a little title overlap too.
        sw = words(source_title)
        cw = words(candidate_title)
        shared = sw & cw
        if len(shared) >= 3:
            score += 0.02

        return min(score, 0.99), f"model eşleşti: {matched[0]}"

    sw = words(source_title)
    cw = words(candidate_title)
    if not sw or not cw:
        return 0.0, "başlık yetersiz"

    shared = sw & cw
    precision = len(shared) / max(1, len(cw))
    recall = len(shared) / max(1, len(sw))
    f1 = 2 * precision * recall / max(0.0001, precision + recall)

    score = f1
    if source_brand and compact(source_brand) in candidate_blob:
        score += 0.08

    return min(score, 0.93), f"başlık eşleşmesi {score:.2f}"


def same_product_pair_score(
    left_title: str,
    right_title: str,
    left_brand: Optional[str] = None,
    right_brand: Optional[str] = None,
    left_model: Optional[str] = None,
    right_model: Optional[str] = None,
    left_variant_text: Optional[str] = None,
    right_variant_text: Optional[str] = None,
) -> tuple[float, str]:
    if left_brand and right_brand:
        lb, rb = compact(left_brand), compact(right_brand)
        if lb and rb and lb != rb:
            # Brand values from scraper can occasionally be garbage/site names.
            # Only hard reject when both brands are short normal brand-like values.
            if len(lb) <= 30 and len(rb) <= 30:
                return 0.0, "marka farklı"

    bad, why = hard_conflict(left_title, right_title)
    if bad:
        return 0.0, why
    bad, why = hard_conflict(right_title, left_title)
    if bad:
        return 0.0, why

    s1, r1 = identity_score(left_title, right_title, left_brand, left_model)
    s2, r2 = identity_score(right_title, left_title, right_brand, right_model)

    # Direction-independent and conservative.
    score = min(s1, s2)
    return score, r1 if s1 <= s2 else r2


# =========================================================
# CRITICAL VARIANTS
# =========================================================

_COLOR_WORDS = {
    "siyah": "black", "black": "black",
    "beyaz": "white", "white": "white",
    "gri": "gray", "gray": "gray", "grey": "gray",
    "kirmizi": "red", "red": "red",
    "mavi": "blue", "blue": "blue",
    "yesil": "green", "green": "green",
    "pembe": "pink", "pink": "pink",
}


def _normalized_variant_blob(title: str, variant_text: Optional[str] = None) -> str:
    return norm(f"{title or ''} {variant_text or ''}")


def length_variants(title: str, variant_text: Optional[str] = None) -> set[int]:
    """
    Physical selectable lengths, normalized to centimeters.
    5m -> 500, 10 metre -> 1000, 50cm -> 50.

    We intentionally do NOT treat 360mm as a generic length here because for AIOs
    it is handled by radiator_variants().
    """
    n = _normalized_variant_blob(title, variant_text)
    out: set[int] = set()

    for m in re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*(?:metre|meter|metreler|m)\b", n):
        try:
            val = float(m.group(1).replace(",", "."))
        except Exception:
            continue
        if 0.3 <= val <= 100:
            out.add(int(round(val * 100)))

    for m in re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*cm\b", n):
        try:
            val = float(m.group(1).replace(",", "."))
        except Exception:
            continue
        if 1 <= val <= 10000:
            out.add(int(round(val)))

    return out


def radiator_variants(title: str, variant_text: Optional[str] = None) -> set[int]:
    n = _normalized_variant_blob(title, variant_text)
    out = set()
    for m in re.finditer(r"\b(120|140|240|280|360|420)\s*mm\b", n):
        out.add(int(m.group(1)))
    return out


def display_inches(title: str, variant_text: Optional[str] = None) -> set[float]:
    n = _normalized_variant_blob(title, variant_text)
    out: set[float] = set()
    patterns = [
        r"\b(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:inch|inc)\b",
        r"\b(\d{1,2}(?:[.,]\d{1,2})?)\s*[\"″]\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, n):
            try:
                v = float(m.group(1).replace(",", "."))
            except Exception:
                continue
            if 1.0 <= v <= 100:
                out.add(round(v, 2))
    return out


def color_variants(title: str, variant_text: Optional[str] = None) -> set[str]:
    n = _normalized_variant_blob(title, variant_text)
    out = set()
    for word, canonical in _COLOR_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", n):
            out.add(canonical)
    return out


def critical_variant_conflict(
    source_title: str,
    source_variant_text: Optional[str],
    candidate_text: str,
) -> tuple[bool, str]:
    """
    Compare only variants that are explicitly known on BOTH sides.
    This prevents 5m -> 10m, 240mm -> 360mm, black -> white, etc.
    Unknown candidate variant is not automatically rejected.
    """
    sl = length_variants(source_title, source_variant_text)
    cl = length_variants(candidate_text, None)
    if sl and cl and sl.isdisjoint(cl):
        return True, f"uzunluk varyanti farkli: kaynak={sorted(sl)}cm aday={sorted(cl)}cm"

    sr = radiator_variants(source_title, source_variant_text)
    cr = radiator_variants(candidate_text, None)
    if sr and cr and sr.isdisjoint(cr):
        return True, f"radyator varyanti farkli: kaynak={sorted(sr)}mm aday={sorted(cr)}mm"

    sc = color_variants(source_title, source_variant_text)
    cc = color_variants(candidate_text, None)
    if sc and cc and sc.isdisjoint(cc):
        return True, f"renk varyanti farkli: kaynak={sorted(sc)} aday={sorted(cc)}"

    # Display size can identify revisions/listings for products where it is explicit.
    si = display_inches(source_title, source_variant_text)
    ci = display_inches(candidate_text, None)
    if si and ci:
        # allow tiny formatting differences, reject materially different panels
        if all(abs(a - b) > 0.10 for a in si for b in ci):
            return True, f"ekran boyutu farkli: kaynak={sorted(si)} aday={sorted(ci)}"

    return False, ""


def variant_query_suffix(title: str, variant_text: Optional[str]) -> str:
    parts = []

    lengths = sorted(length_variants(title, variant_text))
    # meters if divisible by 100, else cm
    for cm in lengths[:1]:
        if cm % 100 == 0:
            parts.append(f"{cm // 100} metre")
        else:
            parts.append(f"{cm} cm")

    radiators = sorted(radiator_variants(title, variant_text))
    if radiators:
        parts.append(f"{radiators[0]}mm")

    colors = sorted(color_variants(title, variant_text))
    tr_color = {"black": "siyah", "white": "beyaz", "gray": "gri",
                "red": "kırmızı", "blue": "mavi", "green": "yeşil", "pink": "pembe"}
    if colors:
        parts.append(tr_color.get(colors[0], colors[0]))

    inches = sorted(display_inches(title, variant_text))
    if inches:
        parts.append(f"{inches[0]:g} inch")

    return " ".join(parts)


def amazon_asin(url: str) -> Optional[str]:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(url or ""), re.I)
    return m.group(1).upper() if m else None


# =========================================================
# PRICE EXTRACTION
# =========================================================

def parse_tl_number(raw: str) -> Optional[float]:
    raw = re.sub(r"\s+", "", str(raw or ""))
    if not raw:
        return None

    try:
        if "," in raw:
            # 6.212,22
            normalized = raw.replace(".", "").replace(",", ".")
        else:
            # "6.212" in Turkish price context is normally 6212.
            chunks = raw.split(".")
            if len(chunks) > 1 and all(len(x) == 3 for x in chunks[1:]):
                normalized = "".join(chunks)
            else:
                normalized = raw

        value = float(normalized)
    except Exception:
        return None

    if 10 <= value <= 5_000_000:
        return value
    return None


def extract_tl_prices(text: str) -> list[float]:
    text = str(text or "")
    pats = [
        r"₺\s*([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)",
        r"([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)\s*(?:TL|TRY)\b",
        r"(?:TL|TRY)\s*([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)",
    ]

    out: list[float] = []
    for pat in pats:
        for m in re.finditer(pat, text, flags=re.I):
            v = parse_tl_number(m.group(1))
            if v is not None:
                out.append(v)

    # Preserve order, dedupe.
    seen = set()
    clean = []
    for x in out:
        key = round(x, 2)
        if key not in seen:
            seen.add(key)
            clean.append(x)
    return clean


def _price_occurrences(text: str) -> list[tuple[float, int, str]]:
    """Return (price, char_position, nearby_text) for explicit TL/TRY/₺ prices."""
    text = str(text or "")
    patterns = [
        r"₺\s*([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)",
        r"([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)\s*(?:TL|TRY)\b",
        r"(?:TL|TRY)\s*([0-9][0-9.\s]{0,14}(?:,[0-9]{1,2})?)",
    ]

    out = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            value = parse_tl_number(m.group(1))
            if value is None:
                continue
            lo = max(0, m.start() - 90)
            hi = min(len(text), m.end() + 90)
            out.append((value, m.start(), text[lo:hi]))

    return out


def _looks_like_installment_context(context: str, price: float) -> bool:
    n = norm(context)
    # Monthly installment values frequently appear as "6x 1.061,50 TL",
    # "taksit", "aylık" etc. They must never become the product price.
    if re.search(r"\b\d{1,2}\s*x\s*[0-9]", n):
        return True
    if any(x in n for x in ("taksit", "aylik", "aylik odeme", "kredi")):
        # Do not reject the whole page; only the price occurrence near these cues.
        return True
    return False


def choose_price(
    text: str,
    source_price: Optional[float],
    source_title: str = "",
    source_model: Optional[str] = None,
) -> Optional[float]:
    """
    Pick a TL price from the product's own local text, not a random number from
    a marketplace/category page.

    Key protections:
    - Prefer prices close to exact MPN/model occurrence.
    - Reject installment/monthly-payment contexts.
    - If source_price exists, NEVER fall back to an implausibly tiny number.
    """
    occurrences = _price_occurrences(text)
    if not occurrences:
        return None

    ids = strong_ids(source_title, source_model)
    normalized_text = compact(text)

    # Locate strong product identifiers in the full indexed content.
    id_positions = []
    for ident in ids:
        needle = compact(ident)
        if not needle:
            continue
        # compact() loses original positions, so search relaxed textual variants too.
        variants = {
            norm(ident),
            norm(ident).replace("-", " "),
            norm(ident).replace("/", " "),
        }
        for v in variants:
            if not v:
                continue
            for m in re.finditer(re.escape(v), norm(text), flags=re.I):
                id_positions.append(m.start())

    candidates = []
    for price, pos, ctx in occurrences:
        if _looks_like_installment_context(ctx, price):
            continue

        # Hard plausibility gate relative to the source product.
        if source_price and source_price > 0:
            ratio = price / float(source_price)
            # Wide enough for real discounts/store differences, narrow enough to
            # kill 645 / 975 / 1308 TL installment-like junk for a 6k product.
            if ratio < 0.35 or ratio > 3.0:
                continue
        else:
            ratio = 1.0

        score = 0.0
        ctx_n = norm(ctx)

        # Product code/title evidence in the same local window is strongest.
        if ids:
            if any(compact(i) in compact(ctx) for i in ids if compact(i)):
                score += 8.0

        # Distance to nearest explicit model occurrence.
        if id_positions:
            distance = min(abs(pos - p) for p in id_positions)
            if distance <= 220:
                score += 6.0
            elif distance <= 600:
                score += 3.0
            elif distance <= 1200:
                score += 1.0

        # Ecommerce price cues.
        if "sepete ozel" in ctx_n:
            score += 2.5
        if any(x in ctx_n for x in ("satis fiyati", "fiyat", "kampanya", "sepete ekle")):
            score += 1.0

        # Prefer prices reasonably near the current source price if evidence ties.
        if source_price and source_price > 0:
            score += max(0.0, 2.0 - abs(1.0 - ratio))

        candidates.append((score, price, pos, ctx))

    if not candidates:
        # IMPORTANT: no "prices[0]" fallback here. That was the bug causing
        # unrelated 645 / 975 / 1308 TL values to leak into the UI.
        return None

    candidates.sort(key=lambda x: (-x[0], x[2]))
    return candidates[0][1]


# =========================================================
# LIGHTWEIGHT PRODUCT PAGE FALLBACK
# =========================================================

HTTP_PAGE_TIMEOUT = 9.0
MAX_HTTP_PAGE_BYTES = 1_200_000

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml",
}


def _jsonld_price(text: str) -> Optional[float]:
    """
    Extract schema.org / meta product prices without launching a browser.
    """
    patterns = [
        r'"price"\s*:\s*"?(?P<p>\d{1,8}(?:[.,]\d{1,2})?)"?',
        r'product:price:amount["\']?\s+content=["\'](?P<p>\d{1,8}(?:[.,]\d{1,2})?)',
        r'itemprop=["\']price["\'][^>]{0,180}content=["\'](?P<p>\d{1,8}(?:[.,]\d{1,2})?)',
        r'content=["\'](?P<p>\d{1,8}(?:[.,]\d{1,2})?)["\'][^>]{0,180}itemprop=["\']price["\']',
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I | re.S)
        if not m:
            continue
        raw = m.group("p").replace(",", ".")
        try:
            value = float(raw)
        except Exception:
            continue
        if value > 0:
            return value
    return None


def _html_stock_state(text: str) -> str:
    n = norm(text[:MAX_HTTP_PAGE_BYTES])
    c = compact(text[:MAX_HTTP_PAGE_BYTES])

    if any(compact(x) in c for x in (
        "schema.org/OutOfStock", '"availability":"OutOfStock"',
        "schema.org/SoldOut", '"availability":"SoldOut"',
    )):
        return "out"
    if any(x in n for x in OUT_OF_STOCK_PATTERNS):
        return "out"

    if any(compact(x) in c for x in (
        "schema.org/InStock", '"availability":"InStock"',
    )):
        return "in"
    if any(x in n for x in IN_STOCK_PATTERNS):
        return "in"
    return "unknown"


async def lightweight_product_page(url: str) -> tuple[Optional[float], str]:
    """
    Best-effort server-side HTML fetch. No Playwright, negligible RAM.
    Used only when Tavily found a convincing product URL but did not expose price.
    """
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_PAGE_TIMEOUT,
            follow_redirects=True,
            headers=_HTTP_HEADERS,
        ) as client:
            r = await client.get(url)

        if r.status_code != 200:
            print("COMPARE V21 HTTP SKIP:", r.status_code, host_of(url), flush=True)
            return None, "unknown"

        html = r.text[:MAX_HTTP_PAGE_BYTES]
        return _jsonld_price(html), _html_stock_state(html)
    except Exception as e:
        print("COMPARE V21 HTTP ERROR:", host_of(url), repr(e), flush=True)
        return None, "unknown"



# =========================================================
# TAVILY
# =========================================================

async def tavily_search(query: str, include_domains: Optional[list[str]] = None) -> list[dict]:
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not key:
        print("COMPARE V19 ERROR: TAVILY_API_KEY yok", flush=True)
        return []

    payload = {
        "query": query,
        "topic": "general",
        "search_depth": "advanced",
        "max_results": 20,
        "include_answer": False,
        "include_images": False,
        "include_raw_content": "text",
    }
    if include_domains:
        payload["include_domains"] = include_domains

    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT, follow_redirects=True) as client:
            r = await client.post(
                TAVILY_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            print("COMPARE V19 TAVILY:", r.status_code, query, flush=True)
            if r.status_code != 200:
                print("COMPARE V19 TAVILY BODY:", r.text[:500], flush=True)
                return []

            rows = (r.json() or {}).get("results") or []
            print("COMPARE V19 RESULTS:", len(rows), query, flush=True)
            return rows
    except Exception as e:
        print("COMPARE V19 TAVILY ERROR:", repr(e), flush=True)
        return []


def build_queries(title: str, brand: Optional[str], model: Optional[str], variant_text: Optional[str] = None) -> list[str]:
    out: list[str] = []

    def add(q: str):
        q = re.sub(r"\s+", " ", str(q or "")).strip()
        if q and all(norm(q) != norm(x) for x in out):
            out.append(q)

    variant_suffix = variant_query_suffix(title, variant_text)

    # Ray-Ban model + color; size remains a variant.
    rbm, rbc, _rbs = rayban_identity(title)
    if rbm:
        add(" ".join(x for x in (brand or "Ray-Ban", rbm.upper(), rbc, "TL") if x))

    ids = strong_ids(title, model)
    if ids:
        ident = ids[0]
        if brand:
            add(f"{brand} {ident} {variant_suffix} TL")
        add(f"{ident} {variant_suffix} TL")

    # Strong fallback for products that do not have a clean manufacturer code.
    meaningful = meaningful_title_query(title, brand)
    if meaningful:
        add(f"{meaningful} {variant_suffix} TL")
        add(f"{meaningful} {variant_suffix} fiyat")

    # Last-resort shorter query.
    core = []
    for w in norm(title).split():
        if w in STOP_WORDS or len(w) < 2:
            continue
        if w not in core:
            core.append(w)
        if len(core) >= 7:
            break
    if core:
        add(" ".join(core + ["TL"]))

    return out[:4]


# =========================================================
# STOCK / AVAILABILITY
# =========================================================

OUT_OF_STOCK_PATTERNS = (
    "stokta yok",
    "stok yok",
    "stoklarımızda yok",
    "stoklarimizda yok",
    "stoklarımızda bulunmamaktadır",
    "stoklarimizda bulunmamaktadir",
    "stokta bulunmuyor",
    "stokta bulunmamaktadır",
    "stokta bulunmamaktadir",
    "ürün stokta yok",
    "urun stokta yok",
    "tükendi",
    "tukendi",
    "ürün tükendi",
    "urun tukendi",
    "satışa kapalı",
    "satisa kapali",
    "satışta değil",
    "satista degil",
    "ürün mevcut değil",
    "urun mevcut degil",
    "mevcut değil",
    "mevcut degil",
    "geçici olarak temin edilemiyor",
    "gecici olarak temin edilemiyor",
    "temin edilemiyor",
    "tedarik edilemiyor",
    "stok bekleniyor",
    "stoklara gelince",
    "stoğa gelince",
    "stoga gelince",
    "gelince haber ver",
    "stok bildirimi",
    "yakında stoklarda",
    "yakinda stoklarda",
    "şu anda mevcut değil",
    "su anda mevcut degil",
    "currently unavailable",
    "temporarily unavailable",
    "temporarily out of stock",
    "not currently available",
    "out of stock",
    "sold out",
)

IN_STOCK_PATTERNS = (
    "stokta",
    "stokta var",
    "stoktan teslim",
    "sepete ekle",
    "hemen al",
    "satın al",
    "satin al",
    "in stock",
    "add to cart",
)


def product_local_text(
    full_text: str,
    source_title: str,
    source_model: Optional[str] = None,
    radius: int = 1200,
) -> str:
    """
    Pull a local window around the strongest product identity.
    This avoids treating an unrelated recommended item's stock message
    elsewhere on the same marketplace page as the candidate's stock state.
    """
    raw = str(full_text or "")
    if not raw:
        return ""

    nraw = norm(raw)
    needles = []

    for ident in strong_ids(source_title, source_model):
        ident_n = norm(ident)
        if ident_n:
            needles.append(ident_n)

    # Fallback: use a distinctive 3-5 word title fragment.
    title_words = [w for w in norm(source_title).split() if w not in STOP_WORDS and len(w) >= 3]
    if title_words:
        needles.append(" ".join(title_words[:5]))
        needles.append(" ".join(title_words[:3]))

    positions = []
    for needle in needles:
        pos = nraw.find(needle)
        if pos >= 0:
            positions.append(pos)

    if not positions:
        # Tavily's snippet/title is usually at the beginning; keep a conservative prefix.
        return raw[:3000]

    # norm() does not perfectly preserve indexes, but this is close enough for
    # nearby page text and much safer than scanning the entire page globally.
    pos = min(positions)
    lo = max(0, pos - radius)
    hi = min(len(raw), pos + radius)
    return raw[lo:hi]


def stock_state(
    full_text: str,
    source_title: str,
    source_model: Optional[str] = None,
) -> str:
    """
    Returns: "out", "in", or "unknown".
    Negative stock signals win when they occur in the local product window.
    """
    local_raw = product_local_text(full_text, source_title, source_model)
    local = norm(local_raw)

    # Structured-data values commonly exposed by indexed ecommerce pages.
    structured_out = (
        "outofstock",
        "soldout",
        "discontinued",
        "preorder",
        '"availability":"outofstock"',
        '"availability": "outofstock"',
    )
    structured_in = (
        "instock",
        '"availability":"instock"',
        '"availability": "instock"',
    )

    local_compact = compact(local_raw)

    if any(compact(x) in local_compact for x in structured_out):
        return "out"

    if any(p in local for p in OUT_OF_STOCK_PATTERNS):
        return "out"

    if any(compact(x) in local_compact for x in structured_in):
        return "in"

    if any(p in local for p in IN_STOCK_PATTERNS):
        return "in"

    return "unknown"


# =========================================================
# EXTRA QUERY HELPERS
# =========================================================

def meaningful_title_query(title: str, brand: Optional[str] = None) -> str:
    """
    Build a stronger fallback for products with no clean MPN/model code.
    Keeps distinctive words and dimensions/variants, while stripping marketplace fluff.
    """
    n = norm(title)
    junk = {
        "amazon.com.tr", "amazon", "moda", "fiyat", "yorumlari", "yorumları",
        "yetiskin", "yetişkin", "unisex",
    }

    toks = []
    for w in n.split():
        if w in STOP_WORDS or w in junk or len(w) < 2:
            continue
        if w not in toks:
            toks.append(w)
        if len(toks) >= 10:
            break

    pieces = []
    if brand and norm(brand) not in toks:
        pieces.append(str(brand))
    pieces.extend(toks)
    return " ".join(pieces).strip()



GENERIC_FAMILY_WORDS = {
    "set", "model", "renk", "siyah", "beyaz", "gri", "gray", "black", "white",
    "medium", "large", "small", "tek", "beden", "full", "yeni", "nesil",
    "mobil", "kontrol", "tak", "calistir", "secenegi", "olcu", "ozellik",
}


def identity_words_strict(text: str) -> set[str]:
    """
    More useful token set for model-less products.
    Keeps product-family words, removes generic marketplace/variant fluff.
    """
    return {
        w for w in words(text)
        if w not in GENERIC_FAMILY_WORDS
        and not re.fullmatch(r"\d+(?:[.,]\d+)?", w)
    }


def title_family_score(source_title: str, candidate_title: str) -> tuple[float, int, float]:
    sw = identity_words_strict(source_title)
    cw = identity_words_strict(candidate_title)
    if not sw or not cw:
        return 0.0, 0, 0.0

    shared = sw & cw
    recall = len(shared) / max(1, len(sw))
    precision = len(shared) / max(1, len(cw))

    # Recall is more important than precision because marketplace titles append
    # seller/marketing text. A valid title can therefore be much longer.
    weighted = (0.72 * recall) + (0.28 * precision)
    return weighted, len(shared), recall



# =========================================================
# PRODUCT FAMILY GUARD
# =========================================================

_PRODUCT_FAMILY_GROUPS = {
    "drinkware": {
        "matara", "suluk", "termos", "şişe", "sise", "bottle", "flask",
        "tumbler", "pipetli", "su matarasi", "su matarası",
    },
    "detergent": {
        "deterjan", "yumusatici", "yumuşatıcı", "camasir", "çamaşır",
        "sivi bakim", "sıvı bakım", "laundry", "detergent",
    },
    "tea_coffee": {
        "demlik", "caydanlik", "çaydanlık", "cay", "çay", "kahve",
        "teapot", "kettle", "suzgec", "süzgeç",
    },
    "mouse": {
        "mouse", "fare", "gaming mouse", "oyuncu mouse",
    },
    "mousepad": {
        "mousepad", "mouse pad", "pad",
    },
    "cooler": {
        "aio", "sivi sogutma", "sıvı soğutma", "liquid cooler",
        "water cooling", "radiator",
    },
    "led": {
        "led", "serit led", "şerit led", "neon", "rgb serit", "rgb şerit",
        "ambilight",
    },
    "eyewear": {
        "gozluk", "gözlük", "sunglasses", "ray-ban", "rayban",
    },
    "backpack": {
        "sirt cantasi", "sırt çantası", "backpack", "canta", "çanta",
    },
    "wall_panel": {
        "duvar paneli", "duvar kagidi", "duvar kağıdı", "pvc panel",
    },
}


def detect_product_families(text: str) -> set[str]:
    n = norm(text)
    out = set()
    for family, phrases in _PRODUCT_FAMILY_GROUPS.items():
        for phrase in phrases:
            if norm(phrase) in n:
                out.add(family)
                break
    return out


def product_family_conflict(source_title: str, candidate_title: str) -> tuple[bool, str]:
    """
    Hard reject if both sides clearly identify different product families.
    Generic measurements such as '1500 ml' can never override this.
    """
    sf = detect_product_families(source_title)
    cf = detect_product_families(candidate_title)

    if sf and cf and sf.isdisjoint(cf):
        return True, f"ürün ailesi farklı: kaynak={sorted(sf)} aday={sorted(cf)}"

    return False, ""


def strong_family_overlap(source_title: str, candidate_title: str) -> bool:
    """
    For model-less products, require at least one meaningful family signal.
    """
    sf = detect_product_families(source_title)
    cf = detect_product_families(candidate_title)
    if sf:
        return bool(sf & cf)

    # If the source family is unknown, demand distinctive lexical overlap,
    # excluding dimensions/volume and marketplace fluff.
    sw = identity_words_strict(source_title)
    cw = identity_words_strict(candidate_title)
    shared = sw & cw

    weak = {
        "1500", "ml", "lt", "litre", "l", "adet", "urun", "ürün",
        "siyah", "beyaz", "black", "white", "mega", "boy",
    }
    strong_shared = {w for w in shared if w not in weak and len(w) >= 4}
    return len(strong_shared) >= 2


def fallback_identity_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str] = None,
) -> tuple[float, str]:
    """
    Conservative but practical matching for products without MPN/model code.

    Old behavior used ordinary F1 >= .84. Search-result snippets are verbose, so
    good products were routinely rejected even when almost every source-title
    token was present. Here source-title recall is weighted more heavily.
    """
    conflict, why = hard_conflict(source_title, candidate_title)
    if conflict:
        return 0.0, why

    family_conflict, family_why = product_family_conflict(source_title, candidate_title)
    if family_conflict:
        return 0.0, family_why

    if not strong_family_overlap(source_title, candidate_title):
        return 0.0, "ürün ailesi doğrulanamadı"

    weighted, shared_count, recall = title_family_score(source_title, candidate_title)
    if shared_count < 3:
        return 0.0, "ayırt edici ortak kelime az"

    brand_hit = bool(
        source_brand
        and compact(source_brand)
        and compact(source_brand) in compact(candidate_title)
    )

    # Strong acceptance:
    # - candidate contains most of the source identity, or
    # - brand matches and a solid majority of source identity is present.
    if recall >= 0.78 and shared_count >= 4:
        score = 0.90 + min(0.06, (recall - 0.78) * 0.25)
    elif brand_hit and recall >= 0.62 and shared_count >= 4:
        score = 0.86 + min(0.05, (recall - 0.62) * 0.20)
    else:
        score = weighted + (0.08 if brand_hit else 0.0)

    # Capacity remains hard when both pages expose it.
    src_caps = capacities(source_title)
    cand_caps = capacities(candidate_title)
    if src_caps and cand_caps and src_caps.isdisjoint(cand_caps):
        return 0.0, "kapasite farklı"

    return min(score, 0.96), (
        f"başlık ailesi eşleşti: ortak={shared_count}, kaynak-kapsama={recall:.2f}"
    )



# =========================================================
# SIMILAR PRODUCTS
# =========================================================

@dataclass
class SimilarResult:
    store: str
    title: str
    price: Optional[float]
    url: str
    image_url: Optional[str]
    score: float
    reason: str


def _similarity_query(title: str, brand: Optional[str], model: Optional[str]) -> str:
    """
    Build a category/family-oriented query, intentionally broader than exact-match
    comparison. We remove the exact manufacturer code so alternatives can surface.
    """
    n = norm(title)

    # Drop strong IDs and storefront noise.
    for ident in strong_ids(title, model):
        ident_n = norm(ident)
        if ident_n:
            n = n.replace(ident_n, " ")

    # Remove exact Ray-Ban frame code if present, but keep brand/category words.
    n = re.sub(r"\brb\d{4}[a-z]?\b", " ", n)
    n = re.sub(r"\b\d{2,6}[-/]\d{3,10}\b", " ", n)

    toks = []
    for w in n.split():
        if w in STOP_WORDS or len(w) < 2:
            continue
        if w not in toks:
            toks.append(w)
        if len(toks) >= 8:
            break

    pieces = []
    if brand:
        pieces.append(str(brand))
    pieces.extend(toks)
    pieces.extend(["benzer", "alternatif", "TL"])
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()[:180]


def _similarity_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str],
) -> tuple[float, str]:
    """
    Similar != same.
    Require same rough product family/category words, but explicitly penalize
    exact same strong model so this endpoint recommends alternatives.
    """
    if is_accessory(candidate_title) and not is_accessory(source_title):
        return 0.0, "aksesuar"

    sw = words(source_title)
    cw = words(candidate_title)
    if not sw or not cw:
        return 0.0, "başlık yetersiz"

    shared = sw & cw
    recall = len(shared) / max(1, len(sw))
    precision = len(shared) / max(1, len(cw))
    f1 = 2 * precision * recall / max(0.0001, precision + recall)

    score = f1

    if source_brand and compact(source_brand) in compact(candidate_title):
        score += 0.10

    # Avoid returning exact same model as "similar".
    src_ids = strong_ids(source_title, None)
    if any(compact(x) in compact(candidate_title) for x in src_ids if compact(x)):
        score -= 0.35

    # Keep category/family suggestions conservative.
    if len(shared) < 3:
        return 0.0, "ortak ürün ailesi zayıf"

    return max(0.0, min(score, 0.95)), f"benzerlik={score:.2f}, ortak={len(shared)}"


async def find_similar_products(
    title: str,
    source_url: str,
    source_price: Optional[float],
    brand: Optional[str] = None,
    model: Optional[str] = None,
    limit: int = 8,
) -> list[SimilarResult]:
    """
    Tavily-index-only, RAM-safe similar product discovery.
    Never launches Playwright.
    """
    query = _similarity_query(title, brand, model)
    if not query:
        return []

    print("SIMILAR V2 START:", title, flush=True)
    print("SIMILAR V2 QUERY:", query, flush=True)

    shop_domains = list(KNOWN_TURKISH_SHOPS.keys())
    rows = await tavily_search(query, include_domains=shop_domains)

    if len(rows) < 8:
        rows.extend(await tavily_search(query))

    source_canonical = canonical_url(source_url)
    found: dict[str, SimilarResult] = {}

    for row in rows:
        url = str(row.get("url") or "").strip()
        rtitle = str(row.get("title") or "").strip()
        snippet = str(row.get("content") or "").strip()
        raw = str(row.get("raw_content") or "").strip()

        if not url or canonical_url(url) == source_canonical:
            continue
        if is_aggregator(url) or not is_turkish_shop(url) or not probable_product_page(url):
            continue

        full = f"{rtitle}\n{snippet}\n{raw}"

        # Never recommend unavailable alternatives.
        availability = stock_state(full, rtitle or title, None)
        if availability == "out":
            print(
                "SIMILAR V2 OUT OF STOCK REJECT:",
                host_of(url),
                rtitle[:110],
                flush=True,
            )
            continue

        score, reason = _similarity_score(title, f"{rtitle} {snippet}", brand)
        if score < 0.42:
            continue

        # Similar-product price need not be close to source price.
        price = choose_price(full, None, rtitle or title, None)
        if price is None:
            continue

        key = canonical_url(url)
        item = SimilarResult(
            store=shop_name(url),
            title=rtitle or "Benzer ürün",
            price=price,
            url=url,
            image_url=None,
            score=score,
            reason=reason,
        )
        old = found.get(key)
        if old is None or item.score > old.score:
            found[key] = item

    results = sorted(
        found.values(),
        key=lambda x: (-x.score, x.price if x.price is not None else 10**18),
    )[:max(1, min(int(limit or 8), 12))]

    print("SIMILAR V2 FINAL:", len(results), flush=True)
    return results


# =========================================================
# MAIN COMPARISON
# =========================================================

async def compare_product(
    title: str,
    source_store: str,
    source_url: str,
    source_price: Optional[float],
    brand: Optional[str] = None,
    model: Optional[str] = None,
    variant_text: Optional[str] = None,
) -> list[MatchResult]:
    print("=" * 56, flush=True)
    print("COMPARE V21 START:", title, flush=True)

    queries = build_queries(title, brand, model, variant_text)
    print("COMPARE V21 QUERIES:", queries, flush=True)

    rows: list[dict] = []
    shop_domains = list(KNOWN_TURKISH_SHOPS.keys())

    # Search ALL useful queries. The old early-stop after 10 plausible URLs caused
    # one noisy query to prevent the actual product query from ever running.
    for q in queries[:4]:
        rows.extend(await tavily_search(q, include_domains=shop_domains))

    # One unrestricted pass is always useful for Turkish stores missing from our list.
    if queries:
        rows.extend(await tavily_search(queries[0]))

    # Marketplace/store-group recovery passes. This prevents Trendyol/HB/etc.
    # from dominating one mixed-domain Tavily response and hiding other stores.
    if queries:
        recovery_groups = [
            [
                "trendyol.com", "hepsiburada.com", "amazon.com.tr",
                "n11.com", "pttavm.com", "pazarama.com", "idefix.com",
            ],
            [
                "itopya.com", "vatanbilgisayar.com", "teknosa.com",
                "mediamarkt.com.tr", "incehesap.com", "sinerji.gen.tr",
                "gaming.gen.tr", "tebilon.com", "inventus.com.tr",
                "gamegaraj.com", "qp.com.tr", "gencergaming.com",
                "wraithesports.com", "meyergaming.com", "neeko.com.tr",
            ],
        ]
        for group in recovery_groups:
            rows.extend(await tavily_search(queries[0], include_domains=group))

    # Deduplicate Tavily rows by canonical URL before expensive checks.
    unique_rows: dict[str, dict] = {}
    discovery_rank: dict[str, int] = {}
    for row_index, row in enumerate(rows):
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        key = canonical_url(url)
        discovery_rank.setdefault(key, row_index)
        old = unique_rows.get(key)
        if old is None:
            unique_rows[key] = row
        else:
            # Prefer the hit with richer indexed content.
            old_len = len(str(old.get("raw_content") or "")) + len(str(old.get("content") or ""))
            new_len = len(str(row.get("raw_content") or "")) + len(str(row.get("content") or ""))
            if new_len > old_len:
                unique_rows[key] = row

    print("COMPARE V21 UNIQUE URLS:", len(unique_rows), flush=True)

    candidates: dict[str, MatchResult] = {}
    candidate_meta: dict[str, dict] = {}
    source_canonical = canonical_url(source_url)
    source_ids = strong_ids(title, model)
    http_fallback_budget = 8

    for row in unique_rows.values():
        url = str(row.get("url") or "").strip()
        rtitle = str(row.get("title") or "").strip()
        snippet = str(row.get("content") or "").strip()
        raw = str(row.get("raw_content") or "").strip()

        if not url or canonical_url(url) == source_canonical:
            continue
        if is_aggregator(url):
            continue
        if not is_turkish_shop(url):
            continue
        if not probable_product_page(url):
            continue

        # Match primarily against result title. Snippets contain recommendations,
        # campaigns and other products, which used to contaminate identity scoring.
        identity_text = rtitle or snippet[:300]
        candidate_variant_evidence = f"{rtitle}\n{snippet}\n{raw}"

        family_bad, family_why = product_family_conflict(title, identity_text)
        if family_bad:
            print(
                "COMPARE V21 FAMILY REJECT:",
                host_of(url),
                rtitle[:100],
                family_why,
                flush=True,
            )
            continue

        variant_bad, variant_why = critical_variant_conflict(
            title,
            variant_text,
            candidate_variant_evidence,
        )
        if variant_bad:
            print(
                "COMPARE V21 VARIANT REJECT:",
                host_of(url),
                rtitle[:100],
                variant_why,
                flush=True,
            )
            continue

        if source_ids:
            score, reason = identity_score(title, identity_text, brand, model)
        else:
            score, reason = fallback_identity_score(title, identity_text, brand)

        if score < MIN_ACCEPT_SCORE:
            print(
                "COMPARE V21 ID REJECT:",
                host_of(url),
                round(score, 3),
                rtitle[:100],
                reason,
                flush=True,
            )
            continue

        full_evidence = candidate_variant_evidence
        availability = stock_state(full_evidence, title, model)

        if availability == "out":
            print("COMPARE V21 OUT OF STOCK REJECT:", host_of(url), rtitle[:110], flush=True)
            continue

        price = choose_price(full_evidence, source_price, title, model)

        # If Tavily found the correct page but its index omitted TL, try a cheap
        # HTML/JSON-LD fetch. No browser/Playwright is used.
        if price is None and http_fallback_budget > 0:
            http_fallback_budget -= 1
            page_price, page_stock = await lightweight_product_page(url)

            if page_stock == "out":
                print("COMPARE V21 HTTP OUT OF STOCK:", host_of(url), rtitle[:100], flush=True)
                continue

            if page_price is not None:
                # Keep the same anti-garbage plausibility gate.
                if source_price and source_price > 0:
                    ratio = page_price / float(source_price)
                    if 0.35 <= ratio <= 3.0:
                        price = page_price
                else:
                    price = page_price

            if availability == "unknown" and page_stock in {"in", "out"}:
                availability = page_stock

        if price is None:
            print("COMPARE V21 NO PRICE:", host_of(url), rtitle[:100], flush=True)
            continue

        result = MatchResult(
            store=shop_name(url),
            title=rtitle or title,
            price=price,
            url=url,
            image_url=None,
            score=score,
            reason=(
                "Aynı ürün kimliği doğrulandı"
                + (" + stokta" if availability == "in" else "")
                + ": " + reason
            ),
        )

        key = canonical_url(url)
        old = candidates.get(key)
        if old is None or result.score > old.score:
            candidates[key] = result
            candidate_meta[key] = {
                "availability": availability,
                "rank": discovery_rank.get(key, 999999),
                "price_distance": (
                    abs(float(price) - float(source_price)) / max(float(source_price), 1.0)
                    if source_price and source_price > 0 else 0.0
                ),
                "asin": amazon_asin(url),
            }
            print(
                "COMPARE V21 ACCEPT:",
                result.store,
                result.price,
                round(result.score, 3),
                result.title[:90],
                flush=True,
            )

    # Same URL only once first.
    unique: dict[tuple[str, str], MatchResult] = {}
    for item in candidates.values():
        key = (norm(item.store), canonical_url(item.url))
        old = unique.get(key)
        if old is None or item.score > old.score:
            unique[key] = item

    items = list(unique.values())

    # Amazon can expose duplicate ASIN detail pages for effectively the same family.
    # Do NOT blindly choose the cheapest one. Prefer:
    # 1) explicit in-stock evidence
    # 2) closer price to source
    # 3) earlier/more relevant Tavily discovery rank
    #
    # This directly prevents a weak duplicate ASIN from replacing the better
    # Amazon Türkiye detail page just because its extracted price is lower.
    amazon_items = [x for x in items if norm(x.store) == norm("Amazon Türkiye")]
    non_amazon_items = [x for x in items if norm(x.store) != norm("Amazon Türkiye")]

    if len(amazon_items) > 1:
        def amazon_quality(x: MatchResult):
            meta = candidate_meta.get(canonical_url(x.url), {})
            in_stock = 1 if meta.get("availability") == "in" else 0
            price_distance = float(meta.get("price_distance", 999.0))
            rank = int(meta.get("rank", 999999))
            return (-in_stock, -x.score, price_distance, rank)

        amazon_items.sort(key=amazon_quality)

        # Keep the best Amazon listing plus any materially different variant/identity
        # only if it survived our hard variant checks.
        amazon_items = amazon_items[:1]

    final = sorted(
        non_amazon_items + amazon_items,
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )[:MAX_FINAL_OFFERS]

    print("COMPARE V21 FINAL:", len(final), flush=True)
    for x in final:
        print("COMPARE V21 OFFER:", x.store, x.price, round(x.score, 3), x.url, flush=True)
    print("=" * 56, flush=True)

    return final

