# Smoke measurements (real `claude -p`)

**Median over the three runs: $0.17 notional cost, 37 s wall time** (Run 1 $0.173 / 36.7 s, Run 2 $0.107 / 33.4 s, Run 3 $1.091 / 44.6 s).

- Date: 2026-10-30. Version 0.3.0 installed with `uv tool install --force --editable`. Claude Code 2.1.314, subscription OAuth, no `ANTHROPIC_API_KEY`, never `--bare`.
- Cost is `total_cost_usd` summed over calls (`cost.usd` in the report JSON). Under a subscription it is notional list-price cost, not a charge.
- Tool lanes: `ruff` not installed (reported as "not installed"). `ast-grep` and `osv-scanner` are installed on this machine, but ast-grep had no rules (`.preflight/ast-rules` empty) and osv-scanner only ran in Run 3 (`uv.lock` changed). `gitleaks` ran in every run with 0 findings.
- Token columns are `usage` as reported by `claude -p`: they are summed over the turns of one call, so a 4-turn call re-reads its own prefix on every turn and looks bigger than its first-turn prompt.
- Real runs made: 5 model-calling reviews (Run 1a failed, Run 1, Run 2, Run 3, Run 3b experiment), plus a few one-call Haiku probes of tool/cache behaviour. One extra `review` call on Run 1's repo hit the "reuse" path (identical diff) and made no model call.

Install check (from a neutral directory):

```
$ preflight-review version --json
{"version": "0.3.0", "claude_code": "2.1.314", "prompts_sha": "abbf33a21d2f5dad2bf1e86466a700a8a0644137"}
$ preflight-review doctor      # outside a git repo
[FAIL] git ... not a git repository   [ok] claude: 2.1.314   [--] ruff: not installed
[ok] gitleaks 8.30.1   [ok] ast-grep 0.51.0   [ok] osv-scanner 2.7.0   [ok] gh 2.96.0
```

After the 1.0.0 bump, `preflight-review version --json` reports "version": "1.0.0" (same install, editable).

## Fixtures

- **Smoke repo** (Runs 1a, 1, 2): one git repo built by a scratch script from the seeded cases `nonatomic_claim` and `fstring_sql`. All seeded cases use the same path `app/target.py`, so they were written to `app/<case>.py` instead. `main` holds both `before` files, `smoke/seeded` both `after` files. Diff: 2 files, +9 -6.
- **Self repo** (Runs 3, 3b): a clone of this repo, branch `build/v1`, folder named `preflight-review`.
- `.preflight/config.yaml` came from `preflight-review init` (defaults: `lenses = correctness, contracts_tests, data_concurrency, rules_compliance`, `security_lane: true`, `walkthrough: true`, `budget_usd: 1.5`). It is untracked, and with an untracked config the review uses defaults, which are the same values.

## Run 1a (failed attempt, before fixes)

| Field | Value |
|---|---|
| Date | 2026-10-30 12:56 IST |
| Repo | smoke-repo (nonatomic_claim + fstring_sql), `smoke/seeded` vs `main` |
| Diff | 2 files, +9 -6 |
| Command | `preflight-review review --base main --mode standard --backend cli --json` |
| Lenses | correctness, contracts_tests, data_concurrency, rules_compliance + security lane + walkthrough |
| Lanes ok / failed | 7 ok / 1 failed (`verifier:316f0bd3248f`) |
| Outcome | `incomplete`: `verifier failures: 1`, `budget exceeded: USD 1.80 > 1.50` |
| Findings | 1 confirmed, 1 unverified, 0 dropped |
| Cost / wall | $1.802 notional / 59.1 s |

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness | sonnet-5-5 | 2 | 0 | 6479 | 906 | 2 | 5795 |
| finder:contracts_tests | sonnet-5-5 | 2 | 4829 | 1695 | 201 | 2 | 2027 |
| finder:data_concurrency | sonnet-5-5 | 2 | 4829 | 2111 | 555 | 2 | 3876 |
| finder:rules_compliance | sonnet-5-5 | 6 | 16378 | 51943 | 370 | 4 | 5564 |
| security | opus-5-5 | 4 | 4995 | 55090 | 1050 | 3 | 11757 |
| verifier:f4b931da4533 | opus-5-5 | 4 | 8664 | 12900 | 1005 | 4 | 10831 |
| verifier:316f0bd3248f | opus-5-5 | 4 | 13876 | 4272 | 292 | 5 | 13258 (FAILED, $0.93) |
| walkthrough | sonnet-5-5 | 2 | 0 | 1852 | 411 | 2 | 3708 |

Failure: the Opus verifier hit `error_max_budget_usd` ("Reached maximum budget ($0.38)"), was retried with the same cap, hit it again, and the two attempts cost $0.93 for no verdict. Cause and fix are under "Defects found and fixed". The 50k+ `cache_write` on `rules_compliance` and `security` did not recur (see Answer a).

## Run 1

