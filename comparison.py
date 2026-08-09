import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scraper import HEADERS, BROWSER_GOTO_TIMEOUT, get_browser


# =========================================================
# AYARLAR
# =========================================================

AGGREGATOR_SEARCH = {
    "Akakçe": [
        "https://www.akakce.com/arama/?q={q}",
    ],
    "UcuzaCebin": [
        "https://www.ucuzacebin.de/search?q={q}",
        "https://www.ucuzacebin.de/search?query={q}",
    ],
}

AGGREGATOR_HOSTS = {
    "Akakçe": {"akakce.com", "www.akakce.com"},
    "UcuzaCebin": {"ucuzacebin.de", "www.ucuzacebin.de"},
}

SEARCH_HTTP_TIMEOUT = 12
MAX_PRODUCT_PAGES_PER_SOURCE = 3
MIN_PRODUCT_PAGE_SCORE = 0.74
MAX_OFFERS_PER_SOURCE = 60

# Fiyat satırında mağaza adını ayıklamak için bilinen satıcı isimleri.
# Liste eşleşme amacıyla kullanılır; kaynaklar yine sadece Akakçe + UcuzaCebin'dir.
MERCHANT_ALIASES = {
    "Trendyol": ["trendyol"],
    "Hepsiburada": ["hepsiburada"],
    "Amazon Türkiye": ["amazon.com.tr", "amazon türkiye", "amazon turkiye", "amazon"],
    "N11": ["n11"],
    "İtopya": ["itopya"],
    "Vatan": ["vatan bilgisayar", "vatan"],
    "Teknosa": ["teknosa"],
    "MediaMarkt": ["mediamarkt", "media markt"],
    "idefix": ["idefix"],
    "Wraith Esports": ["wraith esports", "wraithesports", "wraith"],
    "Meyer Gaming": ["meyer gaming", "meyergaming", "meyer"],
    "Neeko": ["neeko"],
    "Sinerji": ["sinerji"],
    "İncehesap": ["incehesap"],
    "Gaming.Gen.TR": ["gaming.gen.tr", "gaming gen tr", "gaminggen"],
    "Inventus": ["inventus"],
    "GameGaraj": ["gamegaraj", "game garaj"],
    "Gençer Gaming": ["gençer gaming", "gencer gaming", "gencergaming"],
    "Tebilon": ["tebilon"],
    "QP Bilişim": ["qp bilişim", "qp bilisim", "qpbilisim"],
    "PttAVM": ["pttavm", "ptt avm"],
    "Sahibinden": ["sahibinden"],
}


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
    """Başlığın ürün kimliğini taşıyan ayırt edici kelimeleri çıkarır.

    Ölçü, watt, çözünürlük gibi teknik değerleri burada kimlik kelimesi saymayız;
    onlar specs() ile ayrıca ve daha güvenli biçimde karşılaştırılır.
    """
    raw_words = words(text)
    output = set()
    for w in raw_words:
        if w in GENERIC_PRODUCT_WORDS or len(w) < 2:
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?", w):
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?:tb|gb|mb|hz|khz|mhz|ghz|mm|cm|inch|inc|w|mah)", w):
            continue
        if w in {"3d", "2d", "4k", "8k"}:
            continue
        output.add(w)
    return output


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
    for number, word in re.findall(r"(?<![\d.])(\d{2,4})\s+([a-z]{2,12})\b", raw):
        if (
            word not in {"gb", "tb", "hz", "mm", "cm", "mah"}
            and word not in GENERIC_PRODUCT_WORDS
            and word not in STOP_WORDS
        ):
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


def generation_markers(text: str) -> set[str]:
    """Superlight 2 gibi tek haneli nesil işaretlerini bağlamıyla yakalar."""
    toks = norm(text).split()
    out = set()
    for i in range(1, len(toks)):
        if re.fullmatch(r"[2-9]", toks[i]):
            prev = toks[i - 1]
            if (
                prev not in GENERIC_PRODUCT_WORDS
                and prev not in STOP_WORDS
                and re.search(r"[a-z]", prev)
            ):
                out.add(f"{prev} {toks[i]}")
    return out


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


