import asyncio
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx

from scraper import scrape_product


# =========================================================
# GÖRKEM SEPETİ - PRICE COMPARISON V6
# Tavily = discovery only
# Store product page = source of truth
# =========================================================

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 20.0
MAX_TAVILY_RESULTS = 20
MAX_VERIFY_CANDIDATES = 16
VERIFY_CONCURRENCY = 4
MIN_ACCEPT_SCORE = 0.84

AGGREGATOR_HOSTS = {
    "akakce.com",
    "www.akakce.com",
    "ucuzacebin.de",
    "www.ucuzacebin.de",
}

BLOCKED_HOST_FRAGMENTS = {
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "x.com",
    "twitter.com", "tiktok.com", "pinterest.", "reddit.com",
    "eksisozluk.com", "technopat.net", "donanimhaber.com",
}

KNOWN_SHOP_DOMAINS = [
    "trendyol.com",
    "hepsiburada.com",
    "amazon.com.tr",
    "n11.com",
    "itopya.com",
    "vatanbilgisayar.com",
    "teknosa.com",
    "mediamarkt.com.tr",
    "idefix.com",
    "sinerji.gen.tr",
    "incehesap.com",
    "gaming.gen.tr",
    "tebilon.com",
    "inventus.com.tr",
    "gamegaraj.com",
    "wraithesports.com",
    "meyergaming.com",
    "neeko.com.tr",
    "pazarama.com",
    "pttavm.com",
]

TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "source",
}

TR_MAP = str.maketrans({
    "ı": "i", "ğ": "g", "ü": "u", "ş": "s", "ö": "o", "ç": "c",
})

STOP_WORDS = {
    "ve", "ile", "icin", "the", "a", "an", "of", "urun", "urunu",
    "fiyat", "fiyati", "satinal", "satin", "al", "gaming", "oyuncu",
    "kablosuz", "siyah", "beyaz", "black", "white", "turkiye",
}

ACCESSORY_WORDS = {
    "ekran koruyucu", "screen protector", "koruyucu film", "koruyucu cam",
    "kilif", "case", "tasima cantasi", "stand", "bracket", "duvar aparati",
    "yedek parca", "replacement", "cover",
}

