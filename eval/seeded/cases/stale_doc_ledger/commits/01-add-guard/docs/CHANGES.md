# Changes on this branch

| File | Change |
|---|---|
| `app/billing/coupons.py` | `apply_coupon` rejects malformed codes: a `_CODE_RE` shape guard raises `ValueError` before the table lookup. |
| `app/billing/coupons.py` | Codes must match `AAAA-0000`; anything else is refused, never silently treated as 0 %. |