def manufacturer_codes(text: str) -> list[str]:
    """Başlıktaki üretici/model kodlarını çıkarır.

    Logitech 910-006631 gibi yalnız rakam+tire içeren gerçek parça kodları eski
    model_tokens() tarafından kaçırılıyordu. Bu da Akakçe aramasını gereksiz
    kelimelere bırakıyordu.
    """
    raw = norm(text)
    found: list[str] = []

    # Harf+rakam içeren kodlar: 25g4sxu, rb2140, kf560c30bbeak2-32 ...
    for token in re.findall(r"\b[a-z0-9][a-z0-9._+-]{3,}\b", raw):
        if re.search(r"[a-z]", token) and re.search(r"\d", token):
            if token not in found:
                found.append(token)

    # Logitech 910-006631 / benzeri üretici parça kodları.
    for token in re.findall(r"(?<!\d)(\d{3,6}-\d{3,8})(?!\d)", raw):
        if token not in found:
            found.append(token)

    return found


def build_search_queries(title: str, brand: Optional[str], model: Optional[str]) -> list[str]:
    """Tek uzun sorgu yerine birkaç kısa ve güvenli sorgu üretir."""
    brand_s = re.sub(r"\s+", " ", str(brand or "")).strip()
    queries: list[str] = []

    def add(q: str):
        q = re.sub(r"\s+", " ", str(q or "")).strip()[:140]
        if q and norm(q) not in {norm(x) for x in queries}:
            queries.append(q)

    model_n = usable_model(model)
    if model_n:
        add(f"{brand_s} {model}".strip())
        add(str(model))

    codes = manufacturer_codes(title)
    for code in codes[:3]:
        add(f"{brand_s} {code}".strip())
        add(code)

    # Ürün ailesini üçüncü fallback olarak kullan.
    identity = []
    for w in norm(title).split():
        if w in identity_words(title) and w not in identity:
            identity.append(w)
        if len(identity) >= 4:
            break
    if identity:
        add(" ".join(([brand_s] if brand_s else []) + identity))

    add(norm(title)[:120])
    return queries[:6]


def build_search_query(title: str, brand: Optional[str], model: Optional[str]) -> str:
    # Geriye uyumluluk için ilk/en güçlü sorguyu döndürür.
    queries = build_search_queries(title, brand, model)
    return queries[0] if queries else norm(title)[:140]


# =========================================================
# STRICT MATCHING
# =========================================================



ACCESSORY_PHRASES = {
    "ekran koruyucu", "screen protector", "koruyucu film", "koruyucu cam",
    "kilif", "kılıf", "case", "tasima cantasi", "taşıma çantası",
    "stand", "ayak", "bracket", "duvar aparati", "duvar aparatı",
    "yedek parca", "yedek parça", "replacement", "cover",
}

PRODUCT_KIND_MARKERS = {
    "monitor": {"monitor", "monitör"},
    "cpu": {"islemci", "işlemci", "processor", "cpu"},
    "gpu": {"ekran karti", "ekran kartı", "graphics card", "gpu", "geforce", "radeon"},
    "ram": {"ram", "bellek", "memory", "ddr4", "ddr5"},
    "ssd": {"ssd", "nvme", "m.2"},
    "cooler": {"sivi sogutma", "sıvı soğutma", "sivi sogutucu", "sıvı soğutucu", "aio", "liquid cooler"},
}


def _contains_phrase(text: str, phrases: set[str]) -> bool:
    n = norm(text)
    return any(norm(p) in n for p in phrases)


def product_kind(text: str) -> Optional[str]:
    n = norm(text)
    # Aksesuarı önce yakala; "AOC 25G4SXU monitör ekran koruyucu" gibi başlıklar
    # model kodu aynı olsa bile ana ürün değildir.
    if _contains_phrase(n, ACCESSORY_PHRASES):
        return "accessory"
    for kind, markers in PRODUCT_KIND_MARKERS.items():
        if any(norm(marker) in n for marker in markers):
            return kind
    return None


def product_kind_conflict(source_title: str, candidate_title: str) -> tuple[bool, str]:
    src_kind = product_kind(source_title)
    cand_kind = product_kind(candidate_title)

    if cand_kind == "accessory" and src_kind != "accessory":
        return True, "ana ürün yerine aksesuar"

    if src_kind and cand_kind and src_kind != cand_kind:
        return True, f"ürün türü çelişiyor ({src_kind}/{cand_kind})"

    return False, ""


