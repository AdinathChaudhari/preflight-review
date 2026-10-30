# Changelog

## 1.0.2 (2026-10-30) — first public release

- Report-only pre-push review CLI: `preflight-review review`.
- Claude Code plugin with a `/preflight` skill.
- Report-only pre-push hook: `install-hook`; set `PREFLIGHT=0` to skip.
- Pipeline: scope from the merge base with base-branch config; deterministic tools (gitleaks, osv-scanner, ruff, ast-grep); a context packet with callers, co-change, guidelines and learnings; risk-ordered batches with a shared cache prefix.
- Four finder lenses plus an Opus security lane with source-to-sink trace, then a fresh-context verifier.
- Policy: hide pre-existing issues, no repeats across rounds, nit cap.
- Honest coverage: a "Not reviewed" section, merge risk reported as unknown when coverage is incomplete, and a `.dropped.jsonl` of discarded findings.
- Output: `<repo>-PR<##>-preflight-review-<##>.md`, a JSON twin and a manifest.
- `respond`, `learn` and `learn --propose` to close the loop on findings.
- `import-coderabbit` to turn existing review comments into a golden set.
- Eval harness with seeded, clean and golden suites.
- `init` with optional `--with-ast-rules` and `.coderabbit.yaml` import.
- Budget cap per run.
