"""Error classification for yt-dlp and playback failures.

Every message is one sentence: what happened, then what to do next. The same
strings appear in the UI status line and in ``tide doctor``; none of them are
ever printed as raw tracebacks.
"""

from __future__ import annotations

import re

# (pattern, message) - first match wins; patterns are checked in order.
_RULES: tuple[tuple[str, str], ...] = (
    # access controls we never bypass
    (
        r"drm|widevine",
        "This content is protected by DRM and cannot be played or downloaded by Tide.",
    ),
    (
        r"sign in to confirm (your age|you.?re not a bot)|confirm your age",
        "YouTube wants an age check. Set cookies_from_browser or point cookiefile at a cookies.txt in the config.",
    ),
    (
        r"members.?only|join this channel|become a member",
        "That video is members-only. Tide does not bypass membership requirements.",
    ),
    (
        r"premium|youtube red",
        "That video needs a premium account. Tide does not bypass paid access.",
    ),
    (
        r"private video|this is a private video|video is private",
        "The video is private. Sign in via cookies_from_browser if you have access.",
    ),
    (
        r"not made this video available|not available in your country|geo.?blocked|geo restricted",
        "The uploader blocked this video in your region. Tide cannot change that.",
    ),
    (
        r"video unavailable|has been removed|account (has been )?terminated|copyright strike",
        "That video is unavailable (removed or terminated). Pick another result.",
    ),
    # network problems
    (
        r"http error 429|too many requests|rate.?limit",
        "YouTube is rate-limiting this connection. Wait a minute, or set proxy in the config.",
    ),
    (
        r"urlopen error|connection refused|connection reset|getaddrinfo|name resolution|"
        r"network is unreachable|timed out|timeout|temporary failure in name resolution|"
        r"unable to download webpage|remote end closed|no route to host",
        "yt-dlp could not reach YouTube. Check your connection, or set proxy in the config.",
    ),
    (
        r"ssl|certificate",
        "The secure connection to the server failed. Check your system clock and certificates.",
    ),
    # extraction / version problems
    (
        r"unsupported url|no video could be found|not a valid url",
        "That URL is not supported. Check the link, or search by words instead.",
    ),
    (
        r"unable to extract|unable to fetch|is not a valid|failed to resolve|"
        r"returned none:|received 403|http error 403",
        "yt-dlp could not parse this page - it may need an update: pip install -U yt-dlp",
    ),
    (
        r"live event|is live|premieres in|stream is offline",
        "Live streams and premieres are not supported for playback yet.",
    ),
    (
        r"copyright|rights managed|standard license",
        "This content is not downloadable under its license.",
    ),
)

_COMPILED = tuple((re.compile(pattern, re.IGNORECASE | re.VERBOSE), message) for pattern, message in _RULES)

_UPDATE_RE = re.compile(
    r"unable to extract|use extractor|failed to get http response code|"
    r"returned none:|is not a valid url",
    re.IGNORECASE,
)


def classify_yt_dlp_error(text: str) -> str | None:
    """Map raw yt-dlp output to a user-facing sentence; None when unknown."""
    if not text:
        return None
    # keep the search space small: error lines are short
    sample = text[-4000:]
    for pattern, message in _COMPILED:
        if pattern.search(sample):
            return message
    return None


def needs_yt_dlp_update(text: str) -> bool:
    """True when the failure class suggests an outdated yt-dlp."""
    return bool(text and _UPDATE_RE.search(text))


def first_error_line(text: str) -> str:
    """A short technical line for the log panel (never shown as the headline)."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""
