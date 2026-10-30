# preflight-review — reference

Everything the README leaves out: per-repo setup, every command and flag, the report layout, config keys, models per stage, eval, learnings and troubleshooting. Start with the [README](../README.md).

## Per-repo setup

- `preflight-review init [--force] [--with-ast-rules]` writes `.preflight/config.yaml` (every key at its default) and a rules file. `--with-ast-rules` copies the two bundled ast-grep rules (no-shell-true, no-yaml-unsafe-load) into `.preflight/ast-rules/`.
- `preflight-review doctor` checks the Claude CLI, git, and the optional tools (ruff, gitleaks, ast-grep, osv, semgrep).
- `preflight-review install-hook [--force]` installs a report-only pre-push hook. It runs `review --committed-only --incremental` and always lets the push through. `preflight-review uninstall-hook` removes it.
- `PREFLIGHT=0 git push` skips the hook for one push.

## Running

```sh
preflight-review review [--base origin/main] [--full | --incremental] [--mode quick|standard|deep] \
  [--json] [--show-pre-existing] [--backend cli|fake] [--committed-only] [--no-walkthrough] [--budget-usd N]
```

Exit codes: `0` = report written; `1` = incomplete (a lane failed or the budget ran out). The report is written in both cases.

Other commands: `version [--json]`, `respond`, `learn`, `eval`.

Example report: `docs/examples/smoke-report-example.md`.

## Report filename

`<repo-name>-PR<##>-preflight-review-<##>.md` in `.preflight/reviews/`. The last number is the round.

- `example-app-PR07-preflight-review-01.md` (branch has open PR 7)
- `example-app-PR-feature-outbox-claim-preflight-review-01.md` (no open PR: branch slug)

A `.json` with the same basename sits beside it, and `.preflight/reviews/latest.md` points to the newest report.

## Reading a report

Sections, in order:

1. Header and scope line (files, lines, what was not reviewed, result).
2. Walkthrough (with merge risk).
3. Findings (confirmed), each as `### F<n> · severity · category · effort · file:line`.
4. Unverified.
5. Previously reported (still tracked).
6. Pre-existing (hidden unless `--show-pre-existing`).
7. Suppressed by learnings.
8. Tool output (ruff, gitleaks, ast-grep, ...: version, status, finding count).
9. Run (models, calls, cost, duration, prompt and packet SHAs).
10. Respond, then an Aggregate prompt an agent can paste to work through the findings.

## Responding

```sh
preflight-review respond <F#|stable-id> fixed|wrong|intentional|didnt_matter ["reason"] [--learn]
preflight-review learn "text" --path GLOB [--from F3]
preflight-review learn --list
```

Responses are appended to `.preflight/responses.jsonl`. Learnings live in `.preflight/learnings.md` and take effect once that file is on the base branch.

## Config reference

`.preflight/config.yaml`, read from the base branch. Unknown keys are errors.

- `version`: 1
- `base`: `origin/main`
- `profile`: `chill` (quiet | chill | assertive)
- `mode`: `standard` (quick | standard | deep)
- `backend`: `cli`
- `budget_usd`: 1.5
- `max_rounds`: 3
- `verify_cmd`: null
- `rules_file`: `.preflight/RULES.md`
- `guidelines.discover`: true
- `guidelines.ancestors`: true
- `path_filters`: `!**/*.lock`, `!**/node_modules/**`, `!**/.venv/**`, `!**/*.min.*`, `!**/__pycache__/**`
- `path_instructions`: `[]` (items: `{path, instructions}`)
- `tools.ruff.select`: `[B, S, F, E9, RUF]`
- `tools.gitleaks`: true
- `tools.ast_grep`: true
- `tools.osv`: `auto` (auto | true | false)
- `tools.semgrep`: false
- `lenses`: `[correctness, contracts_tests, data_concurrency, rules_compliance]`
- `security_lane`: true
- `walkthrough`: true
- `triage.haiku`: false
- `intent.issues`: false
- `nitpicks.per_file`: 2
- `nitpicks.per_run`: 5
- `report_dir`: `.preflight/reviews`
- `concurrency`: 5
- `cache_warm_seconds`: 20
- `learnings.rerank`: true
- `learnings.cap`: 20
- `verifier.cap`: 15
- `models.*`: see below

