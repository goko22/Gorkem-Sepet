import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scraper import HEADERS, BROWSER_GOTO_TIMEOUT, get_browser, scrape_product
from utils import detect_store


# =========================================================
# AYARLAR
# =========================================================

SEARCH_STORES = {
    "Trendyol": [
        "https://www.trendyol.com/sr?q={q}",
    ],
    "Hepsiburada": [
        "https://www.hepsiburada.com/ara?q={q}",
    ],
    "Amazon Türkiye": [
        "https://www.amazon.com.tr/s?k={q}",
    ],
    "N11": [
        "https://www.n11.com/arama?q={q}",
    ],
    "İtopya": [
        "https://www.itopya.com/arama?q={q}",
        "https://www.itopya.com/Arama?text={q}",
    ],
    "Vatan": [
        "https://www.vatanbilgisayar.com/arama/{path_q}/",
    ],
    "Teknosa": [
        "https://www.teknosa.com/arama/?s={q}",
        "https://www.teknosa.com/arama?q={q}",
    ],
    "MediaMarkt": [
        "https://www.mediamarkt.com.tr/tr/search.html?query={q}",
    ],
    "idefix": [
        "https://www.idefix.com/arama?q={q}",
    ],
}

MAX_ANCHORS_PER_STORE = 80
MAX_PREFILTERED_PER_STORE = 5
MAX_VERIFIED_PER_STORE = 2
SEARCH_HTTP_TIMEOUT = 9


@dataclass
class Candidate:
    store: str
    title: str
    url: str
    pre_score: float


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
# NORMALIZATION / IDENTITY
# =========================================================

TR_MAP = str.maketrans(
    {
        "ı": "i",
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ö": "o",
        "ç": "c",
    }
)

STOP_WORDS = {
    "ve", "ile", "icin", "için", "the", "a", "an", "of",
    "urun", "ürün", "model", "modelleri", "fiyat", "fiyatlari",
    "siyah", "beyaz", "black", "white", "yeni", "new", "original",
    "orijinal", "resmi", "garantili", "turkiye", "türkiye",
    "amazon", "amazon.com.tr", "bilgisayar", "magaza", "mağaza",
}

GENERIC_PRODUCT_WORDS = {
    "aio", "sivi", "sıvı", "sogutma", "soğutma", "sogutucu", "soğutucu",
    "islemci", "işlemci", "cpu", "fan", "argb", "rgb", "oled", "amoled",
    "ekran", "kavisli", "curved", "pompa", "radiator", "radyator", "radyatör",
    "intel", "amd", "lga", "am5", "am4", "tdp", "w", "mm", "cm",
    "dondurulebilen", "döndürülebilen", "ozellestirilebilir",
    "özelleştirilebilir", "anamorfik", "etkisi", "set", "kit",
}

EDITION_MARKERS = {
    "se", "pro", "max", "ultra", "plus", "mini", "lite",
    "v2", "v3", "ii", "iii", "iv", "slc", "p28", "tl",
}

UNIT_ALIASES = {
    "tb": "tb",
    "gb": "gb",
    "mb": "mb",
    "hz": "hz",
    "khz": "khz",
    "mhz": "mhz",
    "ghz": "ghz",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "inch": "inch",
    "inc": "inch",
    "inç": "inch",
    "w": "w",
    "mah": "mah",
}


