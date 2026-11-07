<!-- preflight-review prompt: lens_contracts_tests v3 | adapted from: research/v1/anthropic/02-code-review-plugin-prompt.md; preflight miss sweep 2026-11-06 | retrieved 2026-11-06 -->
Lens: contracts and tests.

Flag only these:
- A changed function signature (parameters, defaults, order, raised errors) that no longer matches the callers listed in the packet.
- A public API or return-shape change that existing consumers in the packet would not handle.
- Data contracts between writers, readers and docs:
  - A writer that drops fields (`csv.DictWriter(..., extrasaction='ignore')`, a fixed header list, a hand-picked dict) that the README, docs or a downstream builder expects. Check the README and the builder consumers before reporting.
  - An OpenAPI or response schema missing a status code or field that the view actually returns.
  - A validator that drifts from the prompt or spec it checks: a field the spec requires that the validator never checks, or a field the validator demands that the spec no longer has.
  - A spec or config field the builder ignores (for example, the spec adds `window` but the builder still uses a hard-coded constant).
- Tests that no longer cover the change, or that still assert the old behaviour.
- Tests that pass without testing what they claim:
  - (a) Vacuous passes: `None == None`, both sides of a comparison looked up and both None, `pytest.skip` when the deliverable under test is missing, a test that mutates nothing before asserting the change.
  - (b) An assertion on text that also appears elsewhere on the page, prompt or output, so it passes without the new code. Quote both occurrences.
  - (c) A test that rebuilds the production query or logic inline instead of calling the production code.
  - (d) An ORM cascade or a Python-side default that masks the database behaviour the test claims to check.
  - (e) A stub or fake that raises before the code under test reaches the path the test is named for.
  - (f) Environment dependence: an ambient env var the test does not set; `load_dotenv` (or a `.env.local` file) re-populating a variable the test just unset; a cached settings getter (`get_settings` behind `lru_cache`) read without an override or cache clear; an inherited schema or database-selector env var. This is a definite defect, not an input-dependent potential issue, so the do-not-flag rule on missing tests does not apply. Quote the line that fails to isolate: the key missing from the cleanup tuple, the `load_dotenv` call, or the settings call without an override.
  - (g) A subprocess test that ignores `returncode`.
  - (h) A set comparison in a test of ordering or determinism.
  - (i) A new error branch or guard with no test, ONLY when the PR description or a commit message claims it is handled or tested, or the branch guards a security, data-loss or contract boundary named in the packet. Severity at most minor; this is an exception to the do-not-flag rule on missing tests.
- A missing co-change companion (a test or migration), ONLY when the packet lists that companion as expected.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category (usually "contracts"), severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote, and optionally finder_confidence. Also return files_read_outside_packet and coverage.

Return {"findings": [], "files_read_outside_packet": [], "coverage": [...]} if you find no candidate, with coverage listing what you checked.
