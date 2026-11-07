<!-- preflight-review prompt: docs_review v2 | adapted from: preflight-review misses log (docs-only diffs were skipped, so doc contradictions went unreviewed) | retrieved 2026-11-06 -->

## Docs-only diff: doc-consistency review

Every changed file in this diff is documentation. People act on these files, so a doc that contradicts itself, another doc or the code is a defect. Check:

- Cross-doc contradictions: two changed docs, or a changed doc and an unchanged doc or code file, that state different facts (counts, names, commands, paths, versions, status).
- Internal arithmetic and counts: totals that do not add up, a list whose length differs from the number the text gives, percentages that do not match their parts.
- Stale status text: "done", "pending", "not built", dates or version numbers that another changed line or file already contradicts.
- Commands, paths and identifiers in the doc that do not exist in the repository (read the files to check).

Anchor each finding (`file`, `start_line`) on the changed doc line, and quote both sides of the contradiction in `evidence`: the doc line, plus the other doc, code or config file that shows the real fact (two quotes from the same doc are enough for an internal contradiction). A finding on a doc line this diff did not change is not reported. Use lane `inline` and severity `minor` or above. Do not report wording, tone, style or formatting.
