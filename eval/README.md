# Eval harness

Measures how well the reviewer finds real bugs and how quiet it is. Three sets, one command: `preflight-review eval --set golden|seeded|clean`.

## Sets

| Set | Source | Measures |
|---|---|---|
| `seeded` | `eval/seeded/cases/<name>/{before.py,after.py,expected.json,meta.json}` (or `before/` + `commits/NN-name/` trees for multi-commit cases): 8 injected bugs plus one benign refactor | EP recall, precision, SNR, per-category recall |
| `golden` | `eval/golden/*.jsonl` (hand-labelled CodeRabbit history, rows with `is_real` and `in_scope` true and a `commit`) | recall of real bugs, precision |
| `clean` | `eval/clean/cases.jsonl` (merged commits that should produce no confirmed findings) | confirmed findings per case, target 0 |

## Formulas

From CodeRabbit's EP (error pattern) benchmark:

- Only inline-lane findings count. nitpick, outside_diff and pre_existing are excluded from every denominator.
- EP_PASS finding = a finding matched to a real golden item.
- precision = EP_PASS / counted findings (None when there are no findings).
- Important = EP_PASS plus any finding a human marked as a real major/critical bug.
- SNR = Important / (counted - Important). None, with `snr_note`, when nothing is noise.
- ep_recall = real golden items with at least one match / real golden items.
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

A case directory with `commits/` uses this layout instead of `before.py`/`after.py`: `before/` is a file tree committed on `main` (optional), and each `commits/<NN-name>/` tree is written and committed in name order on the seed branch. Expected items may set `"file"` per item; it defaults to meta.json's `file`. See `eval/seeded/cases/stale_doc_ledger/README.md`.

## Determinism

Each case's `meta.json` is `{"file": "<relative posix path>"}`: the repo path the case's code is written to (and that expected items and findings refer to). It must be non-empty, relative, and contain no `..`; when absent it defaults to `app/target.py`. Give each case a distinct realistic path.

`tests/fixtures/fake/eval_seeded.json` scripts the fake backend per case (`__per_case__`), so the seeded numbers are fixed: 7 of 8 bugs found (nonatomic_claim is deliberately missed), precision 1.0, ep_recall 0.875. `stale_doc_ledger` is multi-commit: a doc ledger written in commit 1, the behaviour it describes removed in commit 2; before the prefilter kept doc findings with code evidence, it scored 0.75. Use it to check the harness itself, not the reviewer.

## Results

Each run writes `eval/results/<YYYYmmdd-HHMMSS>-<set>.json` (override with `--out`). Keys: set, generated_at, backend, judge, precision, ep_recall, snr, snr_note, f1, n_findings, n_goldens, per_category_recall, agreement_rate, cases, skipped, cost_usd, path.