| Field | Value |
|---|---|
| Date | 2026-10-30 13:01 IST (after fixes 1-3) |
| Repo | smoke-repo, fresh rebuild, `smoke/seeded` vs `main` |
| Diff | 2 files, +9 -6 |
| Command | `preflight-review review --base main --mode standard --backend cli --json` |
| Lenses | correctness, contracts_tests, data_concurrency, rules_compliance + security lane + walkthrough |
| Lanes ok / failed | 8 / 0 |
| Outcome | `complete`, `incomplete_reasons: []` |
| Findings | 2 confirmed, 0 unverified, 0 dropped |
| Cost / wall | $0.173 notional / 36.7 s (`cost.duration_s` 36.5) |

Report: `.preflight/reviews/smoke-repo-PR-smoke-seeded-preflight-review-01.json` (this is the report used for the contract test; its `.md` is copied to `docs/examples/smoke-report-example.md`).

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness | sonnet-5-5 | 2 | 6096 | 383 | 938 | 2 | 5918 |
| finder:contracts_tests | sonnet-5-5 | 2 | 6141 | 383 | 320 | 2 | 3775 |
| finder:data_concurrency | sonnet-5-5 | 2 | 6557 | 383 | 563 | 2 | 3824 |
| finder:rules_compliance | sonnet-5-5 | 6 | 20051 | 967 | 384 | 5 | 4483 |
| security | opus-5-5 | 4 | 16368 | 419 | 892 | 3 | 10032 |
| verifier:f4b931da4533 | opus-5-5 | 4 | 13125 | 4709 | 926 | 4 | 10301 |
| verifier:a8edae119251 | opus-5-5 | 4 | 11361 | 3020 | 998 | 4 | 10737 |
| walkthrough | sonnet-5-5 | 2 | 734 | 1122 | 414 | 2 | 3332 |

Findings: `app/fstring_sql.py:7` critical SQL injection (CONFIRMED, 2 verifier quotes); `app/nonatomic_claim.py:6` major non-atomic job claim (CONFIRMED, 3 verifier quotes). Both are the seeded bugs. Nothing else was reported (no false positives). Because Run 1a ran two minutes earlier, the shared prefix was already in the 1 h cache, so `cache_read` here is warm from Run 1a.

## Run 2

| Field | Value |
|---|---|
| Date | 2026-10-30 13:02 IST |
| Repo | same smoke-repo, round 02 |
| Diff | 2 files, +9 -6 |
| Command | `preflight-review review --base main --mode quick --full --backend cli --json` (without `--full` the same diff is reused: 0.4 s, no model call) |
| Lenses | same four + security + walkthrough |
| Lanes ok / failed | 7 / 0 (one verifier call: the other finding was already reported in round 01) |
| Outcome | `complete` |
| Findings | 1 confirmed, 0 unverified, 0 dropped; `previously_reported`: `a8edae119251` (open) |
| Cost / wall | $0.107 notional / 33.4 s |

Report: `.preflight/reviews/smoke-repo-PR-smoke-seeded-preflight-review-02.json`

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness | sonnet-5-5 | 2 | 6479 | 0 | 936 | 2 | 5833 |
| finder:contracts_tests | sonnet-5-5 | 2 | 6524 | 0 | 368 | 2 | 3708 |
| finder:data_concurrency | sonnet-5-5 | 2 | 6940 | 0 | 621 | 2 | 4259 |
| finder:rules_compliance | sonnet-5-5 | 6 | 20332 | 480 | 346 | 4 | 4126 |
| security | opus-5-5 | 4 | 16368 | 418 | 957 | 3 | 10868 |
| verifier:05370485d8df | sonnet-5-5 | 4 | 8673 | 9001 | 693 | 3 | 6779 |
| walkthrough | sonnet-5-5 | 2 | 734 | 1096 | 471 | 2 | 3685 |

Everything here is a cache read of the prefix written by Run 1, 90 seconds earlier.

## Run 3

| Field | Value |
|---|---|
| Date | 2026-10-30 13:03 IST |
| Repo | clone of preflight-review, `build/v1` (`HEAD~1..HEAD`, committed only) |
| Diff | 72 files, +3559 -83 (git: 73 files, +3740 -85; `uv.lock` unreviewed) |
| Command | `preflight-review review --base HEAD~1 --committed-only --mode quick --backend cli --json` |
| Lenses | correctness, contracts_tests, data_concurrency, rules_compliance + security + walkthrough |
| Lanes ok / failed | 6 / 0 |
| Outcome | `complete` |
| Findings | 0 confirmed, 0 unverified, 0 dropped |
| Cost / wall | $1.091 notional / 44.6 s |

The 1 500-line threshold was exceeded by every range (main..build/v1 15 664 lines, HEAD~2 6 059, HEAD~1 3 740). HEAD~1 was chosen as the smallest range, and it was not cut down to size; the pipeline reviewed all of it (72 files) in one pass with no `--max-turns` or timeout problem, in 44.6 s, so the fallback seeded repo (`missing_auth` + `swallowed_exception`) was not needed. Zero findings on 3.5k added lines of fresh code is plausible for `quick` and the "empty is valid" prompt, but unmeasured (no seeded bug in this range).

