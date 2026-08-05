"""Shared disclosure rules for production failure text."""

import re


_UNSAFE_FAILURE_DETAIL_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|[\s（：；])/[^/]|https?://|ftp://|www\.|"
    r"bearer|token|api[_ -]?key|secret|令牌|密钥|sk-[A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_SENSITIVE_IDENTIFIER_PATTERN = re.compile(
    r"(?:token|secret|bearer|authorization|password|credential|"
    r"api[\s_-]*key|access[\s_-]*key|sk-|令牌|密钥|秘钥|凭据)",
    flags=re.IGNORECASE,
)


def is_sensitive_identifier(name: str) -> bool:
    """Return whether an external identifier is unsafe to show to users."""

    return type(name) is str and _SENSITIVE_IDENTIFIER_PATTERN.search(name) is not None


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
