"""Consequential-control detection and secret redaction.

Risky controls are recognized by whole words in their label. A subtask opts into a
category with `Subtask.allowed_risks`; otherwise such controls are not offered to
the decision model and the runtime refuses to activate them.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

RISK_PHRASES: dict[str, tuple[str, ...]] = {
    "delete": ("delete", "remove", "erase", "trash", "discard", "clear all", "empty trash", "permanently"),
    "send": ("send", "post", "publish", "share", "reply all", "forward", "tweet"),
    "purchase": (
        "buy", "purchase", "pay", "checkout", "check out", "place order", "order now", "subscribe",
        "donate", "confirm payment",
    ),
    "close": ("close", "quit", "exit", "sign out", "log out", "logout", "shut down", "restart", "uninstall"),
}
RISK_CATEGORIES = frozenset(RISK_PHRASES)

_PATTERNS = {
    category: re.compile(r"\b(" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE)
    for category, phrases in RISK_PHRASES.items()
}

SECRET_PLACEHOLDER = "[secret]"

# Actions that activate a control, and so are gated by its risk (ActionKind values).
RISKY_KINDS = frozenset({"CLICK", "DOUBLE_CLICK"})


def risks_of(label: str) -> set[str]:
    """Risk categories whose phrases appear as whole words in a control's label."""
    if not label:
        return set()
    return {category for category, pattern in _PATTERNS.items() if pattern.search(label)}


def disallowed_risks(label: str, allowed: Iterable[str]) -> set[str]:
    return risks_of(label) - set(allowed)


def redact(value: Any, secrets: Iterable[str]) -> Any:
    """Replace every occurrence of a secret in strings nested in dicts, lists and tuples."""
    secrets = tuple(s for s in secrets if s)
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, SECRET_PLACEHOLDER)
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(item, secrets) for item in value)
    return value