Report: `.preflight/reviews/preflight-review-PR-build-v1-preflight-review-01.json`

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness | sonnet-5-5 | 2 | 4829 | 41884 | 750 | 2 | 6173 |
| finder:contracts_tests | sonnet-5-5 | 4 | 51587 | 42709 | 643 | 3 | 5825 |
| finder:data_concurrency | sonnet-5-5 | 2 | 4829 | 42345 | 477 | 2 | 4200 |
| finder:rules_compliance | sonnet-5-5 | 2 | 4829 | 42125 | 83 | 2 | 3403 |
| security | opus-5-5 | 2 | 4995 | 43423 | 454 | 2 | 5223 |
| walkthrough | sonnet-5-5 | 2 | 734 | 2832 | 1971 | 2 | 10716 |

## Run 3b (experiment, not part of the median)

Same range, `--full`, round 02, run with a temporary change that put the packet at the start of each lens prompt so parallel calls would share a cache prefix. $0.748 / 47.5 s, 0 findings, per-call `cache_write` still ~42k on every finder. The change was reverted (see Defects, item 6).

## Answers

1. **(a) Per-call input under ~10k tokens: yes for the fixed overhead, no for large diffs.** With `--setting-sources ""` and `--system-prompt`, a one-turn finder call on the 9-line smoke diff had a total prompt (input + cache_read + cache_write) of 6.5k tokens (Run 1: 2 + 6096 + 383; Run 2: 2 + 6479 + 0), against ~47k without those flags. The fixed part (system prompt + tool definitions) is 4.8k tokens: Run 1a finders 2-4 read exactly 4829 from cache. Multi-turn calls report summed usage (`rules_compliance` 21k over 4-5 turns; Opus verifiers 16-18k over 4 turns), and in Run 3 every call is ~47k because the packet for a 3.5k-line diff is ~42k tokens. So the overhead target holds; the packet size is the diff, not overhead. Adding `--strict-mcp-config --disable-slash-commands` cut a Haiku probe from ~10k to ~7k total per call (connector and skill listings).
2. **(b) Cache sharing across processes: yes for the fixed prefix, no for the packet.** In Run 1a, finder 1 had `cache_read` 0 and finders 2-4 each read 4829 tokens, so the system prompt + tools prefix is shared between separate `claude -p` processes. The packet is not shared: in Run 3 all five calls wrote ~42k `cache_write` (about $0.17 each for Sonnet) and none read it. Moving the packet to the start of the prompt (Run 3b) did not change this, because the whole stdin prompt is one content block and the cache only matches at block boundaries. Sharing the packet would need it in the system prompt (identical across lenses) with the lens text in the user prompt; see Recommendation.
3. **(c) `--tools` honoured: yes.** Every call used `--tools Read,Grep,Glob` with `--permission-mode dontAsk`; no run reported an edit or write attempt, `files_read_outside_packet` lists only reads (`.preflight/RULES.md`, `src/preflight_review/evaluation/score.py`), and `git status --short` is `?? .preflight/` and nothing else in the smoke repo, the self clone, and after Run 1a in its repo. The source repo of this project was not touched by any run.
4. **(d) Failures: one.** Run 1a: `error_max_budget_usd` on an Opus verifier (cap $0.38 from `budget_usd / (findings + 2)`), retried at the same cap, $0.93 wasted, run marked `incomplete`. No `error_max_turns`, no JSON parse failure, no `error_no_structured_output`, no argv rejected by claude in any run. Fixed (items 1-2 below); Runs 1-3 had no failed call.

## Recommendation

Keep `cli` as the default backend. A real standard review of a small diff takes 35-60 s and ~$0.17 notional, which a subscription absorbs; cost only becomes visible on large diffs (Run 3: $1.09 for 3.5k lines, nearly all of it from five calls each re-writing the same 42k-token packet). A `cli-bare` + API key backend is not needed for correctness; it would only matter if the user wants explicit prompt-cache control. Cheaper path first: send the packet in a system-prompt block shared by all lenses (`--system-prompt-file`, or `--append-system-prompt`) and keep the lens text in the user prompt, which should turn 4 of 5 packet writes into reads (about 40-60% less on large diffs). That needs changes in `pipeline.py`/`stages/security.py`, outside this task, and an argv-size check (a 42k-token packet is ~170 KB).

## Defects found and fixed

1. **Retry on budget exhaustion** (`model/cli_backend.py`): `error_max_budget_usd` was retried with the same cap, so a failing verifier cost twice. It is now not retried (`NO_RETRY_SUBTYPES`). Tests: `test_budget_exceeded_is_not_retried`.
2. **Per-call budget cap too small** (`stages/finders.py`, `stages/verify.py`): the cap was `budget_usd / (n + 2)` ($0.25 per finder, $0.375 per verifier with the defaults), which aborted a legitimate Opus verification that needs ~$0.45. The cap now has a floor (finders $0.50, verifiers $0.75), never above `budget_usd`. Tests: `test_per_call_budget_floor_not_a_tiny_split` in `tests/test_finders.py` and `tests/test_verify.py`.
3. **Cost on retried calls was incomplete** (`model/cli_backend.py`): tokens, turns and duration of failed attempts were dropped while cost was summed, so `cost.calls` did not match `cost.usd` (Run 1a showed $0.93 next to 18k tokens). All usage fields are now summed over attempts. Test: `test_retry_sums_tokens_and_turns`.
4. **Hardening flags** (`model/cli_backend.py`): added `--strict-mcp-config --disable-slash-commands` (before `--tools`, which stays last) so account-level connectors and skills never enter the prompt (~3k tokens per call in a probe). `test_build_argv_exact` updated.
5. **Unreproduced:** in Run 1a, `rules_compliance`, `security` and both verifiers had 50k+ `cache_write` tokens (68k total on a 9-line diff). A probe reproduced one such 52k write once (Haiku, one `Read` call) and not again in 7 later probes, with or without the new flags. Cause unknown; it did not appear in Runs 1, 2, 3. Watch `cache_write` on small diffs in the next measurements.
6. **Tried and reverted:** packet-first prompt ordering for cache sharing (Run 3b). No gain, so it was removed, with its tests.

