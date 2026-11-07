<!-- preflight-review prompt: sweep v3 | adapted from: Claude Code bundled /code-review max recipe (sweep phase); security-guidance review_api.py (second pass excludes earlier findings) | retrieved 2026-11-06 -->
Lens: gap sweep.

Other reviewers have already reviewed this change. Every candidate they reported is listed below. Your job is to find what they missed: issues of any kind (correctness, contracts and tests, data integrity, concurrency, error handling, security, quoted rules) that are NOT on the list.

Do not report an issue that is already listed, even with a different title, line range or wording. A new finding at the same place is fine only when it is a different failure. Look first where the list is thin: changed files, functions and hunks it never mentions, and failure modes that differ from the ones it already names.

## Already reported

{{known_findings}}

## Changed files

These files changed since commit {{diff_base}}. When no review packet is attached (a change too large for one packet), read the changed files yourself, starting with those the list above never mentions, and report the files you read under files_read_outside_packet.

{{changed_files}}

## Output

For each new finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote, and optionally finder_confidence. Also return files_read_outside_packet and coverage.

Return {"findings": [], "files_read_outside_packet": [], "coverage": [...]} if you find no new candidate, with coverage listing what you checked.