EDITION_MARKERS = {
    "se", "pro", "max", "ultra", "plus", "mini", "lite",
    "v2", "v3", "ii", "iii", "iv", "slc", "p28",
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


@dataclass
class DiscoveryCandidate:
    title: str
    url: str
    snippet: str
    discovery_score: float


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


def token_words(text: str) -> set[str]:
    return {
        x for x in norm(text).split()
        if len(x) >= 2 and x not in STOP_WORDS
    }


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


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return ""


def store_name_from_url(url: str, scraped_store: Optional[str] = None) -> str:
    if scraped_store and norm(scraped_store) not in {"", "bilinmeyen", "unknown", "generic"}:
        return scraped_store

    host = host_of(url)
    aliases = {
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
        "pttavm.com": "PttAVM",
        "pazarama.com": "Pazarama",
    }
    for domain, name in aliases.items():
        if host == domain or host.endswith("." + domain):
            return name

    if host:
        stem = host.split(".")[0]
        if stem:
            return stem.replace("-", " ").title()
    return "X Mağaza"


def is_blocked_discovery_url(url: str, source_url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return True

    host = host_of(url)
    if not host:
        return True

    if host in {x.replace("www.", "") for x in AGGREGATOR_HOSTS}:
        return True

    if any(x in host for x in BLOCKED_HOST_FRAGMENTS):
        return True

    # The source product itself must never become a comparison row.
    if canonical_url(url) == canonical_url(source_url):
        return True

    path = (urlparse(url).path or "").lower()
    nav_parts = (
        "/search", "/arama", "/kategori", "/category", "/blog", "/haber",
        "/news", "/forum", "/yardim", "/help",
    )
    if any(x in path for x in nav_parts):
        return True

    return False


# =========================================================
# PRODUCT IDENTITY
# =========================================================

def model_like_tokens(text: str) -> set[str]:
    """
    Strong product identifiers:
    - 25G4SXU
    - KF560C30BBEAK2-32
    - RB2140
    - 910-006631 (numeric manufacturer code)
    """
    n = norm(text)
    out: set[str] = set()

    # Alphanumeric model codes.
    for tok in re.findall(r"\b[a-z0-9][a-z0-9._+-]{3,}\b", n):
        if re.search(r"[a-z]", tok) and re.search(r"\d", tok):
            out.add(tok)

    # Numeric manufacturer codes with separators: 910-006631, 0RB2140 etc.
    for tok in re.findall(r"\b\d{2,6}[-/]\d{3,10}\b", n):
        out.add(tok)

    return out


def strongest_identifiers(text: str, model: Optional[str] = None) -> list[str]:
    candidates = set(model_like_tokens(text))

    if model:
        m = norm(model).strip()
        mc = compact(m)
        # Ignore marketplace internal IDs.
        if (
            len(mc) >= 5
            and not mc.startswith(("hbcv", "hbc", "b0"))
            and len(mc) <= 40
        ):
            if re.search(r"\d", m):
                candidates.add(m)

    def rank(x: str):
        has_alpha = bool(re.search(r"[a-z]", x))
        has_digit = bool(re.search(r"\d", x))
        has_sep = "-" in x or "/" in x
        return (has_alpha and has_digit, has_sep, len(x))

    return sorted(candidates, key=rank, reverse=True)


def capacity_tokens(text: str) -> set[str]:
    n = norm(text)
    return {
        f"{v.replace(',', '.')}{u}"
        for v, u in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*(tb|gb|mb)\b", n)
    }


def generation_tokens(text: str) -> set[str]:
    n = norm(text)
    out = set()
    # Superlight 2, Airpods 4 etc.
    toks = n.split()
    for i in range(1, len(toks)):
        if re.fullmatch(r"[2-9]", toks[i]) and re.search(r"[a-z]", toks[i - 1]):
            out.add(toks[i - 1] + " " + toks[i])
    return out


def edition_tokens(text: str) -> set[str]:
    return set(norm(text).split()) & EDITION_MARKERS


def is_accessory(text: str) -> bool:
    n = norm(text)
    return any(p in n for p in ACCESSORY_WORDS)


def looks_like_eyewear(text: str) -> bool:
    n = norm(text)
    return any(x in n for x in ("ray-ban", "ray ban", "gunes gozlugu", "gozluk", "eyewear"))


def rayban_identity(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    RB2140 901 50 -> model RB2140, color 901, size 50.
    Size differences are allowed as same product family.
    """
    n = norm(text)
    model_m = re.search(r"\b(rb\d{4}[a-z]?)\b", n)
    if not model_m:
        return None, None, None

    model = model_m.group(1)
    after = n[model_m.end():]
    nums = re.findall(r"\b\d{2,4}\b", after)
    color = nums[0] if nums else None
    size = nums[1] if len(nums) > 1 else None
    return model, color, size


def hard_conflict(source_title: str, candidate_title: str) -> tuple[bool, str]:
    s = norm(source_title)
    c = norm(candidate_title)

    if is_accessory(candidate_title) and not is_accessory(source_title):
        return True, "aksesuar / ana ürün çelişkisi"

    # Ray-Ban: same model+color, different lens size is allowed.
    if looks_like_eyewear(source_title) and looks_like_eyewear(candidate_title):
        sm, sc, _ss = rayban_identity(source_title)
        cm, cc, _cs = rayban_identity(candidate_title)
        if sm and cm and sm != cm:
            return True, "gözlük modeli farklı"
        if sc and cc and sc != cc:
            return True, "gözlük renk/kod varyantı farklı"
        # size intentionally not a conflict

    # Capacity is critical for storage/memory products.
    scaps = capacity_tokens(source_title)
    ccaps = capacity_tokens(candidate_title)
    if scaps and ccaps and scaps.isdisjoint(ccaps):
        return True, "kapasite farklı"

    sg = generation_tokens(source_title)
    cg = generation_tokens(candidate_title)
    if sg != cg and (sg or cg):
        roots = {x.rsplit(" ", 1)[0] for x in sg | cg}
        if roots & (token_words(source_title) & token_words(candidate_title)):
            return True, "ürün nesli farklı"

    se = edition_tokens(source_title)
    ce = edition_tokens(candidate_title)
    common = token_words(source_title) & token_words(candidate_title)
    if len(common) >= 2 and se != ce and (se or ce):
        return True, "ürün sürümü farklı"

    return False, ""


def identity_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str] = None,
    source_model: Optional[str] = None,
) -> tuple[float, str]:
    bad, why = hard_conflict(source_title, candidate_title)
    if bad:
        return 0.0, why

    sw = token_words(source_title)
    cw = token_words(candidate_title)
    if not sw or not cw:
        return 0.0, "başlık yetersiz"

    shared = sw & cw
    dice = 2 * len(shared) / max(1, len(sw) + len(cw))
    short_cov = len(shared) / max(1, min(len(sw), len(cw)))

    sids = strongest_identifiers(source_title, source_model)
    cids = strongest_identifiers(candidate_title, None)

    # Strong exact code is the main evidence.
    exact_codes = set(map(compact, sids)) & set(map(compact, cids))
    exact_codes.discard("")

    if exact_codes:
        score = max(0.92, 0.72 + 0.20 * short_cov)
        if source_brand and compact(source_brand) in compact(candidate_title):
            score += 0.04
        return min(0.99, score), f"güçlü model kodu eşleşti: {sorted(exact_codes)}"

    # If source has a strong identifier, candidate must carry it.
    if sids:
        cand_comp = compact(candidate_title)
        matched = [x for x in sids if compact(x) and compact(x) in cand_comp]
        if not matched:
            return 0.0, f"kaynak model kodu adayda yok: {sids[0]}"
        return 0.92, f"model kodu metinde eşleşti: {matched[0]}"

    score = 0.58 * short_cov + 0.42 * dice
    if source_brand and compact(source_brand) in compact(candidate_title):
        score += 0.05
    if len(shared) >= 4:
        score += 0.05

    return min(0.93, score), f"başlık eşleşmesi: ortak={len(shared)}, kapsama={short_cov:.2f}"


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
    # Brand contradiction only if both brands are real values.
    if left_brand and right_brand:
        lb, rb = compact(left_brand), compact(right_brand)
        if lb and rb and lb != rb:
            return 0.0, "marka farklı"

    # Hard conflicts must win in either direction.
    bad, why = hard_conflict(left_title, right_title)
    if bad:
        return 0.0, why
    bad, why = hard_conflict(right_title, left_title)
    if bad:
        return 0.0, why

    score1, reason1 = identity_score(left_title, right_title, left_brand, left_model)
    score2, reason2 = identity_score(right_title, left_title, right_brand, right_model)

    # Symmetric product matching: both directions should be credible.
    score = min(score1, score2)
    return score, reason1 if score1 <= score2 else reason2


# =========================================================
# SEARCH QUERIES
# =========================================================

def build_queries(title: str, brand: Optional[str], model: Optional[str]) -> list[str]:
    """
    Search-engine query generation must be minimal.
    Long Turkish ecommerce titles are intentionally NOT the first query.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str):
        q = re.sub(r"\s+", " ", str(q or "")).strip()
        k = norm(q)
        if q and k not in seen:
            seen.add(k)
            out.append(q)

    ids = strongest_identifiers(title, model)

    # Ray-Ban style identity: model + color code; lens size is a variant.
    if looks_like_eyewear(title):
        rb_model, rb_color, _rb_size = rayban_identity(title)
        if rb_model:
            add(" ".join(x for x in (brand or "Ray-Ban", rb_model.upper(), rb_color) if x))

    # Exact manufacturer code is the strongest discovery query.
    if ids:
        ident = ids[0]
        add(ident)
        if brand:
            add(f"{brand} {ident}")

    # Short product-family fallback.
    words = [
        x for x in norm(title).split()
        if x not in STOP_WORDS and len(x) >= 2
    ]
    core: list[str] = []
    for x in words:
        if x not in core:
            core.append(x)
        if len(core) >= 6:
            break

    if core:
        add(" ".join(([brand] if brand else []) + core))

    return out[:4]


# =========================================================
# TAVILY DISCOVERY
# =========================================================

async def tavily_search(
    query: str,
    *,
    include_domains: Optional[list[str]] = None,
    country: Optional[str] = None,
) -> list[dict]:
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        print("COMPARE TAVILY ERROR: TAVILY_API_KEY missing", flush=True)
        return []

    payload = {
        "query": query,
        "topic": "general",
        "search_depth": "basic",
        "max_results": MAX_TAVILY_RESULTS,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if country:
        payload["country"] = country

    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT, follow_redirects=True) as client:
            r = await client.post(
                TAVILY_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            print(
                "COMPARE TAVILY HTTP:",
                r.status_code,
                "query=",
                query,
                "domains=",
                "shops" if include_domains else "web",
                "country=",
                country or "-",
                flush=True,
            )

            if r.status_code != 200:
                print(
                    "COMPARE TAVILY BODY:",
                    r.text[:700].replace("\n", " "),
                    flush=True,
                )
                return []

            data = r.json()
            results = data.get("results") or []
            print(
                "COMPARE TAVILY RESULTS:",
                len(results),
                "query=",
                query,
                flush=True,
            )

            # 200 + [] is not treated as success. Log enough metadata to diagnose it.
            if not results:
                print(
                    "COMPARE TAVILY EMPTY META:",
                    {
                        "response_time": data.get("response_time"),
                        "request_id": data.get("request_id"),
                        "usage": data.get("usage"),
                    },
                    flush=True,
                )

            return results
    except Exception as e:
        print("COMPARE TAVILY EXCEPTION:", repr(e), "query=", query, flush=True)
        return []


def discovery_score(
    source_title: str,
    result_title: str,
    snippet: str,
    source_brand: Optional[str],
    source_model: Optional[str],
) -> float:
    merged = f"{result_title} {snippet}"
    score, _ = identity_score(source_title, merged, source_brand, source_model)

    ids = strongest_identifiers(source_title, source_model)
    mc = compact(merged)
    if ids and any(compact(i) in mc for i in ids if compact(i)):
        score = max(score, 0.96)

    # Ray-Ban product-family pages can omit lens size; model + color is enough
    # for discovery. Store-page verification still runs afterward.
    if looks_like_eyewear(source_title):
        sm, sc, _ss = rayban_identity(source_title)
        rm, rc, _rs = rayban_identity(merged)
        if sm and rm and sm == rm and (not sc or not rc or sc == rc):
            score = max(score, 0.94)

    return score


async def discover_candidates(
    title: str,
    brand: Optional[str],
    model: Optional[str],
    source_url: str,
) -> list[DiscoveryCandidate]:
    queries = build_queries(title, brand, model)
    print("COMPARE V7 QUERIES:", queries, flush=True)

    found: dict[str, DiscoveryCandidate] = {}

    def absorb(rows: list[dict]):
        for row in rows:
            url = str(row.get("url") or "").strip()
            if is_blocked_discovery_url(url, source_url):
                continue

            rtitle = str(row.get("title") or "").strip()
            snippet = str(row.get("content") or "").strip()
            score = discovery_score(title, rtitle, snippet, brand, model)

            # Discovery may be somewhat loose; final store-page verification is strict.
            if score < 0.66:
                continue

            key = canonical_url(url)
            candidate = DiscoveryCandidate(rtitle, url, snippet, score)
            old = found.get(key)
            if old is None or candidate.discovery_score > old.discovery_score:
                found[key] = candidate

    if not queries:
        return []

    # 1) Broad web, exact/short query, NO country parameter.
    # Country is deliberately omitted because the user's logs showed HTTP 200 + 0
    # on shopping queries while country="turkey" was present.
    rows = await tavily_search(queries[0])
    absorb(rows)

    # 2) Explicit Turkish ecommerce domains. This is still Tavily web search,
    # not HTML scraping of search pages.
    if len(found) < 6:
        rows = await tavily_search(
            queries[0],
            include_domains=KNOWN_SHOP_DOMAINS,
        )
        absorb(rows)

    # 3) Alternate query, again unrestricted.
    if len(found) < 6 and len(queries) > 1:
        rows = await tavily_search(queries[1])
        absorb(rows)

    # 4) Last fallback: broader short family query if available.
    if len(found) < 4 and len(queries) > 2:
        rows = await tavily_search(queries[2])
        absorb(rows)

    items = sorted(found.values(), key=lambda x: x.discovery_score, reverse=True)

    print("COMPARE DISCOVERED:", len(items), flush=True)
    for c in items[:12]:
        print(
            "COMPARE CANDIDATE:",
            round(c.discovery_score, 3),
            host_of(c.url),
            c.title[:140],
            flush=True,
        )

    return items[:MAX_VERIFY_CANDIDATES]


# =========================================================
# STORE-PAGE VERIFICATION
# =========================================================

async def verify_candidate(
    candidate: DiscoveryCandidate,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_url: str,
) -> Optional[MatchResult]:
    try:
        scraped = await scrape_product(candidate.url)
    except Exception as e:
        print(
            "COMPARE VERIFY SCRAPE ERROR:",
            host_of(candidate.url),
            repr(e),
            flush=True,
        )
        return None

    actual_url = str(scraped.url or candidate.url)
    if canonical_url(actual_url) == canonical_url(source_url):
        print("COMPARE VERIFY REJECT: source-url", actual_url, flush=True)
        return None

    actual_title = str(scraped.title or candidate.title or "").strip()
    score, reason = identity_score(
        source_title,
        actual_title,
        source_brand,
        source_model,
    )

    if score < MIN_ACCEPT_SCORE:
        print(
            "COMPARE VERIFY REJECT:",
            round(score, 3),
            host_of(actual_url),
            actual_title[:140],
            reason,
            flush=True,
        )
        return None

    price = scraped.price
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        print(
            "COMPARE VERIFY REJECT: no-price",
            host_of(actual_url),
            actual_title[:120],
            flush=True,
        )
        return None

    store = store_name_from_url(actual_url, getattr(scraped, "store", None))

    print(
        "COMPARE VERIFIED:",
        store,
        float(price),
        round(score, 3),
        actual_title[:120],
        flush=True,
    )

    return MatchResult(
        store=store,
        title=actual_title,
        price=float(price),
        url=actual_url,
        image_url=getattr(scraped, "image_url", None),
        score=score,
        reason="Mağaza sayfasından doğrulandı: " + reason,
    )


# =========================================================
# MAIN API USED BY services.py
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
    print("=" * 50, flush=True)
    print("COMPARE V7 START:", title, flush=True)
    print("COMPARE SOURCE:", source_store, source_url, flush=True)

    candidates = await discover_candidates(
        title=title,
        brand=brand,
        model=model,
        source_url=source_url,
    )

    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def one(c: DiscoveryCandidate):
        async with sem:
            return await verify_candidate(
                c,
                source_title=title,
                source_brand=brand,
                source_model=model,
                source_url=source_url,
            )

    verified_raw = await asyncio.gather(*(one(c) for c in candidates))
    verified = [x for x in verified_raw if x is not None]

    # Never return source URL, even if a redirect/canonicalization edge case slipped through.
    verified = [
        x for x in verified
        if canonical_url(x.url) != canonical_url(source_url)
    ]

    # Keep one best row per actual product URL.
    by_url: dict[str, MatchResult] = {}
    for result in verified:
        key = canonical_url(result.url)
        old = by_url.get(key)
        if old is None or result.score > old.score:
            by_url[key] = result

    # Also avoid duplicates from the same merchant+same price.
    by_offer: dict[tuple[str, int], MatchResult] = {}
    for result in by_url.values():
        key = (norm(result.store), int(round(float(result.price or 0) * 100)))
        old = by_offer.get(key)
        if old is None or result.score > old.score:
            by_offer[key] = result

    final = sorted(
        by_offer.values(),
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )

    print(
        "COMPARE FINAL:",
        len(final),
        "verified offers",
        flush=True,
    )
    for x in final:
        print(
            "COMPARE FINAL ROW:",
            x.store,
            x.price,
            round(x.score, 3),
            x.url,
            flush=True,
        )
    print("=" * 50, flush=True)
    return final
