# plan_progress_drift

A docs-only diff: PLAN.md and PROGRESS.md disagree on the backup retention (8 vs 7). Before 1.1 a docs-only diff skipped every model stage, and the prefilter dropped any `.md` finding without evidence in changed code. With `docs_review: auto` the diff gets a doc-consistency review, and a finding that quotes both changed docs is kept.

The golden anchors on `PROGRESS.md:3` only. The matcher compares one file per golden, so a correct finding anchored on `PLAN.md:3` (the other side of the disagreement) scores as a miss plus a false positive; read such a result by hand rather than adding a second golden, which would require both anchors for full recall.
