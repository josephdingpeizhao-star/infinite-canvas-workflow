"""Shared disclosure rules for production failure text."""

import re


_UNSAFE_FAILURE_DETAIL_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|[\s（：；])/[^/]|https?://|ftp://|www\.|"
    r"bearer|token|api[_ -]?key|secret|令牌|密钥|sk-[A-Za-z0-9])",
    flags=re.IGNORECASE,
)


def is_disclosable(text: str) -> bool:
    """Return whether untrusted failure text is safe to show to the user."""

    return (
        type(text) is str
        and bool(text)
        and len(text) <= 200
        and "\n" not in text
        and "\r" not in text
        and "/" not in text
        and "\\" not in text
        and _UNSAFE_FAILURE_DETAIL_PATTERN.search(text) is None
    )
