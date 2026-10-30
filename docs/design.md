# Design

## Goal and stance

preflight-review is a local, pre-push AI code reviewer. It reads the diff between your branch and its merge base, runs a multi-stage model pipeline, and writes a report. It is report-only: it never edits code and never blocks a push. Exit code 0 means the review completed; 1 means it was incomplete or errored.

## The idea

Combine what makes CodeRabbit-style reviews useful with the review discipline of Anthropic's published review prompts.

| CodeRabbit-style inputs | Anthropic-style discipline |
|---|---|
| Linters and scanners as context | Independent finder lenses, each with one job |
| Code graph: callers, callees, co-changed files | A fresh-context verifier per candidate finding |
| Guideline files (CLAUDE.md, AGENTS.md, rules) | Report only what is confirmed |
| Saved learnings from past reviews | Hide pre-existing issues, no style noise |
| Per-path instructions | A cap on review rounds |

## Pipeline stages

1. **Scope.** Find the merge base, list changed files, apply path filters. Config and rules are read from the base branch, so a branch cannot weaken its own review.
2. **Deterministic tools.** gitleaks, osv-scanner, ruff and ast-grep run on the changes. Their output is context for the models, not the report.
3. **Triage.** Classify the change and pick the review depth.
4. **Context packet.** Hunks plus the enclosing function, callers and callees, co-change history, guideline files, and learnings. Files are packed into risk-ordered batches that share one cached prefix.
5. **Finders.** Four lenses run independently: correctness, contracts and tests, data and concurrency, rules compliance.
6. **Security lane.** A separate pass that traces untrusted input from source to sink.
7. **Verify.** Each candidate goes to a verifier with fresh context and gets CONFIRMED, REFUTED or UNKNOWN. Only CONFIRMED findings reach the report.
8. **Cluster and policy.** Merge duplicates, apply the profile (quiet, chill, assertive), nitpick caps, and the pre-existing filter.
9. **Walkthrough.** A short summary of what the change does.
10. **Render.** Write the report files.

## Models per stage

Defaults from `config.py`, all overridable under `models:` in `.preflight/config.yaml`:

| Stage | Default |
|---|---|
| Finders | sonnet (opus for the deep mode) |
| Security lane | opus |
| Verifier | sonnet; opus for major and higher severity |
| Triage | haiku (only when `triage.haiku` is enabled) |
| Learnings rerank | haiku |
| Walkthrough | sonnet |

Other defaults: budget 1.50 USD per run, 3 rounds, concurrency 5.

## Backends

- `cli`: shells out to `claude -p`, using your own Claude Code login. No API key is needed.
- `FakeBackend`: returns scripted responses. All tests use it, so the suite runs offline.

## Report files

Reports go to `.preflight/reviews/` (configurable via `report_dir`).

- `<repo>-PR<##>-preflight-review-<##>.md`: the report. `PR<##>` is the open PR number for the branch, otherwise `PR-<branch-slug>`. The final number is the review round.
- `.json` twin: the same findings, machine-readable.
- `.dropped.jsonl`: candidates that were dropped, with the reason.
- `.manifest.json`: models, prompt SHAs and context packet SHAs for reproducibility.
- `latest.md`: a pointer to the most recent report.

## Learning loop

- `respond`: record a label and reason for a finding (for example, not a bug).
- `learn`: add a free-text learning, optionally scoped to a path glob or tied to a finding.
- `learn --propose`: have a model draft learnings from your recorded responses, for you to accept.

Learnings are reranked per change and capped before they enter the context packet.

## Evaluation

- **Seeded set:** small before/after cases with one planted bug each, plus a benign refactor as a control that must produce no findings.
- **Clean set:** diffs with no real bug, to measure false positives.
- **Golden set:** built by you from your own review history with `import-coderabbit`. It is not shipped with the repo.

An LLM judge matches findings to expected bugs; scoring is computed in code.

## Non-goals

- No fix loop: the tool reports, you decide.
- No blocking gate: it never fails a push.
- No hosted service: everything runs on your machine.
