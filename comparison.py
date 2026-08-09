import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx


# =========================================================
# GÖRKEM SEPETİ - COMPARISON V16 + STOCK-AWARE SIMILAR
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
    n = norm(text)
    out: set[str] = set()

    # 25G4SXU, RB2140, KF560C30BBEAK2-32...
    for t in re.findall(r"\b[a-z0-9][a-z0-9._+-]{3,}\b", n):
        if re.search(r"[a-z]", t) and re.search(r"\d", t):
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
# TAVILY
# =========================================================

async def tavily_search(query: str, include_domains: Optional[list[str]] = None) -> list[dict]:
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not key:
        print("COMPARE V14 ERROR: TAVILY_API_KEY yok", flush=True)
        return []

    payload = {
        "query": query,
        "topic": "general",
        "search_depth": "basic",
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
            print("COMPARE V14 TAVILY:", r.status_code, query, flush=True)
            if r.status_code != 200:
                print("COMPARE V14 TAVILY BODY:", r.text[:500], flush=True)
                return []

            rows = (r.json() or {}).get("results") or []
            print("COMPARE V14 RESULTS:", len(rows), query, flush=True)
            return rows
    except Exception as e:
        print("COMPARE V14 TAVILY ERROR:", repr(e), flush=True)
        return []


def build_queries(title: str, brand: Optional[str], model: Optional[str]) -> list[str]:
    out: list[str] = []

    def add(q: str):
        q = re.sub(r"\s+", " ", str(q or "")).strip()
        if q and all(norm(q) != norm(x) for x in out):
            out.append(q)

    # Ray-Ban model + color; size remains a variant.
    rbm, rbc, _rbs = rayban_identity(title)
    if rbm:
        add(" ".join(x for x in (brand or "Ray-Ban", rbm.upper(), rbc, "TL") if x))

    ids = strong_ids(title, model)
    if ids:
        ident = ids[0]
        if brand:
            add(f"{brand} {ident} TL")
        add(f"{ident} TL")

    # Strong fallback for products that do not have a clean manufacturer code.
    meaningful = meaningful_title_query(title, brand)
    if meaningful:
        add(f"{meaningful} TL")
        add(f"{meaningful} fiyat")

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


def fallback_identity_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str] = None,
) -> tuple[float, str]:
    """
    Conservative no-MPN fallback.
    Requires strong title overlap and no hard conflict.
    """
    conflict, why = hard_conflict(source_title, candidate_title)
    if conflict:
        return 0.0, why

    sw = words(source_title)
    cw = words(candidate_title)
    if not sw or not cw:
        return 0.0, "başlık yetersiz"

    shared = sw & cw
    recall = len(shared) / max(1, len(sw))
    precision = len(shared) / max(1, len(cw))
    f1 = 2 * precision * recall / max(0.0001, precision + recall)

    # Need at least 4 meaningful shared words when there is no strong ID.
    if len(shared) < 4:
        return 0.0, "ayırt edici ortak kelime az"

    score = f1
    if source_brand and compact(source_brand) in compact(candidate_title):
        score += 0.08

    # Dimensions/variants in title should agree when present.
    src_caps = capacities(source_title)
    cand_caps = capacities(candidate_title)
    if src_caps and cand_caps and src_caps.isdisjoint(cand_caps):
        return 0.0, "kapasite farklı"

    return min(score, 0.93), f"güçlü başlık eşleşmesi {score:.2f}"

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
    print("COMPARE V14 STABLE START:", title, flush=True)

    queries = build_queries(title, brand, model)
    print("COMPARE V14 QUERIES:", queries, flush=True)

    # We first search the real Turkish shops only.
    shop_domains = list(KNOWN_TURKISH_SHOPS.keys())
    rows: list[dict] = []

    for q in queries[:3]:
        batch = await tavily_search(q, include_domains=shop_domains)
        rows.extend(batch)

        # Count only plausible Turkish product pages, not raw Tavily rows.
        plausible_count = sum(
            1 for r in rows
            if is_turkish_shop(str(r.get("url") or ""))
            and probable_product_page(str(r.get("url") or ""))
        )
        if plausible_count >= 10:
            break

    # Focused source-marketplace pass: lets us find a cheaper alternate listing
    # on the SAME marketplace (e.g. another Hepsiburada product URL/seller).
    source_host = host_of(source_url)
    if queries and source_host:
        source_domain = None
        for domain in KNOWN_TURKISH_SHOPS:
            if source_host == domain or source_host.endswith("." + domain):
                source_domain = domain
                break
        if source_domain in {
            "hepsiburada.com", "trendyol.com", "amazon.com.tr",
            "n11.com", "pttavm.com", "pazarama.com",
        }:
            rows.extend(await tavily_search(queries[0], include_domains=[source_domain]))

    # If shop-domain search is sparse, one unrestricted query can discover
    # additional .com.tr/.gen.tr stores. Foreign results are discarded below.
    if len(rows) < 8 and queries:
        rows.extend(await tavily_search(queries[0]))

    candidates: dict[str, MatchResult] = {}
    source_canonical = canonical_url(source_url)

    for row in rows:
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

        evidence_title = f"{rtitle} {snippet}"
        source_ids = strong_ids(title, model)

        if source_ids:
            score, reason = identity_score(title, evidence_title, brand, model)
        else:
            score, reason = fallback_identity_score(title, evidence_title, brand)

        if score < MIN_ACCEPT_SCORE:
            continue

        full_evidence = f"{rtitle}\n{snippet}\n{raw}"

        # Explicitly remove out-of-stock / unavailable listings.
        availability = stock_state(full_evidence, title, model)
        if availability == "out":
            print(
                "COMPARE V14 OUT OF STOCK REJECT:",
                host_of(url),
                rtitle[:110],
                flush=True,
            )
            continue

        price = choose_price(full_evidence, source_price, title, model)
        if price is None:
            print("COMPARE V14 NO TL PRICE:", host_of(url), rtitle[:100], flush=True)
            continue

        result = MatchResult(
            store=shop_name(url),
            title=rtitle or title,
            price=price,
            url=url,
            image_url=None,
            score=score,
            reason=(
                "Tavily indeksinde aynı ürün + TL fiyatı doğrulandı"
                + (" + stokta" if availability == "in" else "")
                + ": " + reason
            ),
        )

        key = canonical_url(url)
        old = candidates.get(key)
        if old is None or result.score > old.score:
            candidates[key] = result

    # One row per store+price; duplicate URLs/query hits disappear.
    unique: dict[tuple[str, int], MatchResult] = {}
    for item in candidates.values():
        key = (norm(item.store), int(round(float(item.price or 0) * 100)))
        old = unique.get(key)
        if old is None or item.score > old.score:
            unique[key] = item

    final = sorted(
        unique.values(),
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )[:MAX_FINAL_OFFERS]

    print("COMPARE V14 FINAL:", len(final), flush=True)
    for x in final:
        print("COMPARE V14 OFFER:", x.store, x.price, round(x.score, 3), x.url, flush=True)
    print("=" * 56, flush=True)

    return final