### Defects outside owned files (not edited)

- **Fixed on `build/v1` (uncommitted at time of writing), see Run 5.** - `src/preflight_review/pipeline.py` (reuse path, ~line 148; JSON summary built in `cli.py` ~line 58): when a review is reused (same diff), `preflight-review review --json` prints `"confirmed": 0, "unverified": 0` although the reused report holds 2 confirmed findings. Proposed fix: take the counts from the loaded report (`len(findings)`, `len(unverified)`) in the reuse branch.
- **Fixed on `build/v1` (uncommitted at time of writing), see Run 5; each case now has a distinct path in `meta.json`.** - `eval/seeded/cases/*`: `materialise()` and `load_seeded_cases()` give every case the same path `app/target.py` (`TARGET`), so two cases cannot share one repo without remapping the path (done here in the scratch script). A per-case `file` field in `expected.json` would allow it.
- The design notes mentioned `--system-prompt-file`, but the argv uses `--system-prompt` (test asserts `--system-prompt-file` is absent); the design doc (`docs/design.md`) does not name the flag.

## Contract test

`PREFLIGHT_REAL_REPORT_JSON=<Run 1 json> uv run pytest -q tests/test_report_contract.py` passes (9 passed, 0 skipped): both real findings are CONFIRMED with non-empty `verifier_evidence` quotes, `PREAMBLE` is in every `agent_prompt`, and every `stable_id` matches `^[0-9a-f]{12}$`. The same check on the Run 2 JSON also passes. Without the env var: 8 passed, 1 skipped. The real report path is the Run 1 JSON listed above.

## Run 4 (final verification, self-review of `build/v1`)

| Field | Value |
|---|---|
| Date | 2026-10-30 13:22 IST |
| Repo | this repo in place, branch `build/v1` @ 459d001 vs `main` (7386ffb) |
| Diff | 188 files, +16240 −1 (uv.lock path-filtered) |
| Command | `uv run preflight-review review --base main` (cli backend, standard mode, defaults) |
| Report | `.preflight/reviews/preflight-review-PR-build-v1-preflight-review-01.md` |
| Outcome | `complete`, 0 confirmed, 0 unverified, 6 model calls, no failed call |
| Cost / wall | $0.914 notional / 101.3 s (`/usr/bin/time` real 101.6 s) |

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness | sonnet-5-5 | 2 | 4829 | 34100 | 83 | 2 | 2721 |
| finder:contracts_tests | sonnet-5-5 | 2 | 4829 | 34145 | 83 | 2 | 1316 |
| finder:data_concurrency | sonnet-5-5 | 2 | 4829 | 34561 | 83 | 2 | 1400 |
| finder:rules_compliance | sonnet-5-5 | 2 | 4829 | 34341 | 307 | 2 | 4219 |
| security | opus-5-5 | 2 | 4995 | 35639 | 433 | 2 | 4779 |
| walkthrough | sonnet-5-5 | 2 | 734 | 5553 | 4051 | 2 | 20846 |

**Defect found (not fixed): silent coverage loss on large diffs.** The 0-finding result is not a clean bill. `packer._diff_section` caps the diff at `diff_cap=12_000` estimated tokens, packs files largest-first, and stops at the first file that does not fit. On this 181k-token diff it kept 2 of 188 files (`tests/fixtures/fake/eval_seeded.json`, `src/preflight_review/scope.py`, 8.6k tokens) and listed 186 under `# Not included (diff budget)` inside the packet only. Finders read no files outside the packet (`files_read_outside_packet` empty for every lane). The omitted list never reaches `Scope`/`render/report.py`, so the report says `reviewed 188/188, not reviewed: uv.lock` and `Result: complete`. Needed: carry `packet` omissions into the report's "not reviewed" line and mark the run `incomplete` (or split the diff into several packets / Haiku overflow summaries see docs/design.md, Context packet).

*Fixed on `build/v1` (uncommitted at time of writing) by batching + coverage accounting; see Run 5.*

## Run 5 (re-run of the Run 4 self-review after the D1/D2/D3 fixes)