def norm(text: str) -> str:
    text = str(text or "").lower().translate(TR_MAP)
    text = text.replace("×", "x")
    text = re.sub(r"[^a-z0-9.+x\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words(text: str) -> set[str]:
    return {
        w
        for w in norm(text).split()
        if len(w) >= 2 and w not in STOP_WORDS
    }


def identity_words(text: str) -> set[str]:
    """Başlığın ürün kimliğini taşıyan ayırt edici kelimelerini çıkarır."""
    raw_words = words(text)
    return {
        w for w in raw_words
        if w not in GENERIC_PRODUCT_WORDS
        and not re.fullmatch(r"\d+", w)
        and len(w) >= 2
    }


def edition_tokens(text: str) -> set[str]:
    raw = set(norm(text).split())
    return raw & EDITION_MARKERS


def title_spec_tokens(text: str) -> list[str]:
    raw = norm(text)
    out = []
    for value, unit in re.findall(
        r"\b(\d+(?:[.,]\d+)?)\s*(tb|gb|hz|mhz|ghz|mm|cm|inch|inc)\b",
        raw,
    ):
        token = f"{value.replace(',', '.')}{UNIT_ALIASES.get(unit, unit)}"
        if token not in out:
            out.append(token)
    return out


def model_tokens(text: str) -> set[str]:
    raw = norm(text)
    out = set()

    for token in re.findall(r"\b[a-z0-9][a-z0-9._+-]{2,}\b", raw):
        has_alpha = bool(re.search(r"[a-z]", token))
        has_digit = bool(re.search(r"\d", token))

        if has_alpha and has_digit:
            out.add(token)

    return out


def series_tokens(text: str) -> set[str]:
    raw = norm(text)
    out = set()

    # Örn: "990 pro", "14 gen" gibi seri kalıpları.
    for number, word in re.findall(r"\b(\d{2,4})\s+([a-z]{2,12})\b", raw):
        if word not in {"gb", "tb", "hz", "mm", "cm", "mah"}:
            out.add(f"{number} {word}")

    return out


def dimension_tokens(text: str) -> set[str]:
    raw = norm(text)
    out = set()

    for a, b, unit in re.findall(
        r"\b(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m)?\b",
        raw,
    ):
        unit = unit or ""
        out.add(f"{a}x{b}{unit}")

    return out


def standalone_numbers(text: str) -> set[str]:
    raw = norm(text)
    return set(
        re.findall(r"(?<![a-z0-9])\d{2,4}(?![a-z0-9])", raw)
    )


def specs(text: str) -> dict[str, set[str]]:
    raw = norm(text)
    result: dict[str, set[str]] = {}

    for value, unit in re.findall(
        r"\b(\d+(?:[.,]\d+)?)\s*(tb|gb|mb|hz|khz|mhz|ghz|mm|cm|inch|inc|w|mah)\b",
        raw,
    ):
        canonical = UNIT_ALIASES.get(unit, unit)
        result.setdefault(canonical, set()).add(value.replace(",", "."))

    dims = dimension_tokens(raw)
    if dims:
        result["dimension"] = dims

    return result


def usable_model(model: Optional[str]) -> Optional[str]:
    if not model:
        return None

    value = norm(model)
    compact = re.sub(r"[^a-z0-9]", "", value)

    if not compact or compact.isdigit():
        return None

    # Pazar yeri dahili ürün kimlikleri gerçek üretici model kodu değildir.
    if compact.startswith(("hbcv", "hbc")):
        return None

    # Amazon ASIN benzeri dahili kodlar.
    if re.fullmatch(r"b0[a-z0-9]{8,}", compact):
        return None

    if len(compact) > 32:
        return None

    return value


def critical_variant_values(variant_text: Optional[str]) -> list[str]:
    if not variant_text:
        return []

    values = []

    for part in str(variant_text).split("•"):
        if ":" in part:
            _, value = part.split(":", 1)
        else:
            value = part

        value = norm(value)
        if value and value not in {"default", "standart", "standard"}:
            values.append(value)

    return values


def build_search_query(title: str, brand: Optional[str], model: Optional[str]) -> str:
    pieces: list[str] = []

    if brand:
        pieces.append(str(brand).strip())

    model_n = usable_model(model)
    if model_n:
        pieces.append(str(model).strip())

    seen = set()
    for w in norm(title).split():
        if (
            w in STOP_WORDS
            or w in GENERIC_PRODUCT_WORDS
            or re.fullmatch(r"\d+", w)
            or len(w) < 2
        ):
            continue
        if w not in seen:
            pieces.append(w)
            seen.add(w)
        if len(seen) >= 6:
            break

    for token in title_spec_tokens(title)[:3]:
        if token not in " ".join(pieces).lower():
            pieces.append(token)

    output = []
    seen_norm = set()
    for piece in pieces:
        n = norm(piece)
        if not n or n in seen_norm:
            continue
        seen_norm.add(n)
        output.append(piece)

    query = " ".join(output)
    query = re.sub(r"\s+", " ", query).strip()
    return query[:140]


# =========================================================
# STRICT MATCHING
# =========================================================


def hard_conflict(
    source_title: str,
    candidate_title: str,
    source_model: Optional[str] = None,
    source_variant_text: Optional[str] = None,
    candidate_variant_text: Optional[str] = None,
) -> tuple[bool, str]:
    src = norm(source_title)
    cand = norm(candidate_title)

    usable_source_model = usable_model(source_model)

    if usable_source_model:
        m = norm(usable_source_model)
        if (
            len(m) >= 4
            and re.search(r"\d", m)
            and re.sub(r"[^a-z0-9]", "", m)
                not in re.sub(r"[^a-z0-9]", "", cand)
        ):
            return True, f"model kodu yok: {source_model}"

    src_specs = specs(src + " " + str(source_variant_text or ""))
    cand_specs = specs(cand + " " + str(candidate_variant_text or ""))

    for unit, src_values in src_specs.items():
        cand_values = cand_specs.get(unit)
        if not cand_values:
            continue

        if src_values.isdisjoint(cand_values):
            return True, f"kritik teknik değer çelişiyor ({unit})"

    src_models = model_tokens(src)
    cand_models = model_tokens(cand)

    # Kaynakta güçlü bir alfanümerik model varsa ve adayda farklı bir güçlü model
    # varsa, yanlış eşleşmeye karşı reddet.
    strong_src = {
        x for x in src_models
        if len(x) >= 5
    }

    if strong_src and cand_models:
        if strong_src.isdisjoint(cand_models):
            # Model tokenları ürün adının küçük parçaları olabilir; bu yüzden yalnız
            # kaynak model tokenı aday metninde hiç yoksa sert ret uygula.
            if not any(token in cand for token in strong_src):
                return True, "model ailesi çelişiyor"

    # Aynı güçlü model kodu mevcutsa gözlük ölçüsü / seri alt kodu gibi
    # başlıkta tek başına duran sayıların çelişmesini de reddet.
    shared_strong_model = {
        x for x in model_tokens(src) & model_tokens(cand)
        if len(x) >= 5
    }

    if shared_strong_model:
        src_nums = standalone_numbers(src)
        cand_nums = standalone_numbers(cand)

        if len(src_nums) >= 2 and len(cand_nums) >= 2:
            if (src_nums & cand_nums) and src_nums != cand_nums:
                # Kaynak sayılar adayda tamamen mevcut değilse alt model/ölçü farklı.
                if not src_nums.issubset(cand_nums):
                    return True, "model alt kodu / ölçü çelişiyor"

    src_editions = edition_tokens(src)
    cand_editions = edition_tokens(cand)
    shared_identity = identity_words(src) & identity_words(cand)

    if len(shared_identity) >= 2 and src_editions != cand_editions:
        only_src = src_editions - cand_editions
        only_cand = cand_editions - src_editions
        if only_src or only_cand:
            return True, (
                "ürün sürümü çelişiyor: "
                f"kaynak={sorted(src_editions) or '-'}, "
                f"aday={sorted(cand_editions) or '-'}"
            )

    for value in critical_variant_values(source_variant_text):
        # Ölçü / kapasite / yüzey gibi kısa ama kritik varyantları ara.
        if re.search(r"\d", value) or value in {
            "soft", "xsoft", "mid", "surge", "control", "speed",
            "xl", "xxl", "xxxl",
        }:
            combined = cand + " " + norm(candidate_variant_text or "")
            if value not in combined:
                # Adayda başka açık varyant bilgisi varsa sert ret.
                if candidate_variant_text:
                    return True, f"varyant uyuşmuyor: {value}"

    return False, ""


def deterministic_score(
    source_title: str,
    candidate_title: str,
    source_brand: Optional[str] = None,
    source_model: Optional[str] = None,
    source_variant_text: Optional[str] = None,
    candidate_variant_text: Optional[str] = None,
) -> tuple[float, str]:
    conflict, reason = hard_conflict(
        source_title,
        candidate_title,
        source_model,
        source_variant_text,
        candidate_variant_text,
    )

    if conflict:
        return 0.0, reason

    src_words = words(source_title)
    cand_words = words(candidate_title)

    if not src_words or not cand_words:
        return 0.0, "başlık yetersiz"

    intersection = src_words & cand_words
    precision = len(intersection) / max(1, len(cand_words))
    recall = len(intersection) / max(1, len(src_words))
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    score = f1 * 0.42

    src_identity = identity_words(source_title)
    cand_identity = identity_words(candidate_title)
    identity_shared = src_identity & cand_identity

    if src_identity and cand_identity:
        id_precision = len(identity_shared) / max(1, len(cand_identity))
        id_recall = len(identity_shared) / max(1, len(src_identity))
        id_f1 = (
            2 * id_precision * id_recall / (id_precision + id_recall)
            if id_precision + id_recall
            else 0.0
        )
        score += id_f1 * 0.34
        if len(identity_shared) >= 3:
            score += 0.12
        elif len(identity_shared) == 2:
            score += 0.06
    else:
        id_f1 = 0.0

    src_models = model_tokens(source_title)
    cand_models = model_tokens(candidate_title)
    shared_models = src_models & cand_models

    if shared_models:
        score += min(0.30, 0.23 + 0.05 * len(shared_models))

    shared_series = series_tokens(source_title) & series_tokens(candidate_title)
    if shared_series:
        score += min(0.22, 0.16 + 0.03 * len(shared_series))

    if source_brand:
        brand_n = re.sub(r"[^a-z0-9]", "", norm(source_brand))
        cand_compact = re.sub(r"[^a-z0-9]", "", norm(candidate_title))
        src_compact = re.sub(r"[^a-z0-9]", "", norm(source_title))

        if brand_n and brand_n in cand_compact:
            score += 0.08
        elif brand_n and brand_n in src_compact:
            score -= 0.08

    usable_source_model = usable_model(source_model)
    if usable_source_model:
        model_n = re.sub(r"[^a-z0-9]", "", norm(usable_source_model))
        cand_compact = re.sub(r"[^a-z0-9]", "", norm(candidate_title))
        if model_n and model_n in cand_compact:
            score += 0.22

    src_specs = specs(source_title + " " + str(source_variant_text or ""))
    cand_specs = specs(candidate_title + " " + str(candidate_variant_text or ""))

    comparable = 0
    matched = 0

    for unit, values in src_specs.items():
        if unit in cand_specs:
            comparable += 1
            if not values.isdisjoint(cand_specs[unit]):
                matched += 1

    if comparable:
        score += 0.12 * (matched / comparable)

    strong_shared = {
        x for x in shared_models
        if len(x) >= 5
    }
    if strong_shared:
        src_nums = standalone_numbers(source_title)
        cand_nums = standalone_numbers(candidate_title)
        if src_nums and src_nums.issubset(cand_nums):
            score += 0.12

    variants = critical_variant_values(source_variant_text)
    if variants:
        combined = norm(candidate_title + " " + str(candidate_variant_text or ""))
        hits = sum(1 for value in variants if value in combined)
        if hits:
            score += min(0.10, 0.05 * hits)

    score = max(0.0, min(1.0, score))

    return score, (
        f"kelime={f1:.2f}, kimlik={id_f1:.2f}, "
        f"ortak_kimlik={len(identity_shared)}, model={len(shared_models)}, "
        f"teknik={matched}/{comparable}"
    )


# =========================================================
# OPTIONAL AI VERIFIER
# =========================================================


def _extract_response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue

            text = content.get("text")
            if isinstance(text, str):
                return text

    return ""


async def ai_verify_match(
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
    candidate_title: str,
    candidate_brand: Optional[str],
    candidate_model: Optional[str],
    candidate_variant_text: Optional[str],
) -> tuple[Optional[bool], float, str]:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None, 0.0, "AI kapalı"

    model = os.getenv(
        "OPENAI_MATCH_MODEL",
        "gpt-5-mini",
    )

    prompt = f"""
Sen bir e-ticaret ürün eşleştirme doğrulayıcısısın.
Amaç yanlış eşleşmeyi önlemektir. Şüphedeysen SAME=false de.
Aynı ürün ailesi yetmez; kapasite, boyut, model kodu, nesil, yüzey,
renk/beden kritikse ve farklıysa SAME=false olmalı.

KAYNAK:
Başlık: {source_title}
Marka: {source_brand or '-'}
Model: {source_model or '-'}
Varyant: {source_variant_text or '-'}

ADAY:
Başlık: {candidate_title}
Marka: {candidate_brand or '-'}
Model: {candidate_model or '-'}
Varyant: {candidate_variant_text or '-'}

Yalnızca tek satır JSON döndür:
{{"same":true veya false,"confidence":0 ile 1 arası sayı,"reason":"kısa neden"}}
""".strip()

    try:
        async with httpx.AsyncClient(timeout=18) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": prompt,
                },
            )

            response.raise_for_status()
            payload = response.json()

        text = _extract_response_text(payload).strip()
        match = re.search(r"\{.*\}", text, re.S)

        if not match:
            return None, 0.0, "AI JSON vermedi"

        data = json.loads(match.group(0))
        same = bool(data.get("same"))
        confidence = float(data.get("confidence") or 0)
        reason = str(data.get("reason") or "AI")[:220]

        return same, confidence, reason

    except Exception as e:
        print(
            "COMPARE AI ERROR:",
            repr(e),
            flush=True,
        )
        return None, 0.0, "AI hata"