def hard_conflict(
    source_title: str,
    candidate_title: str,
    source_model: Optional[str] = None,
    source_variant_text: Optional[str] = None,
    candidate_variant_text: Optional[str] = None,
) -> tuple[bool, str]:
    src = norm(source_title)
    cand = norm(candidate_title)

    kind_bad, kind_reason = product_kind_conflict(source_title, candidate_title)
    if kind_bad:
        return True, kind_reason

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

    # "Superlight 2" / "Superlight" gibi nesil farklarını atlama.
    src_generations = generation_markers(src)
    cand_generations = generation_markers(cand)
    if src_generations != cand_generations:
        generation_contexts = {
            marker.rsplit(" ", 1)[0]
            for marker in (src_generations | cand_generations)
        }
        if generation_contexts & shared_identity:
            return True, (
                "ürün nesli çelişiyor: "
                f"kaynak={sorted(src_generations) or '-'}, "
                f"aday={sorted(cand_generations) or '-'}"
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
    """İki ürün kaydını yön bağımsız biçimde eşleştirir.

    Bu fonksiyon özellikle sepette iki farklı mağazadan eklenmiş aynı ürün için
    kullanılır. Bir başlığın uzun, diğerinin kısa olması skoru tek yönlü bozmaz.
    """
    # Çelişki kontrolünü iki yönde de çalıştır. Böylece model/varyant bilgisi
    # yalnızca taraflardan birinde dolu olsa bile güvenli davranırız.
    for args in (
        (left_title, right_title, left_model, left_variant_text, right_variant_text),
        (right_title, left_title, right_model, right_variant_text, left_variant_text),
    ):
        conflict, reason = hard_conflict(*args)
        if conflict:
            return 0.0, reason

    li = identity_words(left_title)
    ri = identity_words(right_title)
    shared = li & ri
    if not li or not ri or not shared:
        return 0.0, "ortak ürün kimliği yok"

    dice = (2 * len(shared)) / max(1, len(li) + len(ri))
    shorter_coverage = len(shared) / max(1, min(len(li), len(ri)))

    # Marka açıkça iki tarafta da varsa farklı olması sert ret.
    lb = re.sub(r"[^a-z0-9]", "", norm(left_brand or ""))
    rb = re.sub(r"[^a-z0-9]", "", norm(right_brand or ""))
    if lb and rb and lb != rb:
        return 0.0, "marka çelişiyor"

    # Sürüm belirteçleri (SE/Pro/Max vb.) bir tarafta mevcutsa diğer tarafta da
    # aynı olmalı. hard_conflict bunu çoğu durumda yakalar; burada da güveni artırır.
    le = edition_tokens(left_title)
    re_ = edition_tokens(right_title)
    edition_match = bool(le or re_) and le == re_

    # Ortak üretici model kodu en güçlü kanıtlardan biridir.
    lm = model_tokens(left_title)
    rm = model_tokens(right_title)
    shared_models = lm & rm

    # Teknik değerler: iki tarafta da açıkça yazıyorsa eşleşmesini ödüllendir.
    ls = specs(left_title + " " + str(left_variant_text or ""))
    rs = specs(right_title + " " + str(right_variant_text or ""))
    comparable = 0
    matched = 0
    for unit, values in ls.items():
        other = rs.get(unit)
        if other:
            comparable += 1
            if not values.isdisjoint(other):
                matched += 1

    score = 0.0
    score += 0.46 * dice
    score += 0.34 * shorter_coverage

    if len(shared) >= 3:
        score += 0.12
    elif len(shared) == 2:
        score += 0.04

    if edition_match:
        score += 0.08
    if shared_models:
        score += min(0.20, 0.14 + 0.03 * len(shared_models))
        # Birebir güçlü üretici/model kodu (örn. 25G4SXU, DUAL-RTX5070-O12G)
        # aynıysa ve yukarıdaki sert çelişki kontrollerinden geçtiyse bu, başlık
        # uzunluk farkından çok daha güçlü kanıttır.
        if any(len(x) >= 5 and re.search(r"[a-z]", x) and re.search(r"\d", x) for x in shared_models):
            score = max(score, 0.92)
    if comparable:
        score += 0.10 * (matched / comparable)

    # Kısa başlık uzun başlığın çekirdeğini tamamen taşıyorsa bu çok güçlü bir
    # sinyaldir. Örn. "TRYX PANORAMA SE AIO Sıvı Soğutma" ile uzun Amazon başlığı.
    if len(shared) >= 3 and shorter_coverage >= 0.99:
        score = max(score, 0.90)

    score = max(0.0, min(0.99, score))
    return score, (
        f"simetrik: ortak={sorted(shared)}, dice={dice:.2f}, "
        f"kisa_kapsama={shorter_coverage:.2f}, teknik={matched}/{comparable}"
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
# AKAKÇE + UCUZACEBİN KEŞİF MOTORU
# =========================================================
# =========================================================
# AKAKÇE + UCUZACEBİN KAYNAK ADAPTERLARI
# =========================================================


def _aggregator_search_urls(source: str, query: str) -> list[str]:
    encoded = quote(query)
    return [template.format(q=encoded) for template in AGGREGATOR_SEARCH.get(source, [])]


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_internal_url(source: str, url: str) -> bool:
    return _host(url) in AGGREGATOR_HOSTS.get(source, set())


def _is_search_or_nav_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    if not path or path == "/":
        return True
    blocked = (
        "/arama", "/search", "/kategori", "/category", "/marka", "/brand",
        "/kampanya", "/blog", "/login", "/giris", "/uye", "/hesap",
        "/yardim", "/hakkimizda", "/iletisim",
    )
    return any(x in path for x in blocked)


def _looks_like_product_page(source: str, url: str) -> bool:
    if not _is_internal_url(source, url) or _is_search_or_nav_url(url):
        return False
    path = (urlparse(url).path or "").lower()
    if source == "Akakçe":
        return "fiyati" in path and path.endswith(".html")
    if source == "UcuzaCebin":
        return "/urun/" in path
    return False


def _text_of(element) -> str:
    try:
        text = element.get_text(" ", strip=True)
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text or "").strip()


def _candidate_title(anchor) -> str:
    title = _text_of(anchor)
    if len(title) >= 4:
        return title[:500]
    img = anchor.find("img")
    if img:
        alt = str(img.get("alt") or img.get("title") or "").strip()
        if len(alt) >= 4:
            return re.sub(r"\s+", " ", alt)[:500]
    return ""


async def _fetch_html(url: str) -> str:
    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            timeout=SEARCH_HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            if response.status_code == 200 and len(response.text) > 1500:
                return response.text
    except Exception as e:
        print("AGG HTTP ERROR:", url, repr(e), flush=True)

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
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_GOTO_TIMEOUT)
        except Exception:
            pass
        await page.wait_for_timeout(1300)
        return await page.content()
    except Exception as e:
        print("AGG BROWSER ERROR:", url, repr(e), flush=True)
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