| Field | Value |
|---|---|
| Date | 2026-10-30 |
| Repo | this repo in place, branch `build/v1` with the D1/D2/D3 fixes uncommitted in the worktree, vs `main` (7386ffb); 528 tests passed, 1 skipped before the run |
| Diff | 198 files, +17217 −1 (`--full`, worktree included; uv.lock path-filtered) |
| Command | `/usr/bin/time -p uv run preflight-review review --base main --full --json` (cli backend, standard mode, `budget_usd` 1.5) |
| Report | `.preflight/reviews/preflight-review-PR-build-v1-preflight-review-02.md` (and `.json`) |
| Batches | 3 run (the per-run budget stopped scheduling after batch 3; the total number planned is not recorded in the report) |
| Reviewed | 60/198 (Scope line and `scope.reviewed`/`scope.total` agree) |
| Outcome | `incomplete`, exit code 1, 0 confirmed, 0 unverified. Reason: `coverage: 136 of 198 changed files not reviewed` |
| Cost / wall | $1.495 notional / 56.9 s (`/usr/bin/time` real 57.14 s), 16 model calls, all ok |

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms |
|---|---|---|---|---|---|---|---|
| finder:correctness@b1 | sonnet-5-5 | 2 | 3446 | 25918 | 297 | 2 | 2713 |
| finder:contracts_tests@b1 | sonnet-5-5 | 2 | 28426 | 983 | 83 | 2 | 1349 |
| finder:data_concurrency@b1 | sonnet-5-5 | 2 | 28426 | 1399 | 83 | 2 | 3523 |
| finder:rules_compliance@b1 | sonnet-5-5 | 2 | 28426 | 1179 | 83 | 2 | 3427 |
| security@b1 | opus-5-5 | 2 | 3612 | 27463 | 108 | 2 | 1473 |
| finder:correctness@b2 | sonnet-5-5 | 2 | 3446 | 38909 | 560 | 2 | 5550 |
| finder:contracts_tests@b2 | sonnet-5-5 | 2 | 41417 | 983 | 83 | 2 | 1815 |
| finder:data_concurrency@b2 | sonnet-5-5 | 2 | 41417 | 1399 | 134 | 2 | 2271 |
| finder:rules_compliance@b2 | sonnet-5-5 | 2 | 41417 | 1179 | 83 | 2 | 1181 |
| security@b2 | opus-5-5 | 2 | 3612 | 40454 | 472 | 2 | 5181 |
| finder:correctness@b3 | sonnet-5-5 | 2 | 3446 | 40629 | 701 | 2 | 5914 |
| finder:contracts_tests@b3 | sonnet-5-5 | 2 | 43137 | 983 | 83 | 2 | 2615 |
| finder:data_concurrency@b3 | sonnet-5-5 | 2 | 43137 | 1399 | 375 | 2 | 3430 |
| finder:rules_compliance@b3 | sonnet-5-5 | 2 | 43137 | 1179 | 83 | 2 | 2244 |
| security@b3 | opus-5-5 | 2 | 3612 | 42174 | 417 | 2 | 5208 |
| walkthrough | sonnet-5-5 | 2 | 734 | 5848 | 4265 | 2 | 21758 |

**D1 (coverage) result.** The diff was split into batches and finders received 60 of 198 files. The remaining files were listed in the report's `Not reviewed (139)` section, one line per file: 136 with reason `run budget exhausted (USD 1.43 spent, next batch est. USD 0.55, budget USD 1.50)`, 2 `no textual diff`, 1 `path filter` (uv.lock). The run was marked `incomplete` and exited 1. The Scope line (`reviewed 60/198`) matched `scope.reviewed`/`scope.total` in the JSON. Small inconsistencies, recorded as found: 60 + 139 = 199, one more than 198, so one file appears to be counted in both lists; and `incomplete_reasons` says 136 (budget only) while the section header says 139 (budget + no-diff + path-filter). The budget stop worked: scheduling stopped before a fourth batch whose estimate ($0.55) would have exceeded $1.50. Total cost $1.495, under the cap. The finders found nothing in the 60 reviewed files; the 0-confirmed result covers only those 60 files.

**D2 (cache sharing) verdict: worked for the Sonnet finder lenses.** In each batch the first lens (`correctness`) wrote the packet to cache (25.9k, 38.9k, 40.6k `cache_write`; `cache_read` 3446, the system prefix only). The three follower lenses of the same batch read it: `cache_read` 28426 / 41417 / 43137 for batches 1 / 2 / 3, and `cache_write` only 983 / 1399 / 1179 (their own lens instructions). Run 4 had 4829 read and ~34k write on every lens. Follower finder cost was $0.008-0.014 per call, against $0.107-0.170 for the first lens of each batch. Not shared: the Opus `security` lane (different model, so a separate cache): `cache_read` 3612 and `cache_write` 27.5k / 40.5k / 42.2k per batch, $0.22-0.35 per call. The security lane was the largest cost item ($0.90 of $1.495 across the three batches), so its packet write is the next cost to look at; whether it can share a cache with the finders (it cannot while on a different model) was not tested here.

## Run 6 (Opus final verification of the D1/D2/D3 fixes, committed 1.0.1)

Named Run 6 because Run 5 above is the fixer's own re-run of the uncommitted worktree; this is the independent verifier run on the committed fix.