## Models per stage

- `finder`: sonnet
- `finder_deep`: opus
- `security`: opus
- `verifier_major`: opus (findings at major severity or above in correctness or security)
- `verifier`: sonnet
- `triage`: haiku
- `rerank`: haiku
- `walkthrough`: sonnet

## Measured cost and time

All figures are notional list-price cost under subscription OAuth, not a charge. Full log: `docs/measurements.md`.

| Run | Diff | Cost | Time | Result |
|---|---|---|---|---|
| Demo (shell=True case) | 1 file, +2/−1 | $0.19 | 33 s | 1 confirmed, true positive |
| Seeded eval (8 cases) | 1 file each | $1.43 total | 3 m 30 s | recall 7/7, precision 9/9, 0 on the benign control |
| Run 7 self-review | 202 files, ~17k lines | $4.80 (budget $5) | 168 s | 52/202 reviewed (all `src/`), incomplete, 1 confirmed true positive |

A typical 5–20 file branch fits inside the default $1.50 budget. The Opus security lane is about 60% of the cost of a large run.

## Eval

```sh
preflight-review eval --set seeded [--backend cli|fake] [--no-judge] [--judge-models sonnet,opus] [--cases-dir DIR] [--out eval/results]
preflight-review eval --set clean  [--backend cli|fake]
preflight-review eval --set golden --repo-path <checkout of the reviewed repo> [--backend cli|fake]
preflight-review eval --set seeded --compare-bundled
```

- `seeded`: each case is `eval/seeded/cases/<name>/{before.py,after.py,expected.json}`. Measures whether a known bug is found.
- `clean`: rows in `eval/clean/cases.jsonl`. Measures false positives on code that should produce no finding.
- `golden`: labelled findings in `eval/golden/`. Measures recall of real bugs and precision.
- `--no-judge` matches deterministically; without it a model judge scores matches.

Add a clean row: append one JSON object per line to `eval/clean/cases.jsonl`: `{"repo_path": "/path/to/repo", "base_sha": "...", "head_sha": "...", "note": "merged PR #12"}`. Lines that start with `#` and blank lines are skipped.

Label a golden file: see `eval/golden/README.md`. Run `preflight-review import-coderabbit --repo <owner>/<repo> --out .preflight`, copy `.preflight/golden.jsonl` to `eval/golden/<repo>.jsonl`, then hand-label `is_real` and `in_scope` on every line.

## Learnings

1. `preflight-review respond F3 wrong "reason" --learn` labels a finding and writes a learning.
2. `preflight-review learn "text" --path GLOB [--from F3]` adds a learning by hand. `learn --list` shows them.
3. `preflight-review learn --propose` asks a model to turn the learnings into `path_instructions` and prints them.
4. `preflight-review learn --propose --apply` appends the proposals to `.preflight/config.yaml`.

## Troubleshooting

- `claude` not logged in: run `claude`, then `/login`. Run `preflight-review doctor` to see which check fails.
- `ruff` or `ast-grep` missing: the lane reports "not installed" and the review continues. Install with `uv tool install ruff` or `brew install ast-grep`.
- `gh` missing or no open PR: the report filename uses `PR-<branch-slug>` instead of `PR<##>`.
- Exit code `1`: the run was incomplete (a lane failed or the budget ran out), or an error occurred (one-line message). The report is still written for incomplete runs. There is no exit code 2.

## Roadmap not built in 1.0

- Fix loop and blocking gate: removed on purpose (see `docs/design.md`). The tool reports only.
- Verifier command execution in a scratch worktree.
- Semgrep lane (config key exists, default off).
- Embeddings for learnings retrieval.
- `PreToolUse` hook.

## Licences

Source and licence status of adapted prompts: `docs/licences.md`. See `LICENSE` and `NOTICE` for licensing and third-party attributions.

## What it never does

- Fix code.
- Block a push.
- Push.