def _extract_product_candidates(
    source: str,
    search_url: str,
    html: str,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    output: dict[str, Candidate] = {}

    for anchor in soup.find_all("a", href=True)[:5000]:
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(search_url, href).split("#", 1)[0]
        if not _looks_like_product_page(source, url):
            continue

        title = _candidate_title(anchor)
        if len(title) < 4:
            parent = anchor
            for _ in range(4):
                parent = getattr(parent, "parent", None)
                if parent is None:
                    break
                text = _text_of(parent)
                if 8 <= len(text) <= 900:
                    title = text
                    break
        if len(title) < 4:
            continue

        conflict, why = product_kind_conflict(source_title, title)
        if conflict:
            continue

        score, _ = deterministic_score(
            source_title, title, source_brand, source_model,
            source_variant_text, None,
        )

        # Birebir üretici kodu, arama sonuçlarında başlık uzunluğu farkından daha
        # güçlü kanıttır. Özellikle 910-006631 gibi rakamsal parça kodlarını kaçırma.
        source_codes = manufacturer_codes(source_title + " " + str(source_model or ""))
        candidate_compact = re.sub(r"[^a-z0-9]", "", norm(title))
        exact_code_hit = any(
            len(re.sub(r"[^a-z0-9]", "", code)) >= 5
            and re.sub(r"[^a-z0-9]", "", code) in candidate_compact
            for code in source_codes
        )
        if exact_code_hit:
            score = max(score, 0.94)

        if score < 0.42:
            continue

        c = Candidate(source, title[:500], url, score)
        old = output.get(url)
        if old is None or c.pre_score > old.pre_score:
            output[url] = c

    return sorted(output.values(), key=lambda x: x.pre_score, reverse=True)[:MAX_PRODUCT_PAGES_PER_SOURCE]


def _page_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        text = _text_of(h1)
        if len(text) >= 4:
            return text[:500]
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return re.sub(r"\s+", " ", str(og.get("content"))).strip()[:500]
    title = soup.find("title")
    if title:
        return _text_of(title)[:500]
    return ""


def _turkish_price_to_float(raw: str) -> Optional[float]:
    if not raw:
        return None
    s = raw.strip().replace("\xa0", " ")
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        right = s.split(",")[-1]
        if len(right) in (1, 2):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
            s = "".join(parts)
    try:
        value = float(s)
    except ValueError:
        return None
    return value if 0 < value <= 50_000_000 else None


def _price_candidates(text: str) -> list[float]:
    patterns = [
        r"(?:₺|TL)\s*([0-9][0-9.\s]*(?:,[0-9]{1,2})?)",
        r"([0-9][0-9.\s]*(?:,[0-9]{1,2})?)\s*(?:₺|TL)",
    ]
    out: list[float] = []
    for pattern in patterns:
        for raw in re.findall(pattern, str(text or ""), flags=re.I):
            value = _turkish_price_to_float(raw)
            if value is not None and value not in out:
                out.append(value)
    return out


def _decode_outbound_url(base_url: str, href: str) -> str:
    """Teklif linkindeki açık hedef URL'yi çözer.

    Çözülemeyen aggregator redirect linkini olduğu gibi bırakır; ancak ürün sayfasına
    fallback YAPMAZ. Böylece karşılaştırma satırı hiçbir zaman kullanıcının eklediği
    kaynak ürün linkine sahte bir teklif linki olarak dönmez.
    """
    from urllib.parse import parse_qs, unquote

    url = urljoin(base_url, href).replace("&amp;", "&")
    parsed = urlparse(url)
    try:
        qs = parse_qs(parsed.query)
        for key in (
            "url", "u", "target", "to", "redirect", "redirect_url",
            "link", "dest", "destination", "r",
        ):
            for value in qs.get(key, []):
                decoded = unquote(str(value)).strip()
                if decoded.startswith(("http://", "https://")):
                    return decoded
    except Exception:
        pass
    return url


def _same_url(a: str, b: str) -> bool:
    def clean(value: str) -> str:
        try:
            parsed = urlparse(str(value or ""))
            host = (parsed.hostname or "").lower().removeprefix("www.")
            path = (parsed.path or "/").rstrip("/") or "/"
            return host + path
        except Exception:
            return str(value or "").rstrip("/")
    return bool(a and b and clean(a) == clean(b))


def _usable_offer_link(source: str, product_url: str, target: str) -> bool:
    """Bir linkin gerçekten teklif aksiyonu olup olmadığını kontrol eder.

    En kritik kural: aggregator ürün sayfasının kendisi teklif linki sayılamaz.
    """
    if not target or _same_url(target, product_url):
        return False

    target_host = _host(target)
    if not target_host:
        return False

    # Gerçek mağaza alan adıysa güvenli biçimde kullanılabilir.
    if target_host not in AGGREGATOR_HOSTS.get(source, set()):
        return True

    # Aggregator içindeki bir yönlendirme linkiyse ürün sayfasından farklı olmalı ve
    # tipik click/redirect işaretlerinden en az birini taşımalı.
    parsed = urlparse(target)
    marker_text = norm((parsed.path or "") + " " + (parsed.query or ""))
    redirect_markers = (
        "redirect", "yonlendir", "git", "click", "out", "target",
        "merchant", "magaza", "store", "offer", "url=", "u=",
    )
    return any(marker in marker_text for marker in redirect_markers)


def _merchant_from_domain(url: str) -> Optional[str]:
    host = _host(url).replace("www.", "")
    if not host:
        return None
    for canonical, aliases in MERCHANT_ALIASES.items():
        for alias in aliases:
            compact = norm(alias).replace(" ", "")
            if compact and compact in norm(host).replace(" ", ""):
                return canonical
    all_agg = {h for hosts in AGGREGATOR_HOSTS.values() for h in hosts}
    if host not in all_agg:
        stem = host.split(".")[0]
        return stem[:50].title() if stem else None
    return None


def _merchant_from_text(text: str) -> Optional[str]:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    n = norm(raw)
    for canonical, aliases in MERCHANT_ALIASES.items():
        if any(norm(alias) in n for alias in aliases):
            return canonical

    # UcuzaCebin mağaza hücreleri çoğunlukla alan adıyla başlar.
    domain = re.search(r"\b([a-z0-9.-]+\.(?:com\.tr|gen\.tr|net\.tr|com|net|de|tr))\b", n)
    if domain:
        return domain.group(1)

    # Akakçe bazı kartlarda açıkça "Satıcı: X" yazar.
    seller = re.search(r"satici\s*:\s*([^|•]{2,60})", n)
    if seller:
        value = seller.group(1).strip(" -/")
        value = re.split(r"\b(?:yorum|stokta|kargoda|yetkili|ucretsiz)\b", value)[0].strip()
        if value:
            return value[:50].title()
    return None


def _bad_offer_context(text: str) -> bool:
    n = norm(text)
    bad = (
        "outlet", "ikinci el", "2. el", "olu pixel", "olu piksel",
        "yenilenmis", "refurbished", "teshir", "acik kutu", "open box",
    )
    return any(x in n for x in bad)


def _nearest_row(anchor, max_chars: int = 1800) -> str:
    best = _text_of(anchor)
    node = anchor
    for _ in range(7):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = _text_of(node)
        if not text or len(text) > max_chars:
            continue
        if _price_candidates(text):
            best = text
            # "Satıcıya Git" veya "Git" ile fiyatın aynı küçük kapsayıcıda olması
            # teklif satırı için güçlü sinyaldir.
            nt = norm(text)
            if "saticiya git" in nt or re.search(r"\bgit\b", nt):
                break
    return best


def _first_price_before_action(context: str) -> Optional[float]:
    n = norm(context)
    cut = len(context)
    # Normalize edilmiş index birebir aynı olmayabilir; pratikte aksiyon fiyatın
    # arkasında olduğu için önce tüm fiyatları al, taksit varsa ana fiyatı seç.
    prices = _price_candidates(context)
    if not prices:
        return None
    if "taksit" in n and len(prices) > 1:
        # Taksit bedeli ana fiyattan küçük olur; ana teklif genellikle en büyük ilk
        # birkaç değerden biridir. Aşırı eski/liste fiyatı riskine karşı ilk fiyatı
        # tercih ediyoruz; sadece ilk fiyat bariz küçükse ikinciyi al.
        if prices[0] < 500 and len(prices) > 1:
            return prices[1]
    return prices[0]


def _find_heading(soup: BeautifulSoup, pattern: str):
    rx = re.compile(pattern, re.I)
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "div", "span"]):
        text = _text_of(tag)
        if text and len(text) <= 120 and rx.search(norm(text)):
            return tag
    return None