| Field | Value |
|---|---|
| Date | 2026-10-30 13:53 IST |
| Repo | this repo in place, branch `build/v1` @ 0c334c0 (1.0.1), clean worktree, vs `main` (7386ffb); `uv run pytest -q`: 528 passed, 1 skipped (87.8 s) |
| Diff | 198 files, +17260 −2 (`--full`; uv.lock path-filtered) |
| Command | `/usr/bin/time -p uv run preflight-review review --base main --full --json` (cli backend, standard mode, default `budget_usd` 1.50) |
| Report | `.preflight/reviews/preflight-review-PR-build-v1-preflight-review-03.md` (and `.json`, `.manifest.json`) |
| Batches | 3 run; budget stop before batch 4 (`USD 1.29 spent, next batch est. USD 0.61, budget USD 1.50`) |
| Reviewed | 58/198 (Scope line = `scope.reviewed`/`scope.total`) |
| Outcome | `incomplete`, exit 1, 0 confirmed, 0 unverified, 0 dropped. Reason: `coverage: 138 of 198 changed files not reviewed` |
| Cost / wall | $1.354 notional / 48.8 s (`/usr/bin/time` real 49.5 s), 16 model calls, all ok |

| Role | Model | Input | cache_read | cache_write | Output | Turns | ms | USD |
|---|---|---|---|---|---|---|---|---|
| finder:correctness@b1 | sonnet-5-5 | 2 | 3446 | 24665 | 83 | 2 | 1191 | 0.0998 |
| finder:contracts_tests@b1 | sonnet-5-5 | 2 | 27173 | 983 | 83 | 2 | 1558 | 0.0075 |
| finder:data_concurrency@b1 | sonnet-5-5 | 2 | 27173 | 1399 | 83 | 2 | 1914 | 0.0091 |
| finder:rules_compliance@b1 | sonnet-5-5 | 2 | 27173 | 1179 | 83 | 2 | 1174 | 0.0083 |
| security@b1 | opus-5-5 | 2 | 3612 | 26210 | 268 | 2 | 3370 | 0.2158 |
| finder:correctness@b2 | sonnet-5-5 | 2 | 3446 | 24290 | 83 | 2 | 2042 | 0.0983 |
| finder:contracts_tests@b2 | sonnet-5-5 | 2 | 26798 | 983 | 83 | 2 | 2179 | 0.0074 |
| finder:data_concurrency@b2 | sonnet-5-5 | 2 | 26798 | 1399 | 126 | 2 | 2494 | 0.0095 |
| finder:rules_compliance@b2 | sonnet-5-5 | 2 | 26798 | 1179 | 83 | 2 | 2634 | 0.0082 |
| security@b2 | opus-5-5 | 2 | 3612 | 25835 | 163 | 2 | 2044 | 0.2107 |
| finder:correctness@b3 | sonnet-5-5 | 2 | 3446 | 45581 | 868 | 2 | 6927 | 0.1914 |
| finder:contracts_tests@b3 | sonnet-5-5 | 2 | 48089 | 983 | 83 | 2 | 2282 | 0.0096 |
| finder:data_concurrency@b3 | sonnet-5-5 | 2 | 48089 | 1399 | 241 | 2 | 2401 | 0.0128 |
| finder:rules_compliance@b3 | sonnet-5-5 | 2 | 48089 | 1179 | 83 | 2 | 2352 | 0.0104 |
| security@b3 | opus-5-5 | 2 | 3612 | 47126 | 581 | 2 | 7190 | 0.3894 |
| walkthrough | sonnet-5-5 | 2 | 734 | 5880 | 4236 | 2 | 20674 | 0.0660 |

**Versus Run 4.** Total cache_read 328,088 tokens (Run 4: 25,045); total cache_write 210,270 (Run 4: 178,339) while delivering 58 files instead of 2. Follower finder lenses cost $0.007-0.013 per call (Run 4: every lens paid a ~34k write). Cost split: security (Opus) $0.816 = 60% of the run; all 12 finder calls $0.472. Extrapolation: at ~$0.35-0.6 per batch, the full 198-file branch needs roughly $3.5-5, i.e. `budget_usd` ~5 for a complete self-review.

**Verdict.**
- D1: honest. 58 files went to finders (3 packets, `packet_shas` in the manifest); the other 140 changed files are listed one per line with a reason; outcome `incomplete`, exit 1. D2: works for the Sonnet finders (followers read 26.8k-48.1k from cache). D3 + minor notes: fixed (`--json` carries `outcome`/`confirmed`/`round`/`reused`; seeded cases have distinct `app/...` paths; SKILL.md uses `--mode`).
- The 0-finding result covers only the 58 reviewed files, which are mostly docs, eval fixtures and top-level `src/preflight_review/*.py` modules. Nothing was verified, so there is no file:line evidence to judge.

