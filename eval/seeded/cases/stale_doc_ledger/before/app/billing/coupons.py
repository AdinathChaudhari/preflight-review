"""Coupon redemption."""

from __future__ import annotations


def apply_coupon(subtotal: int, code: str, table: dict[str, int]) -> int:
    """Return the subtotal in cents after the coupon's percentage discount."""
    pct = table.get(code, 0)
    return subtotal - subtotal * pct // 100