def _section_after_heading(heading, stop_patterns: tuple[str, ...], max_nodes: int = 120):
    """Başlığın ardından gelen DOM parçalarını bir sonraki büyük bölüm başlığına kadar döndür."""
    if heading is None:
        return []
    nodes = []
    # Önce heading'in aynı parent içindeki sonraki kardeşlerini gez.
    parent = heading.parent
    if parent is None:
        return nodes
    started = False
    for child in parent.children:
        if child is heading or (hasattr(child, "find") and child.find(lambda t: t is heading)):
            started = True
            continue
        if not started or not hasattr(child, "get_text"):
            continue
        text = _text_of(child)
        nt = norm(text)
        if any(norm(p) in nt[:140] for p in stop_patterns):
            break
        nodes.append(child)
        if len(nodes) >= max_nodes:
            break
    # Yapı daha iç içeyse heading'in sonraki elementlerini fallback olarak kullan.
    if not nodes:
        for node in heading.find_all_next(limit=max_nodes):
            if node is heading or not getattr(node, "name", None):
                continue
            text = _text_of(node)
            nt = norm(text)
            if any(norm(p) in nt[:140] for p in stop_patterns):
                break
            nodes.append(node)
    return nodes


def _extract_akakce_offers(product_url: str, html: str, verified_title: str, product_score: float) -> list[MatchResult]:
    soup = BeautifulSoup(html, "html.parser")
    heading = _find_heading(soup, r"tum fiyatlar")
    nodes = _section_after_heading(
        heading,
        ("outlet ve ikinci el", "fiyat analizi", "yorum", "ozellikleri"),
        max_nodes=180,
    )
    root_nodes = nodes or [soup]
    offers: list[MatchResult] = []
    seen: set[tuple[str, int, str]] = set()

    for root in root_nodes:
        for anchor in root.find_all("a", href=True):
            atext = norm(_text_of(anchor))
            if "saticiya git" not in atext:
                continue
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue
            context = _nearest_row(anchor)
            if _bad_offer_context(context):
                continue
            price = _first_price_before_action(context)
            if price is None:
                continue
            target = _decode_outbound_url(product_url, href)
            if not _usable_offer_link("Akakçe", product_url, target):
                continue
            merchant = _merchant_from_domain(target) or _merchant_from_text(context) or "X Mağaza"
            if not merchant:
                continue
            key = (norm(merchant), int(round(price * 100)), target.rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            offers.append(MatchResult(
                store=merchant,
                title=verified_title,
                price=price,
                url=target,
                image_url=None,
                score=min(0.99, max(0.86, product_score)),
                reason="Akakçe Tüm Fiyatlar bölümünden doğrulandı",
            ))
            if len(offers) >= MAX_OFFERS_PER_SOURCE:
                return offers
    return offers


def _extract_ucuzacebin_offers(product_url: str, html: str, verified_title: str, product_score: float) -> list[MatchResult]:
    soup = BeautifulSoup(html, "html.parser")
    heading = _find_heading(soup, r"fiyat karsilastirma")

    # UcuzaCebin'in ürün sayfasında teklif bölümü tablo semantiğiyle gelir.
    table = None
    if heading is not None:
        table = heading.find_next("table")
    if table is None:
        # Tasarım değişirse sadece Fiyat Karşılaştırma bölümüne yakın tabloları dene.
        table = soup.find("table")
    if table is None:
        return []

    offers: list[MatchResult] = []
    seen: set[tuple[str, int, str]] = set()
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        row_text = _text_of(row)
        if _bad_offer_context(row_text):
            continue
        # Header satırını atla.
        if "magaza" in norm(row_text) and "fiyat" in norm(row_text) and not _price_candidates(row_text):
            continue
        merchant = _merchant_from_text(_text_of(cells[0]))
        if not merchant:
            merchant = _merchant_from_text(row_text)
        price = None
        # Önce fiyat hücresini kullan; sonra satıra fallback.
        for cell in cells[1:3]:
            vals = _price_candidates(_text_of(cell))
            if vals:
                price = vals[0]
                break
        if price is None:
            vals = _price_candidates(row_text)
            price = vals[0] if vals else None
        if not merchant or price is None:
            continue
        link = None
        for a in row.find_all("a", href=True):
            if norm(_text_of(a)) == "git" or "git" in norm(_text_of(a)):
                link = _decode_outbound_url(product_url, str(a.get("href") or ""))
                break
        if not link:
            # Satırdaki ilk link ancak gerçek teklif/yönlendirme linkiyse kullanılabilir.
            for a in row.find_all("a", href=True):
                candidate_link = _decode_outbound_url(product_url, str(a.get("href") or ""))
                if _usable_offer_link("UcuzaCebin", product_url, candidate_link):
                    link = candidate_link
                    break
        if not link or not _usable_offer_link("UcuzaCebin", product_url, link):
            # ÖNEMLİ: product_url'ye fallback yok. Ürün sayfasını mağaza teklifi gibi
            # göstermek kullanıcının gördüğü yanlış link probleminin ana sebebiydi.
            continue
        merchant = merchant or _merchant_from_domain(link) or "X Mağaza"
        key = (norm(merchant), int(round(price * 100)), link.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        offers.append(MatchResult(
            store=merchant,
            title=verified_title,
            price=price,
            url=link,
            image_url=None,
            score=min(0.99, max(0.88, product_score)),
            reason="UcuzaCebin Fiyat Karşılaştırma tablosundan doğrulandı",
        ))
        if len(offers) >= MAX_OFFERS_PER_SOURCE:
            break
    return offers


async def _verify_aggregator_product_page(
    source: str,
    candidate: Candidate,
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> tuple[Optional[str], float, str, str]:
    html = await _fetch_html(candidate.url)
    if not html:
        return None, 0.0, "sayfa alınamadı", ""
    soup = BeautifulSoup(html, "html.parser")
    page_title = _page_title(soup) or candidate.title

    kind_bad, kind_reason = product_kind_conflict(source_title, page_title)
    if kind_bad:
        print("AGG PRODUCT REJECT:", source, page_title, kind_reason, flush=True)
        return None, 0.0, kind_reason, html

    score, reason = deterministic_score(
        source_title, page_title, source_brand, source_model,
        source_variant_text, None,
    )
    if score >= MIN_PRODUCT_PAGE_SCORE:
        return page_title, score, reason, html
    if score >= 0.68:
        same, confidence, ai_reason = await ai_verify_match(
            source_title, source_brand, source_model, source_variant_text,
            page_title, None, None, None,
        )
        if same is True and confidence >= 0.98:
            return page_title, min(0.99, max(score, confidence)), ai_reason, html
    print("AGG PRODUCT REJECT:", source, page_title, "score=", round(score, 3), reason, flush=True)
    return None, score, reason, html


async def search_aggregator(
    source: str,
    queries: list[str],
    source_title: str,
    source_brand: Optional[str],
    source_model: Optional[str],
    source_variant_text: Optional[str],
) -> list[MatchResult]:
    product_candidates: dict[str, Candidate] = {}

    for query in queries:
        for search_url in _aggregator_search_urls(source, query):
            print("AGG SEARCH:", source, "query=", query, search_url, flush=True)
            html = await _fetch_html(search_url)
            if not html:
                continue
            for candidate in _extract_product_candidates(
                source, search_url, html, source_title, source_brand,
                source_model, source_variant_text,
            ):
                old = product_candidates.get(candidate.url)
                if old is None or candidate.pre_score > old.pre_score:
                    product_candidates[candidate.url] = candidate

        # Güçlü bir aday bulduysak gereksiz yere bütün fallback sorguları dolaşma.
        if any(c.pre_score >= 0.90 for c in product_candidates.values()):
            break

    if not product_candidates:
        print("AGG NO PRODUCT:", source, "queries=", queries, flush=True)
        return []

    for candidate in sorted(product_candidates.values(), key=lambda x: x.pre_score, reverse=True):
        page_title, score, reason, html = await _verify_aggregator_product_page(
            source, candidate, source_title, source_brand,
            source_model, source_variant_text,
        )
        if page_title is None:
            continue
        if source == "Akakçe":
            offers = _extract_akakce_offers(candidate.url, html, page_title, score)
        else:
            offers = _extract_ucuzacebin_offers(candidate.url, html, page_title, score)
        if offers:
            print("AGG OFFERS:", source, len(offers), "product=", page_title, flush=True)
            return offers
        print("AGG PRODUCT HAS NO OFFERS:", source, candidate.url, flush=True)
    return []


def _dedupe_aggregator_results(results: list[MatchResult]) -> list[MatchResult]:
    # Aynı mağaza aynı fiyatsa tek satır. Aynı mağazada iki kaynak farklı fiyat
    # söylüyorsa ikisini de tutuyoruz; yanlış bir "en ucuz" uydurmuyoruz.
    unique: dict[tuple[str, int], MatchResult] = {}
    for result in results:
        if result.price is None:
            continue
        key = (norm(result.store), int(round(result.price * 100)))
        old = unique.get(key)
        if old is None or result.score > old.score:
            unique[key] = result
    return list(unique.values())


async def compare_product(
    title: str,
    source_store: str,
    source_url: str,
    source_price: Optional[float],
    brand: Optional[str] = None,
    model: Optional[str] = None,
    variant_text: Optional[str] = None,
) -> list[MatchResult]:
    queries = build_search_queries(title, brand, model)
    print("COMPARE QUERIES:", queries, flush=True)
    print("COMPARE SOURCES: Akakçe + UcuzaCebin", flush=True)

    async def one_source(source: str) -> list[MatchResult]:
        try:
            return await search_aggregator(
                source, queries, title, brand, model, variant_text,
            )
        except Exception as e:
            print("AGG SOURCE ERROR:", source, repr(e), flush=True)
            return []

    batches = await asyncio.gather(one_source("Akakçe"), one_source("UcuzaCebin"))
    results = _dedupe_aggregator_results([x for batch in batches for x in batch])

    # Kaynak ürün karşılaştırma tekliflerine EKLENMEZ. Ana kart zaten kaynak
    # mağazayı ve fiyatını gösteriyor. Buraya eklemek, dış kaynaklar sonuç
    # bulamadığında kullanıcının kendi eklediği linki "karşılaştırma sonucu" gibi
    # göstermesine neden oluyordu. Sepetteki başka eş ürünleri services.py ekler.

    # Son güvenlik ağı: hiçbir aggregator sonucu kaynak ürün URL'sine eşit olamaz.
    results = [r for r in results if not _same_url(r.url, source_url)]

    unique: dict[tuple[str, str, int], MatchResult] = {}
    for result in results:
        price_key = -1 if result.price is None else int(round(result.price * 100))
        key = (norm(result.store), result.url.rstrip("/"), price_key)
        old = unique.get(key)
        if old is None or result.score > old.score:
            unique[key] = result

    return sorted(
        unique.values(),
        key=lambda x: (
            x.price is None,
            x.price if x.price is not None else 10**18,
            -x.score,
        ),
    )
