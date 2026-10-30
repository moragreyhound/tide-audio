"""Text helpers shared by the media layer and the UI.

No Textual or NumPy imports here - everything is deterministic and unit
tested: column-aware truncation, duration and count formatting, fuzzy
matching, and platform-safe filename sanitization.
"""

from __future__ import annotations

import re
import unicodedata

ELLIPSIS = "\u2026"

# ---------------------------------------------------------------------------
# Width-aware text
# ---------------------------------------------------------------------------
_WIDE = {"W", "F"}


def char_width(ch: str) -> int:
    """Terminal columns consumed by one character (0, 1 or 2)."""
    if ch == "\t":
        return 4
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in _WIDE:
        return 2
    # Box drawing / block elements are single width; emoji-free by policy.
    return 1


def display_width(text: str) -> int:
    """Terminal columns consumed by ``text``."""
    return sum(char_width(ch) for ch in text)


def truncate(text: str, width: int, ellipsis: str = ELLIPSIS) -> str:
    """Truncate ``text`` to ``width`` columns, appending an ellipsis.

    Wide (2-column) characters are never split in half. Input is assumed to
    contain no control characters (they are stripped).
    """
    if width <= 0:
        return ""
    clean = _strip_control(text)
    if display_width(clean) <= width:
        return clean
    ell_w = display_width(ellipsis)
    budget = width - ell_w
    if budget <= 0:
        return ellipsis[:width]
    out: list[str] = []
    used = 0
    for ch in clean:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def fit(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    """Pad ``text`` to exactly ``width`` columns (never truncates below 0)."""
    text = truncate(text, width)
    pad = max(0, width - display_width(text))
    if align == "right":
        return fill * pad + text
    if align == "center":
        left = pad // 2
        return fill * left + text + fill * (pad - left)
    return text + fill * pad


def tabular(text: str, width: int, align: str = "right") -> str:
    """Fixed-width column for numbers so they do not jitter while updating."""
    return fit(text, width, align=align)


def _strip_control(text: str) -> str:
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_duration(seconds: float | int | None) -> str:
    """``m:ss`` below one hour, ``h:mm:ss`` above. Negative clamps to 0:00."""
    if seconds is None:
        return "--:--"
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_remaining(seconds: float | int | None) -> str:
    """Remaining time with a leading minus sign; ``--:--`` when unknown."""
    if seconds is None:
        return "--:--"
    return "-" + format_duration(abs(seconds))


def abbrev_count(value: int | float | None) -> str:
    """Abbreviate view counts: ``999``, ``1.2K``, ``12.3M``, ``1.1B``."""
    if value is None:
        return "-"
    n = int(value)
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return _trim(n / 1_000) + "K"
    if n < 1_000_000_000:
        return _trim(n / 1_000_000) + "M"
    return _trim(n / 1_000_000_000) + "B"


def _trim(x: float) -> str:
    text = f"{x:.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_bytes_per_second(value: float | None) -> str:
    if value is None or value <= 0:
        return "- B/s"
    if value < 1024:
        return f"{value:.0f} B/s"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value / (1024 * 1024):.1f} MB/s"


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------
def fuzzy_score(query: str, target: str) -> float | None:
    """Score how well ``query`` matches ``target`` (higher is better).

    ``None`` means no match. Subsequence matches score higher when the letters
    are consecutive, at word starts, and near the beginning of the target.
    Case-insensitive; cheap enough for a few thousand candidates.
    """
    if not query:
        return 1.0
    q = query.lower()
    t = target.lower()
    ti = 0
    score = 0.0
    last_hit = -1
    for ch in q:
        if ch == " ":
            continue
        found = t.find(ch, ti)
        if found < 0:
            return None
        run = found - last_hit if last_hit >= 0 else 0
        at_word = found == 0 or t[found - 1] in " -_./"
        score += 1.0
        if run == 1:
            score += 2.0
        if at_word:
            score += 1.5
        score -= 0.03 * found  # earlier matches are better
        last_hit = found
        ti = found + 1
    # Prefer shorter targets when scores tie.
    return score - 0.01 * len(t)


def fuzzy_best(query: str, candidates: list[str], limit: int | None = None) -> list[int]:
    """Indices of ``candidates`` sorted best-first for ``query``."""
    scored = []
    for idx, text in enumerate(candidates):
        s = fuzzy_score(query, text)
        if s is not None:
            scored.append((-s, idx))
    scored.sort()
    order = [idx for _, idx in scored]
    return order if limit is None else order[:limit]


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------
WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")


def sanitize_filename(name: str, max_len: int = 180) -> str:
    """Make ``name`` a safe single path component on Windows, macOS and Linux.

    * strips control characters and replaces reserved punctuation with ``_``
    * prefixes Windows reserved device names (``CON``, ``COM1``, ...) so they
      survive even with or without an extension
    * strips trailing dots and spaces (Windows rejects them)
    * collapses whitespace runs
    * truncates to ``max_len`` characters, keeping the extension
    """
    text = _CONTROL.sub("", str(name))
    text = _ILLEGAL.sub("_", text)
    text = _WS.sub(" ", text).strip()
    text = text.rstrip(". ")

    if not text:
        return "untitled"

    stem, dot, ext = text.rpartition(".")
    if not dot or not stem:
        stem, ext = text, ""
    else:
        ext = "." + ext

    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem

    if len(stem) + len(ext) > max_len:
        stem = stem[: max(1, max_len - len(ext))].rstrip(". ")

    result = stem + ext
    return result or "untitled"


def sanitize_path_segment(name: str, max_len: int = 120) -> str:
    """Sanitize one directory segment (no separators survive)."""
    return sanitize_filename(name, max_len=max_len)


def slugify(text: str, max_len: int = 40) -> str:
    """Stable identifier for playlist filenames: lowercase ASCII, dashes."""
    lowered = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", lowered).strip("-").lower()
    return (cleaned or "item")[:max_len]