# =========================================================
# STORE SEARCH
# =========================================================


def _search_urls(store: str, query: str) -> list[str]:
    encoded = quote(query)
    path_encoded = quote(query, safe="")

    return [
        template.format(
            q=encoded,
            path_q=path_encoded,
        )
        for template in SEARCH_STORES.get(store, [])
    ]


def _same_store(store: str, url: str) -> bool:
    detected = detect_store(url)
    return detected == store


def _is_probable_product_url(store: str, url: str) -> bool:
    path = urlparse(url).path.lower()

    if not path or path == "/":
        return False

    blocked = [
        "/arama", "/search", "/sr", "/kategori", "/category",
        "/marka", "/brand", "/magaza", "/seller", "/sepet",
        "/cart", "/login", "/giris", "/hesab", "/kampanya",
    ]

    if any(x in path for x in blocked):
        return False

    if store == "Amazon Türkiye":
        return "/dp/" in path or "/gp/product/" in path

    if store == "Hepsiburada":
        return "-p-" in path or "/p-" in path

    if store == "Trendyol":
        return "-p-" in path

    if store == "N11":
        return "/urun/" in path

    # Diğer mağazalarda başlık eşleşmesi ana filtre olacak.
    return len(path.strip("/").split("/")) >= 1


async def _fetch_search_html(url: str) -> str:
    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            timeout=SEARCH_HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)

            if response.status_code == 200 and len(response.text) > 5000:
                return response.text

    except Exception as e:
        print(
            "COMPARE SEARCH HTTP ERROR:",
            url,
            repr(e),
            flush=True,
        )

    browser = await get_browser()
    context = None
    page = None

    try:
        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1365, "height": 900},
        )

        page = await context.new_page()

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_GOTO_TIMEOUT,
            )
        except Exception:
            pass

        await page.wait_for_timeout(900)
        return await page.content()

    except Exception as e:
        print(
            "COMPARE SEARCH BROWSER ERROR:",
            url,
            repr(e),
            flush=True,
        )
        return ""

    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