**Remaining gaps found by this run (not fixed; report-only verification).**
1. Batch order spends the budget on low-risk files first. `plan_batches` (`context/packer.py:256`) orders by parent directory name, so `.claude-plugin/`, `docs/`, `eval/` go before `src/preflight_review/{context,stages,model,render}/` and `pipeline.py`, which were all left unreviewed. Order batches by risk (source before docs/fixtures/tests) so a budget stop drops the least important files.
2. Merge risk line says `low — 0 confirmed` on an `incomplete` run that left 70% of files unreviewed (`render/report.py:75-82`); it should say `unknown`/`incomplete` when coverage is partial. The walkthrough's model merge risk also says "no reported findings".
3. Count mismatch: `Not reviewed (141)` includes uv.lock, which is not in the 198 total, so 58 + 141 = 199; `incomplete_reasons` says 138 (budget only). Same inconsistency as Run 5.
4. `--json` stdout is not pure JSON: `cli.py:49` prints the human summary line before the JSON object, so `... --json | jq` fails. That summary line says `0 confirmed` with no mention of `incomplete` (the reason goes only to stderr).
5. Finder-dropped candidates (`FinderOutcome.dropped`, `stages/finders.py:197`) are only logged, never written to the report or JSON `dropped`; raw finder output is not persisted, so a lens that writes 868 output tokens and returns 0 findings (correctness@b3) cannot be audited.
6. Security lane (Opus) does not share the finder cache and is 60% of spend; options: run security only on batches containing source code, or put it on its own cached prefix ordered first.

## Run 7 (Opus final verification of the G1-G5 fixes, committed 1.0.2)

| Field | Value |
|---|---|
| Date | 2026-10-30 15:45 IST |
| Repo | this repo in place, branch `build/v1` @ f4c82d7 (1.0.2), clean worktree, vs `main` (7386ffb); `uv run pytest -q`: 569 passed, 1 skipped (141 s) |
| Diff | 202 files, +17985 −2 (`--full`; uv.lock path-filtered, listed separately) |
| Command | `/usr/bin/time -p uv run preflight-review review --base main --full --budget-usd 5` (cli backend, standard mode; config not changed) |
| Report | `.preflight/reviews/preflight-review-PR-build-v1-preflight-review-04.md` (and `.json`, `.manifest.json`, `.dropped.jsonl`; raw output in `.preflight/runs/preflight-review-PR-build-v1-preflight-review-04/raw/`, 40 files) |
| Batches | 8 run; budget stop before batch 9 (`USD 4.52 spent, next batch est. USD 0.63, budget USD 5.00`) |
| Reviewed | 52/202, all 52 under `src/preflight_review/`; 150 not reviewed (40 source, 54 test, 17 config, 39 docs/fixtures); 1 excluded by path filter |
| Outcome | `incomplete`, exit 1, 1 confirmed (Minor), 0 unverified, 0 dropped. Merge risk `unknown` in Markdown and JSON; model merge risk (Low) shown as suppressed |
| Cost / wall | $4.80 notional / 168.2 s (`/usr/bin/time` real 168.6 s), 42 model calls, all ok |

| Role (summed over 8 batches) | Model | Calls | cache_read | cache_write | Output | USD |
|---|---|---|---|---|---|---|
| finder:correctness (cache leader) | sonnet-5-5 | 8 | 24,122 | 333,876 | 3,911 | 1.377 |
| finder:contracts_tests | sonnet-5-5 | 8 | 398,524 | 8,287 | 1,032 | 0.083 |
| finder:data_concurrency | sonnet-5-5 | 8 | 350,494 | 11,192 | 3,045 | 0.110 |
| finder:rules_compliance | sonnet-5-5 | 8 | 350,494 | 9,432 | 664 | 0.079 |
| security | opus-5-5 | 8 | 25,284 | 346,402 | 4,821 | 2.873 |
| verifier:e660416b8d92 | sonnet-5-5 | 1 | 0 | 50,132 | 640 | 0.207 |
| walkthrough | sonnet-5-5 | 1 | 0 | 6,783 | 4,271 | 0.070 |

Totals: cache_read 1,148,918; cache_write 766,104. Cache writes are 1-hour ephemeral (`cacheCreationEphemeral1hInputTokens` in raw output). Security (Opus) is 60% of spend again; the correctness leader plus security write the same ~40k packet twice per batch.

**Gap verdicts.**
- G1 batch order: fixed. All 52 reviewed files are `src/`; every `[risk: …]` tag is shown in Not reviewed. Residual: `eval/seeded/cases/*.py` (16 fixture files) are tiered `source`, so they inflate "40 source" and would outrank real code on a smaller diff; small sensitive files (`hooks/pre-push.sh`, 44 lines) lose to large non-sensitive ones because score is lines × weight.
- G2 merge risk: fixed. Markdown, JSON (`merge_risk: "unknown"`) and the stderr line all say unknown/incomplete. Residual: the model-written walkthrough still says "every changed file is reviewed" and lists all 202 files under Changes despite the "(Partial …)" prefix.
- G3 `--json`: not exercised in this run (non-JSON invocation); covered by tests. Non-JSON summary line now names `incomplete` and merge risk.
- G4 counts: fixed. 52 + 150 = 202; uv.lock on its own "Excluded by path filters (1)" line; `incomplete_reasons` says 150 (148 budget + 2 no-diff). Residual: JSON `scope.unreviewed` holds `["uv.lock"]`, a confusing duplicate of `excluded_by_path_filter`.
- G5 dropped audit: works. `.dropped.jsonl` written (empty: 0 dropped) and the report has the `Dropped: 0` line; raw per-call output saved as `<lane>@b<N>.json`.
- Verifier context: the verifier call for osv.py got the packet containing the cited lines (50k write).

