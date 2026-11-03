"""Coupon redemption."""

from __future__ import annotations


def apply_coupon(subtotal: int, code: str, table: dict[str, int]) -> int:
    """Return the subtotal in cents after the coupon's percentage discount.

    Codes are normalised (trimmed, upper-cased) and looked up as-is. There is
    no shape check: the marketing team issues codes in several formats, and an
    unknown code simply gives no discount.
    """
    pct = table.get(code.strip().upper(), 0)
    return subtotal - subtotal * pct // 100