def _extract_anchor_candidates(
    store: str,
    search_url: str,
    html: str,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    output: dict[str, Candidate] = {}

    for anchor in soup.find_all("a", href=True)[:2500]:
        href = str(anchor.get("href") or "").strip()

        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue

        url = urljoin(search_url, href).split("#", 1)[0]

        if not _same_store(store, url):
            continue

        if not _is_probable_product_url(store, url):
            continue

        title = anchor.get_text(" ", strip=True)

        if len(title) < 4:
            img = anchor.find("img")
            if img:
                title = str(
                    img.get("alt")
                    or img.get("title")
                    or ""
                ).strip()

        title = re.sub(r"\s+", " ", title).strip()

        if len(title) < 4:
            continue

        pre_score, reason = deterministic_score(
            source_title,
            title,
            source_brand,
            source_model,
            source_variant_text,
            None,
        )

        if pre_score < 0.34:
            continue

        previous = output.get(url)

        candidate = Candidate(
            store=store,
            title=title[:500],
            url=url,
            pre_score=pre_score,
        )

        if previous is None or candidate.pre_score > previous.pre_score:
            output[url] = candidate

        if len(output) >= MAX_ANCHORS_PER_STORE:
            break

    return sorted(
        output.values(),
        key=lambda x: x.pre_score,
        reverse=True,
    )[:MAX_PREFILTERED_PER_STORE]


async def search_store_candidates(
    store: str,
    query: str,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> list[Candidate]:
    all_candidates: dict[str, Candidate] = {}

    for search_url in _search_urls(store, query):
        print(
            "COMPARE SEARCH:",
            store,
            search_url,
            flush=True,
        )

        html = await _fetch_search_html(search_url)

        if not html:
            continue

        candidates = _extract_anchor_candidates(
            store,
            search_url,
            html,
            source_title,
            source_brand,
            source_model,
            source_variant_text,
        )

        for candidate in candidates:
            old = all_candidates.get(candidate.url)
            if old is None or candidate.pre_score > old.pre_score:
                all_candidates[candidate.url] = candidate

        if all_candidates:
            break

    return sorted(
        all_candidates.values(),
        key=lambda x: x.pre_score,
        reverse=True,
    )[:MAX_PREFILTERED_PER_STORE]


# =========================================================
# VERIFY CANDIDATES
# =========================================================


async def verify_candidate(
    candidate: Candidate,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> Optional[MatchResult]:
    try:
        scraped = await scrape_product(candidate.url)
    except Exception as e:
        print(
            "COMPARE CANDIDATE SCRAPE ERROR:",
            candidate.store,
            candidate.url,
            repr(e),
            flush=True,
        )
        return None

    score, reason = deterministic_score(
        source_title,
        scraped.title,
        source_brand,
        source_model,
        source_variant_text,
        scraped.variant_text,
    )

    # Çok güçlü deterministik eşleşme doğrudan kabul.
    if score >= 0.77:
        return MatchResult(
            store=candidate.store,
            title=scraped.title,
            price=scraped.price,
            url=scraped.url,
            image_url=scraped.image_url,
            score=score,
            reason="Katı doğrulama: " + reason,
        )

    # Orta-yüksek eşleşmede AI yalnızca ikinci doğrulayıcıdır.
    # AI tek başına ürün kabul edemez.
    if score >= 0.70:
        same, confidence, ai_reason = await ai_verify_match(
            source_title,
            source_brand,
            source_model,
            source_variant_text,
            scraped.title,
            scraped.brand,
            scraped.model,
            scraped.variant_text,
        )

        if same is True and confidence >= 0.98:
            final_score = min(
                0.99,
                max(score, confidence),
            )

            return MatchResult(
                store=candidate.store,
                title=scraped.title,
                price=scraped.price,
                url=scraped.url,
                image_url=scraped.image_url,
                score=final_score,
                reason=f"Katı+AI: {ai_reason}",
            )

    print(
        "COMPARE REJECT:",
        candidate.store,
        scraped.title,
        "score=",
        round(score, 3),
        reason,
        flush=True,
    )

    return None


async def compare_product(
    title: str,
    source_store: str,
    source_url: str,
    source_price: Optional[float],
    brand: Optional[str] = None,
    model: Optional[str] = None,
    variant_text: Optional[str] = None,
) -> list[MatchResult]:
    query = build_search_query(
        title,
        brand,
        model,
    )

    print(
        "COMPARE QUERY:",
        query,
        flush=True,
    )

    # Kaynak mağazayı da arayabiliriz; farklı satıcı fiyatı bulma ihtimali var.
    stores = list(SEARCH_STORES.keys())

    semaphore = asyncio.Semaphore(3)

    async def one_store(store: str):
        async with semaphore:
            try:
                candidates = await search_store_candidates(
                    store,
                    query,
                    title,
                    brand,
                    model,
                    variant_text,
                )

                results = []

                for candidate in candidates[:MAX_VERIFIED_PER_STORE]:
                    # Kaynak URL'nin aynısını yeniden teklif olarak eklemeye gerek yok.
                    if candidate.url.rstrip("/") == source_url.rstrip("/"):
                        continue

                    result = await verify_candidate(
                        candidate,
                        title,
                        brand,
                        model,
                        variant_text,
                    )

                    if result is not None:
                        results.append(result)
                        break

                return results

            except Exception as e:
                print(
                    "COMPARE STORE ERROR:",
                    store,
                    repr(e),
                    flush=True,
                )
                return []

    batches = await asyncio.gather(
        *(one_store(store) for store in stores)
    )

    results = [
        item
        for batch in batches
        for item in batch
    ]

    # Kaynak teklifi de karşılaştırma listesinde sabit tut.
    results.append(
        MatchResult(
            store=source_store,
            title=title,
            price=source_price,
            url=source_url,
            image_url=None,
            score=1.0,
            reason="Kaynak ürün",
        )
    )

    unique: dict[tuple[str, str], MatchResult] = {}

    for result in results:
        key = (
            result.store,
            result.url.rstrip("/"),
        )

        previous = unique.get(key)

        if previous is None or result.score > previous.score:
            unique[key] = result

    return sorted(
        unique.values(),
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )
