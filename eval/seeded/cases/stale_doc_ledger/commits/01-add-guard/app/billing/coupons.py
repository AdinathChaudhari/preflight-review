"""Coupon redemption."""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"^[A-Z]{4}-[0-9]{4}$")


def apply_coupon(subtotal: int, code: str, table: dict[str, int]) -> int:
    """Return the subtotal in cents after the coupon's percentage discount."""
    if not _CODE_RE.match(code):
        raise ValueError(f"malformed coupon code: {code!r}")
    pct = table.get(code, 0)
    return subtotal - subtotal * pct // 100
