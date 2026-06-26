"""Policy / banned-term guard — ported from multicombine's content/policy_filter.

TikTok shadow-filters financial/crypto/"easy money" comments hardest. This module
flags those high-risk terms so the user is warned before a run (it does NOT silently
rewrite their text — meaning/CTA stays the user's call). `sanitize_content_text` is
available if softer wording is wanted explicitly.
"""
import hashlib
import re
from typing import List, Optional

BANNED_TERMS = [
    # фінанси / гроші
    "гроші", "грошей", "заробіток", "заробити", "заробляти",
    "дохід", "доходу", "прибуток", "прибутку",
    "money", "cash", "dollars", "income", "profit",
    # крипто
    "крипта", "криптовалюта", "біткоїн", "біткойн", "bitcoin",
    "crypto", "trading", "трейдинг",
    # схеми / p2p
    "схема", "система заробітку", "легкі гроші", "пасивний дохід", "p2p арбітраж",
    # інвестиції / торгівля
    "інвестиції", "інвестувати", "купити", "продати", "купівля",
    "торгівля", "forex", "ставки", "казино", "спред", "спреди",
    # розкіш / гроші
    "багатство", "мільйон", "мільярд", "dollar", "доллар", "wealth",
    # соцмережі у текстах (TikTok знижує охоплення за згадку зовнішніх платформ)
    "telegram", "телеграм", "тг", "канал",
]

SAFE_REPLACEMENTS = {
    "гроші": "можливості", "грошей": "результатів", "заробіток": "розвиток",
    "заробити": "досягти", "заробляти": "розвиватися", "дохід": "результат",
    "доходу": "результату", "прибуток": "досягнення", "прибутку": "досягнень",
    "money": "progress", "cash": "value", "dollars": "goals", "income": "growth",
    "profit": "success", "крипта": "технології", "криптовалюта": "цифрові рішення",
    "біткоїн": "цифровий актив", "біткойн": "цифровий актив", "bitcoin": "digital asset",
    "crypto": "digital", "trading": "skills", "трейдинг": "аналітика", "схема": "підхід",
    "інвестиції": "вклад у себе", "інвестувати": "розвивати навички", "купити": "обрати",
    "продати": "передати", "торгівля": "практика", "багатство": "успіх", "wealth": "success",
    "мільйон": "велика мета", "telegram": "профілі", "телеграм": "профілі",
}

_REPLACEMENT_PATTERNS = [
    (re.compile(re.escape(src), re.IGNORECASE), tgt)
    for src, tgt in sorted(SAFE_REPLACEMENTS.items(), key=lambda i: len(i[0]), reverse=True)
]

# Whole-word-ish match (word boundaries don't work for Cyrillic in `re`, so we
# match the term as a substring but require it not be glued inside a longer word
# via lookaround on letters).
_TERM_PATTERNS = [
    (term, re.compile(r"(?<![а-яА-Яa-zA-ZіїєґІЇЄҐ])" + re.escape(term) +
                      r"(?![а-яА-Яa-zA-ZіїєґІЇЄҐ])", re.IGNORECASE))
    for term in sorted(BANNED_TERMS, key=len, reverse=True)
]

_HASHTAG_PATTERN = re.compile(r"#\w+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def find_risky_terms(text: str) -> List[str]:
    """Return the distinct banned terms present in `text` (case-insensitive)."""
    found = []
    seen = set()
    for term, pat in _TERM_PATTERNS:
        if pat.search(text or "") and term.lower() not in seen:
            seen.add(term.lower())
            found.append(term)
    return found


def risk_warning(text: str) -> Optional[str]:
    """A short user-facing warning if the text has high-risk terms, else None."""
    terms = find_risky_terms(text)
    if not terms:
        return None
    shown = ", ".join(terms[:8])
    return (
        f"⚠️ <b>Ризик тіньового бану.</b> Текст містить слова, які TikTok фільтрує "
        f"найжорсткіше: <code>{shown}</code>.\n"
        f"Порада: переформулюй через інтригу (без прямих «гроші/крипта/канал») — "
        f"охоплення коментарів буде вищим."
    )


def sanitize_content_text(text: Optional[str]) -> str:
    """Soften banned terms via SAFE_REPLACEMENTS and strip hashtags. Optional helper —
    not applied automatically to user comments."""
    sanitized = _WHITESPACE_PATTERN.sub(" ", _HASHTAG_PATTERN.sub("", text or "")).strip()
    for pattern, replacement in _REPLACEMENT_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return _WHITESPACE_PATTERN.sub(" ", sanitized).strip("-•:;, ")


def content_fingerprint(text: str) -> str:
    return hashlib.sha1(sanitize_content_text(text).lower().encode("utf-8")).hexdigest()
