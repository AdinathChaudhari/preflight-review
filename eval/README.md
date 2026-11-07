# Eval harness

Measures how well the reviewer finds real bugs and how quiet it is. Three sets, one command: `preflight-review eval --set golden|seeded|clean`.

## Sets

| Set | Source | Measures |
|---|---|---|
| `seeded` | `eval/seeded/cases/<name>/{before.py,after.py,expected.json,meta.json}` (or `before/` + `commits/NN-name/` trees for multi-commit cases): 25 cases with injected bugs (26 expected findings) plus 5 clean controls | EP recall, precision, SNR, per-category recall |
| `golden` | `eval/golden/*.jsonl` (hand-labelled CodeRabbit history, rows with `is_real` and `in_scope` true and a `commit`) | recall of real bugs, precision |
| `clean` | `eval/clean/cases.jsonl` (merged commits that should produce no confirmed findings) | confirmed findings per case, target 0 |

## Formulas

From CodeRabbit's EP (error pattern) benchmark, research `round2/05-deterministic-layer-merge-and-eval-scoring.md` section 5:

- Only inline-lane findings count. nitpick, outside_diff and pre_existing are excluded from every denominator.
- EP_PASS finding = a finding matched to a real golden item.
- precision = EP_PASS / counted findings in the report plus Unverified (None when there are no findings). This is "precision incl. unverified": every UNKNOWN finding a golden does not match counts as a false positive. The key keeps this 1.0 definition so runs stay comparable.
- precision_reported = EP_PASS / counted findings in the report only (confirmed findings; Unverified left out). The gap between the two is what Unverified costs.
- n_unverified = counted findings that landed in Unverified; each case also carries its own `unverified` count.
- Important = EP_PASS plus any finding a human marked as a real major/critical bug.
- SNR = Important / (counted - Important). None, with `snr_note`, when nothing is noise.
- ep_recall = real golden items with at least one match / real golden items. A golden matched by an Unverified finding counts: it was surfaced.
- F1 = 2PR/(P+R); 0.0 when P or R is 0; None when either is undefined.
- per_category_recall uses the coarse class (security, correctness, data, style).

Matching: a finding matches a golden item when an LLM judge says they describe the same issue. Two judge models (default `sonnet,opus`) must both say yes; `agreement_rate` is the share of pairs where all judges agreed. `--no-judge` uses a deterministic match instead: same file, line ranges within 3 lines, same coarse category.

## How to run

```bash
# Seeded, deterministic, offline (no model calls):
PREFLIGHT_FAKE_RESPONSES=tests/fixtures/fake/eval_seeded.json \
  uv run preflight-review eval --set seeded --backend fake --no-judge

# Seeded, real models (uses the claude CLI, costs money):
uv run preflight-review eval --set seeded

# Golden: needs a checkout of the repo the goldens came from.
uv run preflight-review eval --set golden --repo-path /path/to/your-repo

# Clean: fill eval/clean/cases.jsonl first, then:
uv run preflight-review eval --set clean
```

Golden and clean runs never touch your repo: each commit is cloned with `git clone --shared --no-checkout` into a temp dir and reviewed there.

## Hand-labelling goldens

1. `preflight-review import-coderabbit --repo <owner>/<repo> --out .preflight`
2. `cp .preflight/golden.jsonl eval/golden/<repo>.jsonl`
3. For each row read the diff, then set `is_real` (true if it was a bug that mattered) and `in_scope` (false if real but outside the PR). Rows need `commit` to be replayed.
4. Real bugs the reviewer finds that are not in the golden set: add them as new rows so they stop counting as false positives.

## Clean set

`eval/clean/cases.jsonl` holds one JSON object per line: `{"repo_path", "base_sha", "head_sha", "note"}`. Lines starting with `#` and blank lines are ignored. Fill it with 5 to 10 merged commits you consider bug-free.

## Multi-commit cases

A case directory with `commits/` uses this layout instead of `before.py`/`after.py`: `before/` is a file tree committed on `main` (optional), and each `commits/<NN-name>/` tree is written and committed in name order on the seed branch. A path part starting with `dot_` is written with a leading `.` (`dot_env.local` becomes `.env.local`), so a case can hold dotfiles without this repository committing one. Expected items may set `"file"` per item; it defaults to meta.json's `file`. See `eval/seeded/cases/stale_doc_ledger/README.md`.

## Determinism

Each case's `meta.json` is `{"file": "<relative posix path>"}`: the repo path the case's code is written to (and that expected items and findings refer to). It must be non-empty, relative, and contain no `..`; when absent it defaults to `app/target.py`. Give each case a distinct realistic path.

`tests/fixtures/fake/eval_seeded.json` scripts the fake backend per case (`__per_case__`), so the seeded numbers are fixed: 25 of 26 bugs found (nonatomic_claim is deliberately missed), precision 1.0, ep_recall 0.962. The clean controls (`benign_refactor`, `json_constant_ok`, `pipefail_ok`, `natural_sort_ok`, `migration_dialect_skip_ok`) expect no finding; with a real backend, any finding on them counts against precision, so each control is kept free of real bugs and of fixture gaps (a missing script, table or extension) that a reviewer would rightly flag. `stale_doc_ledger` is multi-commit: a doc ledger written in commit 1, the behaviour it describes removed in commit 2; before 1.0.3 its finding was dropped by the prefilter (0.75). Use it to check the harness itself, not the reviewer.

Verifier-stage cases: `committed_env_file`, `prompt_injection_grade` and `cors_wildcard_credentials` are classes the security verifier's exclusions used to refute (see `docs/reference.md`, Verification). `plan_progress_drift` (a docs-only diff) and `prompt_md_kept` (a finding on a prompt file in a mixed diff) cover the doc-scope prefilter. With the fake backend they check the plumbing only; run them on real models to measure whether the verifier and prefilter keep them.

CI and deploy cases: `workflow_run_pr_deploy` (a `workflow_run` deploy that a pull request can trigger, and with no concurrency group, so an older commit can land last) and `runbook_job_order` (a runbook-only diff that applies a Job and never deletes the old one, so later deploys skip their migrations) cover the path-gated `deploy_ci` lens. The runbook case also checks that a docs-only diff touching the lens's paths is reviewed, and that its finding on the runbook survives the docs drop.

1.1 release cases: `prompt_rule_contradiction` (a prompt-only diff where a new prompt layer contradicts the base layer it is composed with), `vacuous_test` (a new test that passes even if the function under test does nothing) and `migration_bootstrap_drift` (a migration the hand-maintained bootstrap schema does not mirror). They exercise the prompt-only review path, the contracts_tests test-strength checklist and the data_concurrency bootstrap-parity item. With the fake backend they check the plumbing only.

## Results

Each run writes `eval/results/<YYYYmmdd-HHMMSS>-<set>.json` (override with `--out`). Keys: set, generated_at, backend, judge, precision (incl. unverified), precision_reported, ep_recall, snr, snr_note, f1, n_findings, n_goldens, n_unverified, per_category_recall, agreement_rate, cases, skipped, failed, cost_usd, path. `failed` lists cases whose review produced nothing to score (every finder lane failed), with their incomplete reasons; they are kept out of the scores, and `cases[].unverified` gives each case's Unverified count.