**Findings judged (file:line opened by the verifier).**

| id | sev | location | title | verdict |
|---|---|---|---|---|
| F1 (e660416b8d92) | Minor | `src/preflight_review/tools/osv.py:137-142` | Don't treat a failed base scan as an empty baseline | True positive, actionable. `_scan` returns `(None, note)` on tool failure; line 141 discards the note; `parse_osv(..., base_text=None)` sets `base = {}` so every current vuln is reported as newly introduced (Major, dependency) and the status still says `ran`. Fix is local. |

Precision 1/1. Recall is the open question: 40 finder/security calls over 52 source files (8,325 added lines) produced 1 candidate in total (data_concurrency@b6); 39 calls returned `findings: []`. Nothing was dropped, so nothing could be wrongly dropped. The report's Evidence block lists the osv.py:141 and :77 quotes twice (finder evidence + verifier evidence not de-duplicated).

**Remaining gaps.**
1. A complete self-review needs ~$11-13 at current rates (8 batches = 52 files = $4.5); still incomplete at $5.
2. Security lane cost: run it on its own cheaper prefix, or only on sensitive/source batches; consider 5-minute cache TTL for single-pass runs (1h writes cost more).
3. Low candidate yield (1 per 40 calls) on a new 17k-line codebase; check finder recall on the seeded eval before trusting a clean result.
4. Tiering: treat `eval/**` code as fixtures; give small sensitive files (hooks, shell) a floor score.
5. Walkthrough text contradicts coverage; constrain it to reviewed files or post-filter the claim.

## Eval 1 — seeded set, real models (2026-10-30)

`uv run preflight-review eval --set seeded`, judges sonnet+opus. 3 min 30 s, $1.43. Raw result: `eval/results/20261030-162934-seeded.json` (local; `eval/results/` is gitignored).

| Case | Findings | Planted bug caught | Cost |
|---|---|---|---|
| benign_refactor (control) | 0 | n/a | $0.064 |
| dropped_await | 1 | yes | $0.181 |
| fstring_sql | 1 | yes | $0.189 |
| path_traversal (earlier case) | 2 | yes (both match the one bug) | $0.231 |
| missing_auth | 1 | yes | $0.163 |
| nonatomic_claim | 2 | yes (both match the one bug) | $0.276 |
| shell_true | 1 | yes | $0.166 |
| swallowed_exception | 1 | yes | $0.159 |

The path_traversal row was measured on an earlier seeded case of similar difficulty that has since been replaced; rerun `eval --set seeded` for current numbers.

ep_recall 1.00 (7/7), precision 1.00 (9/9), F1 1.00, judge agreement 1.00, 0 findings on the benign control.

Limits: single-file cases with one obvious bug each, so this is a floor test. The two 2-finding cases point to duplicates that clustering did not merge. This answers Run 7 gap 3 only for small diffs; recall on real multi-file diffs needs the golden set.

## Eval 2 — seeded set, real models, 1.1 exhaustive defaults (2026-11-07)

`preflight-review eval --set seeded`, deep mode, judges sonnet+opus, 30 cases (25 with planted bugs, 26 expected findings, plus 5 clean controls). Defaults at the time: Opus finders and security lane, 3 Opus verifier votes per finding (Sonnet for rules findings), Fable adjudicator, one sweep per batch.

| Run | precision (incl. unverified) | precision_reported | ep_recall | F1 | Findings | Unverified | USD |
|---|---|---|---|---|---|---|---|
| Before the eval fixes (security verifier v5 labels, seed fixes; CHANGELOG 1.1.0) | 0.569 | — | 0.960 | — | — | — | 27.48 |
| After the eval fixes | 0.700 | 0.729 | 0.962 (25/26) | 0.810 | 50 | 2 | 26.94 |

The one miss in the second run is `prompt_md_kept` (a prompt `.md` that still asks for prose while the code now parses JSON; the finding must be anchored on the prompt file). Whether the finders did not report it or a later stage dropped it was not established in this run. Cost per case averaged about $0.90.

These numbers are for the exhaustive defaults. The lean defaults of 2026-11-07 (decision 15: Sonnet finders, security and sweep; up to 3 Sonnet votes with Opus only on escalated splits; tool findings and nits unverified; one sweep per run) change both cost and possibly recall and precision, so the seeded eval has to be re-measured before these figures are quoted for them.

## Eval 3 — lean defaults, 6-case slice (2026-11-07)

Lean defaults (Sonnet finders, up to 3 Sonnet votes with Opus only on critical splits, tool findings and nits unverified). Seeded slice of 6 cases (`benign_refactor`, `natural_sort_ok`, `fstring_sql`, `missing_auth`, `committed_env_file`, `migration_bootstrap_drift`), deep mode, judge sonnet+opus.

| Metric | Lean (this run) | Exhaustive 1.1, same 6 cases |
|---|---|---|
| ep_recall | 1.000 (4/4) | 1.000 |
| precision | 0.714 (5 of 7) | ~0.6 |
| clean controls with findings | 0 of 2 | 1 of 2 |
| USD | 1.61 | ~4.6 |

A slice, not the full set: re-run all 30 cases only when a prompt or verifier change needs it (~$8-10 at lean prices).
