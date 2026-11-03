# stale_doc_ledger

Class: a doc ledger written in commit 1 describes behaviour X; commit 2 removes X
from the code; the doc is left stale. The doc-code contradiction is only visible
by reading the doc against the final code.

- `before/`: base tree on `main`.
- `commits/01-add-guard/`: adds a `_CODE_RE` shape guard to `apply_coupon` and a
  ledger row (`docs/CHANGES.md`) describing it.
- `commits/02-drop-guard/`: removes the guard (review said it was too strict) and
  changes the lookup; `docs/CHANGES.md` is not touched.

The net diff (`main..seed/stale_doc_ledger`) adds the ledger and changes
`coupons.py`, but the guard itself never appears in it. A finder that reports the
stale ledger cites `coupons.py` as evidence; until this fix the prefilter dropped
every finding on a `.md` file as "docs file", even with code evidence.
