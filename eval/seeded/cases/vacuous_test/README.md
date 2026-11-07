# vacuous_test

The new test cancels an order it never created. `cancel_order` returns early on the missing order, `store.get` returns `None`, and the `is None or ...` assertion passes, so the test would pass with an empty `cancel_order`. Covered by the contracts_tests lens's test-strength checklist (vacuous passes).
