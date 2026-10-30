# Golden set

Labelled review findings used by `preflight-review eval --set golden` to measure recall of real bugs and precision on all emitted comments.

The golden set is user-built and not shipped: no data is committed here. Build one from your own repo's CodeRabbit history.

## Producing a golden file

1. `preflight-review import-coderabbit --repo <owner>/<repo> --out .preflight` (needs `gh` auth)
2. `cp .preflight/golden.jsonl eval/golden/<repo>.jsonl`
3. Hand-label `is_real` and `in_scope` on every line.
4. `preflight-review eval --set golden --repo-path /path/to/checkout`

`.preflight/history.jsonl` (same run) feeds retrieval of past review comments.

## Fields

| Field | Meaning |
|---|---|
| repo, pr | GitHub repo and pull request number |
| commit | head commit CodeRabbit reviewed (replay the base..commit diff) |
| file, line_start, line_end | location of the comment |
| bug_class | CodeRabbit category |
| expected_severity | critical / major / minor / trivial |
| is_real | true if it was a bug that mattered; null until hand-labelled |
| in_scope | false if real but outside the PR's scope or not code review; null until labelled |
| author_verdict | fixed / declined / deferred / unknown |
| title | CodeRabbit's bold title |

## Privacy

Golden files contain your code paths and review titles. Keep them out of public repos: `eval/golden/*.jsonl` is your own data.
