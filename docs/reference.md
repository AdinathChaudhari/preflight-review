# preflight-review — reference

Everything the README leaves out: per-repo setup, every command and flag, the report layout, config keys, models per stage, eval, learnings and troubleshooting. Start with the [README](../README.md).

## Per-repo setup

- `preflight-review init [--force] [--with-ast-rules]` writes `.preflight/config.yaml` (every key at its default) and a rules file. `--with-ast-rules` also copies the bundled ast-grep rules (`no-shell-true`, `no-yaml-unsafe-load`) into `.preflight/ast-rules/`.
- `preflight-review doctor` checks the Claude CLI, git, the config `review` will use (`.preflight/config.yaml` from the base branch; when the base branch has none it checks the working-tree file and says review uses defaults until it is merged), and the optional tools (ruff, gitleaks, ast-grep, osv-scanner, zizmor, actionlint, shellcheck). It also makes one small model call to check that the `models.adjudicator` alias answers on this account and provider; when it does not, the check is shown as optional and split votes adjudicate on `models.adjudicator_fallback`.
- `preflight-review install-hook [--force]` installs a report-only pre-push hook. It runs `review --committed-only` (incremental by default) and always lets the push through. `preflight-review uninstall-hook` removes it.
- `PREFLIGHT=0 git push` skips the hook for one push.

## Running

```sh
preflight-review review [--base origin/main] [--full] [--mode auto|quick|standard|deep] \
  [--json] [--show-pre-existing] [--backend cli|fake] [--committed-only] [--no-walkthrough] [--budget-usd N] [--yes] [--resume]
```

Exit codes: `0` = report written; `1` = incomplete (a lane failed, files were not reviewed, or a budget cap ran out, from `--budget-usd` or `budget_usd`) or not started (the estimate was above `confirm_above_usd` and nobody confirmed), or paused (the Claude usage limit was reached). The report is written in every case.

Outcomes (`outcome` in the JSON and the report's Result line): `complete`, `incomplete`, `paused`, `not_started`.

### Review depth

| Mode | Verifier votes (Sonnet) | Sweep | Effort | Finder turns |
|---|---|---|---|---|
| `auto` (default) | resolves to `deep` or `standard` once per run, before any model call | | | |
| `quick` | 1 | no | `effort.quick` (low) | 4 |
| `standard` | 2, a 3rd on disagreement (`verifier.votes: 3`) | no | `effort.standard` (medium) | 6 |
| `deep` | 2, a 3rd on disagreement (`verifier.votes: 3`) | one per run (`sweep: true`) | `effort.deep` (high) | 18 (security 18, verifier 10) |

Every mode runs the same lenses on the same model (Sonnet by default) and, with `security_lane: true`, the security lane. Deep mode buys more turns, higher effort and the sweep, not a bigger model.

`auto` runs `deep` when any changed path matches a glob in `auto.deep_paths`, and `standard` otherwise. The default globs cover migrations (`**/migrations/**`, `**/alembic/**`, `**/*.sql`), auth and security (`**/auth/**`, `**/*auth*.py`, `**/security/**`, `**/permissions*`), secrets and env files (`**/.env*`, `**/*secret*`, `**/*credential*`), CI and deploy (`.github/workflows/**`, `**/Dockerfile*`, `**/k8s/**`, `**/helm/**`, `**/deploy/**`, `**/*.sh`), prompts and agent instructions (`**/prompts/**`, `**/*.jinja2`, `**/AGENTS.md`, `**/CLAUDE.md`, `**/SKILL.md`), and scoring, grading, billing and payment code (`**/*scor*.py`, `**/*grad*.py`, `**/billing/**`, `**/payment*/**`). Globs are gitignore-style, as in `path_filters`. An explicit `--mode`, or a `mode:` other than `auto` in the config, always wins.

**Paths that never make a run deep**, whatever the globs say: anything under a `tests`, `test`, `__tests__`, `spec`, `eval`, `evals`, `fixtures`, `testdata`, `docs` or `doc` directory at any depth, and any file in risk tier `test` or `docs` (`test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts`, prose `.md`/`.rst`/`.txt`, lockfiles, generated files). Eval and fixture trees hold sample migrations, workflows and prompts; before this rule a 74-file change went deep only because eval fixtures contained migration files. Prompt files and agent instructions (`SKILL.md`, `CLAUDE.md`, `AGENTS.md`, `.claude/**/*.md`) are risk tier `source`, so they still count unless they sit under one of those directories. The rule lives in `depth.deep_eligible`.

The report header has one Mode line, for example `Mode: deep (auto — crucial paths: db/migrations/0015.py, app/auth/login.py)`, `Mode: standard (auto — no crucial paths changed)`, `Mode: quick (set explicitly)` or `Mode: quick (chosen at the pre-run depth menu)`. The manifest records `mode_requested` (`menu` when the depth was picked at the menu), `mode_resolved`, `crucial_paths` (up to 10), the pre-run `estimate` (USD, minutes, files, batches, calls), and `calls_by_stage` / `calls_by_model` for the run.

### Pre-run estimate and depth menu

A review is never cut short once it starts (only an opt-in `--budget-usd` stops batches). Instead, before the first model call, preflight estimates the run and prints it with its planned calls, for example:

```text
preflight: estimated ~$1.9, ~7 min for 10 files in standard mode (2 batches; ~36 calls: 8 finders, 2 security, ~15 verify (~1 adjudicated), 10 triage, 1 walkthrough; rough)
```

The estimate counts batches from the diff size; finder and security calls per batch; one sweep per run in deep mode; verifier calls for an expected 3 model candidates per batch (about 2.3 Sonnet votes each with `verifier.votes: 3`, plus a 5% share of Opus adjudications); batched Haiku triage (up to 20 files per call); and the walkthrough. Each call is priced from Opus-equivalent rates fitted to measured runs (an incremental 74-file deep run in 10 batches: 56 finder calls ~$14.8, 11 security calls $3.7; the seeded eval in deep mode: ~$0.90 per 1-3 file case), scaled by the configured model (Sonnet 1/2, Haiku 1/40 of Opus; an unknown model counts as Opus). Read it as an order of magnitude, not a quote.

When the estimate is above `confirm_above_usd` ($5 by default) and `--yes` was not passed:

- On a terminal, preflight offers a choice of depth, one line per mode with its own estimate:

  ```text
  preflight: 74 files, 10 batches. Choose depth:
    1) quick     ~$4.2   ~165 calls  ~13 min  (Sonnet finders at low effort, 1 Sonnet vote, no sweep)
    2) standard  ~$11    ~206 calls  ~35 min  (Sonnet finders at medium effort, up to 3 Sonnet votes, opus on critical splits, no sweep)
    3) deep      ~$16    ~211 calls  ~59 min  (Sonnet finders at high effort, up to 3 Sonnet votes, opus on critical splits, sweep)  (auto)
    4) cancel
  Choose [3]:
  ```

  Enter runs the default, marked `(auto)` (or `(configured)` when the mode was set explicitly). A number or a mode name picks that mode; anything else cancels, so a typo spends nothing. A picked mode is noted in the Run section and the Mode line.
- Without a terminal (the pre-push hook, the `/preflight` skill, CI), or on cancel, no model is called. The report says the review was not started and lists the estimate for each depth and the exact command for each (`... --mode quick --yes`, `... --mode standard --yes`, `... --mode deep --yes`). The summary line starts with `preflight: not started:` and names each mode's cost and calls, the JSON `outcome` is `not_started`, and the exit code is 1. Branch state is not changed, so the next run keeps the same round number.

Under the threshold only the single estimate line is printed and the run starts. `--yes` skips the menu and runs the resolved mode; `confirm_above_usd: null` turns the menu off.

### Usage limit: PAUSED and `--resume`

When a model call fails because the Claude account hit its usage or rate limit (`You've hit your 5-hour limit · resets 4:40pm`, `usage limit`, `rate limit`, HTTP 429), preflight stops calling the model. Calls already running finish; every call not yet started is skipped without starting `claude` and is recorded as `paused: usage limit`, not as a failed lane. A usage-limited call is not retried. `overloaded_error` (529) is not a usage limit: it is retried as before.

The run's outcome is `paused`, the exit code is 1, and the report's Result line reads `PAUSED — Claude usage limit reached (resets <time>). N of M calls done. Resume with: preflight-review review --resume` (with the run's other flags). `M` counts the calls this run asked for: findings a skipped lane would have produced were never verified, so a complete run can make more calls. Findings gathered before the limit are reported; candidates whose verification was skipped stay Unverified. The summary line starts with `preflight: paused:` and the JSON has a `paused` object (`resets`, `calls_done`, `calls_total`, `resume_command`). A paused run does not advance the round or the findings state.

A paused run saves every successful call to `.preflight/runs/<report>/calls.json` (beside the `raw/` lane outputs), keyed by a hash of the role, model, prompts (the batch packet is part of the system prompt), schema, turn limit and effort. After the reset, `preflight-review review --resume` re-runs the same scope with the same round number and report name, answers every call whose key matches from that file, and calls the model only for the missing lanes, votes and sweeps. The report notes `resumed from round NN; reused K cached call(s)`. A resumed run does not ask the `confirm_above_usd` question again. If the diff changed since the paused run, or there is no paused run on the branch, `--resume` runs a normal review and the report notes why.

Other commands: `version [--json]`, `respond`, `learn`, `eval`.

Example report: `docs/examples/smoke-report-example.md`.

## Report filename

`<repo-name>-PR<##>-preflight-review-<##>.md` in `.preflight/reviews/`. The last number is the round.

- `example-app-PR07-preflight-review-01.md` (branch has open PR 7)
- `example-app-PR-feature-outbox-claim-preflight-review-01.md` (no open PR: branch slug)

A `.json` with the same basename sits beside it, and `.preflight/reviews/latest.md` points to the newest report.

## Reading a report

Sections, in order:

1. Header, scope line (files, lines, what was not reviewed), Mode line and result.
2. Walkthrough (with merge risk).
3. Findings (confirmed), each as `### F<n> · severity · category · effort · file:line`.
4. Unverified.
5. Previously reported (still tracked).
6. Pre-existing (hidden unless `--show-pre-existing`).
7. Suppressed by learnings.
8. Tool output (ruff, gitleaks, ast-grep, osv-scanner, secret-files, zizmor, actionlint, shellcheck: version, status, finding count). A tool that is enabled and has work in this diff (ruff or ast-grep with changed Python, gitleaks always, osv-scanner with a changed manifest, zizmor or actionlint with a changed workflow, shellcheck with a changed `.sh`/`.bash` file) but is missing, fails, or returns output that cannot be parsed is marked `DEGRADED`: the report opens with a `DEGRADED:` banner naming the tool, the Result line reads `complete (DEGRADED: <tools>)`, the summary line ends with `DEGRADED: <tools> did not run` (a tool that ran on part of its input, such as osv-scanner with one failed manifest, is listed as `<tool> ran partially` and as `<tool> (partial)` on the Result line), and the JSON has a `degraded` list. The run is not failed (exit code stays 0), but it is never presented as a clean complete run. A disabled tool, or one with nothing to check, is never degraded.
9. Run (models, calls, cost, duration, prompt and packet SHAs, and notes such as a shallow lens that was retried).
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
- `profile`: `assertive` (quiet | chill | assertive)
- `mode`: `auto` (auto | quick | standard | deep). See "Review depth".
- `auto.deep_paths`: the crucial-path globs listed under "Review depth". Replacing the list replaces every default.
- `auto` also never goes deep on tests, eval cases, fixtures or docs; see "Review depth".
- `confirm_above_usd`: 5. Above this many USD of pre-run estimate, offer the depth menu; with no terminal, the review is not started. null = never ask. See "Pre-run estimate and depth menu".
- `backend`: `cli`
- `budget_usd`: null. No run cap and no per-call `--max-budget-usd`. Set a number (or pass `--budget-usd N`) to opt in: batches stop once the next one would exceed it, and every model call gets a per-call cap derived from it. `--budget-usd` can add a cap but cannot remove one a repo's config sets; set `budget_usd: null` there instead (on the base branch: it takes effect once merged).
- `call_timeout_s`: 1500. Wall-clock limit for each `claude -p` call. A call that times out fails its lane.
- `max_rounds`: 3. Advisory only: past this round the report prints a "step back" message. It drops nothing, so it stays.
- `verify_cmd`: null
- `rules_file`: `.preflight/RULES.md`
- `guidelines.discover`: true
- `guidelines.ancestors`: true
- `path_filters`: `!**/*.lock`, `!**/node_modules/**`, `!**/.venv/**`, `!**/*.min.*`, `!**/__pycache__/**`
- `path_instructions`: `[]` (items: `{path, instructions}`)
- `docs_review`: `auto` (auto | on | off). See "Docs, prompts and agent instructions" below. `off` restores the 1.0 skip of docs-only diffs.
- `tools.ruff.select`: `[B, S, F, E9, RUF]`
- `tools.ruff.test_ignore`: `[S101, S105, S106, S107, S108, S311]` (assert, hardcoded test passwords, temp paths, non-crypto random). Not reported on `test_paths`; elsewhere they are reported as usual.
- `tools.ruff.test_paths`: `tests/**`, `test/**`, `**/tests/**`, `**/test/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py`
- `tools.gitleaks` reads the repository's own `.gitleaks.toml` when there is one (gitleaks' format: `[extend] useDefault = true` plus `[[allowlists]]`) and passes it with `--config` to both the history scan and the worktree scan. This repo's file allowlists PEM test fixtures whose body is the literal `\nREDACTED\n`; a real key is still reported. The file is read from the working tree, as gitleaks itself does, so a branch can add an allowlist entry; review `.gitleaks.toml` changes like code.
- `tools.gitleaks`: true
- `tools.ast_grep`: true
- `tools.osv`: `auto` (auto | true | false)
- `tools.semgrep`: false
- `tools.zizmor`: true. Audits changed GitHub Actions workflows (`.github/workflows/*.yml`) and `action.yml` files: `zizmor --format json --min-severity medium --offline`, default persona. High → major, medium → minor, category security.
- `tools.actionlint`: false (opt-in). Workflow syntax and expression checks; every finding is minor, category correctness.
- `tools.shellcheck`: false (opt-in). Changed `.sh`/`.bash` files; error → major, warning → minor, info and style → trivial, category correctness.
- `tools.key_consumers.enabled`: true. Adds *candidate* lines to the packet's Tool signals for changed JSON/YAML files; they are leads a finder must check, never findings. Two checks: a key added to a data file that no code reads by name while at least two of its sibling keys are (`"key"`, `['key']`, `.get('key')`, `.key`); and, when `id_patterns` is set, an added string value matching one of those regexes that no data file defines (as a mapping key or an `id` value), or defines only under a `_superseded*` / `deprecated*` container. Only files that existed before the change get the key check.
- `tools.key_consumers.exclude`: CI config, `pyproject.toml`, `package.json`, lockfiles, `docker-compose*`, fixtures, i18n/locales, OpenAPI and JSON Schema files.
- `tools.key_consumers.consumed_whole`: `[]`. Globs for data files read whole (for example injected into a model prompt), which skip the key check.
- `tools.key_consumers.id_patterns`: `[]`. Regexes for id strings to resolve; empty turns the id check off.
- `lenses`: `[correctness, contracts_tests, data_concurrency, rules_compliance, deploy_ci]` (extension modules can register more). `deploy_ci` is path-gated; see "Path-gated lenses" below.
- `auto_lenses`: true. Adds a path-gated lens with `auto` set (`deploy_ci`) whenever a changed file matches its paths, even if `lenses` does not list it, and the lenses and ast-grep rule directories a repo detector switches on (see "Extensions" below). Each addition is noted in the Run section. `false` runs exactly `lenses`.
- `security_lane`: true
- `sweep`: true. Deep mode only: after verification, one fresh finder per run gets the list of every candidate found so far and the list of changed files (plus the packet when the run has one batch; with several batches it reads the changed files itself) and reports only new ones. They are deduplicated by stable id and go through the same votes. A failed sweep marks the run incomplete.
- `walkthrough`: true
- `triage.haiku`: true. A Haiku call per changed file (after the deterministic whitespace, binary and rename checks) approves trivial files before the finders. Prose docs are never approved unless `docs_review: off`.
- `intent.issues`: false
- `nitpicks.per_file`: null (no cap; a number caps nitpicks per file under profile `chill` only)
- `nitpicks.per_run`: null (no cap; a number caps nitpicks per run under profile `chill` only)
- `report_dir`: `.preflight/reviews`
- `concurrency`: 10. Cap on model calls in flight across all packets and verifier votes.
- `packet_tokens`: `{quick: 32000, standard: 24000, deep: 16000}`. Diff tokens per packet, by depth.
- `cache_warm_seconds`: 20
- `learnings.rerank`: true
- `learnings.cap`: 20
- `verifier.cap`: null (every candidate is verified; a number verifies the top N by severity and marks the rest UNKNOWN "over verifier cap")
- `verifier.votes`: 3. Most Sonnet votes per finding: two run first and the rest only when those two disagree (quick mode always uses 1). See "Verification" below.
- `effort.quick` / `effort.standard` / `effort.deep`: `low` / `medium` / `high`. Passed as `claude --effort` to the finder, security, verifier, adjudicator and sweep calls in that mode (low | medium | high | xhigh | max; null = the model's default). A Haiku model never gets an effort level, whatever the stage. Triage, rerank and walkthrough keep the model default.
- `models.*`: see below

Nitpicks are kept in every round; earlier versions dropped them from round 2 on.

Turn limits per call: finders 4 / 6 / 18 (quick / standard / deep), security 8 (18 in deep), verifier 6 (10 in deep). A call that ends on its turn limit is not retried, since the retry would hit the same limit.

## Finders

Finders are recall-first: they report every candidate with a nameable failure scenario and at least one quoted line, including uncertain and low-severity ones, and leave filtering to verification. A failure that needs a specific input is still a candidate when that input can reach the changed code. Each finding may carry `finder_confidence` (1-5), which is logged and never filters; it is separate from the security lane's 0-1 `confidence`.

Every finder result has a `coverage` list: one `{file, symbol_or_hunk, inputs_considered, result}` entry per changed symbol or hunk the lens checked. A lens that returns no findings, lists no file in `coverage` and reads no file outside the packet is **shallow** when the packet holds a non-docs diff, or when the call ended within 2 turns. A shallow lens is retried once on `models.finder_deep` with the deep turn limit, and the Run section notes it (`shallow lens: <lens> ...`, with "retry was also shallow" when the second answer is empty too). A retry that fails marks the lens failed and the run incomplete. The retry is a separate cost entry (`finder:<lens>#retry`).

## Path-gated lenses

A lens can carry a path gate (gitignore-style globs). It runs only on a batch with a changed file that matches one, and the Run section notes `lens '<name>' skipped: no changed file matches its paths` when nothing in the diff does. A gated lens's findings on files inside its gate are exempt from the prose-doc drop below, since reviewing those docs is its job, and a docs-only diff that touches its paths is reviewed by it even under `docs_review: off` (only the gated lens runs then, with no doc-consistency review).

`deploy_ci` (Opus, bug tier) is gated to `.github/workflows/**`, `.github/actions/**`, `action.yml`, `deploy/**`, `k8s/`, `kubernetes/`, `helm/` and `charts/` directories, `kustomization.yaml`, `Dockerfile*`, `docker-compose*.yml`, `*.sh`, `*.bash`, and runbook and deploy docs (`*runbook*`, `DEPLOY*.md`, `deploy*.md`). Its checklist: `workflow_run` deploys that do not check `event == 'push'`, missing `permissions:`, checkout of untrusted code without `persist-credentials: false`, OIDC or workload-identity trust not restricted by ref, RBAC that implies Secret read, immutable Job templates applied before delete, server dry runs the Role cannot do, rollout checks that miss a Deployment, `set image` then `set env`, curl without timeouts or with `|| true` under `set -e`, retry budgets longer than `timeout-minutes`, step outputs interpolated into scripts, and health checks that ignore a condition's status.

## Extensions

Modules in the `preflight_review.extensions` package can register extra lenses (`lens_specs()`) and a repo detector (`detect(repo_root)`) that switches on lenses and extra ast-grep rule directories for a matching repo. Detectors run only when `auto_lenses` is true. Extra rule files load next to `.preflight/ast-rules/`; a rule file with the same name as one already there is skipped, so a repo that installed the same rules is not scanned twice. A detector that raises is logged and skipped.

## Docs, prompts and agent instructions

- Prompt files (`.md`/`.txt`/`.jinja2` under a `prompts/` directory) and agent-instruction files (`SKILL.md`, `CLAUDE.md`, `AGENTS.md`, any `.md` under `.claude/`) change behaviour, so they are risk tier `source` and reviewed like code. A diff of only these files is not a docs-only diff.
- Other `.md`/`.rst`/`.txt` files are prose docs. A finding on a prose doc is kept when its evidence quotes another file changed in the diff (code or doc: doc-code drift, or two changed docs that contradict each other), or quotes the same doc at least twice in category correctness or contracts (an internal contradiction). Under `docs_review: auto|on` a finding anchored on a changed line of the doc is also kept when it quotes another file that exists in the repository, changed or not (new doc text that contradicts unchanged code or config, or names a path that is not there). Otherwise it is dropped as `docs file`. A `path_instructions` entry matching the doc keeps every finding on it.
- `docs_review: auto`: when prose docs are half or more of the changed lines, a doc finding is kept only in correctness, contracts or data_integrity at minor or above, or in any category at major or above; nitpicks are dropped. `on` keeps every doc finding that passes the evidence rule.
- A docs-only diff (prose docs, lockfiles, licences) under `auto` or `on` runs the correctness and contracts_tests lenses (whichever are configured; correctness if neither) with a doc-consistency instruction: cross-doc contradictions, counts and arithmetic, stale status text, commands and paths that do not exist. The security lane does not run on it. A diff of only lockfiles and licences, or any docs-only diff under `off`, still skips the model stages.
- With `triage.haiku: true`, Haiku never approves a prose doc unless `docs_review: off`.

## Secret files

A built-in `secret-files` check runs on every review, with or without gitleaks. It flags any changed, non-deleted file named `.env` or `.env.*` (except templates, whose last suffix is `example`, `sample`, `template`, `dist` or `defaults`, such as `.env.example` or `.env.local.example`), a `*.pem` holding a `PRIVATE KEY` block (public certificate and CA files are not flagged), `*.p12`, `*.pfx`, `id_rsa` / `id_dsa` / `id_ecdsa` / `id_ed25519`, `credentials.json`, `*service-account*.json`, or a `.json` whose `type` is `service_account`. Files the path filters excluded are checked too. Each finding is critical, category secrets, always inline (never hidden as pre-existing), never clustered into another finding, and skips the verifier like gitleaks. File contents are never quoted; only a `.pem`'s first 64 KB (for the key marker) and a `.json`'s `type` field are read. To silence a deliberate test key, add a learning (`do not flag ...`) for its path.

## Models per stage

Cheap models by default, Opus only for very critical work (user cost decision, 2026-11-07; decision 15). **This deviates from Anthropic's open-source `/code-review` plugin**, which finds and validates bug findings on Opus: an exhaustive Opus run of a 74-file change planned 1214 calls and hit the usage limit. Haiku does the cheap steps and never decides whether a finding is real.

| Config key | Default | Role | Source |
|---|---|---|---|
| `finder` | sonnet | Every lens in standard and quick mode: correctness, contracts_tests, data_concurrency, deploy_ci and any opt-in domain lens | User cost decision 2026-11-07 (the plugin uses Opus for bug finders) |
| `finder_deep` | sonnet | Replaces `finder` in deep mode, and runs the shallow-lens retry; deep differs by turns, effort and sweep | Same |
| `finder_rules` | sonnet | The rules_compliance lens | Anthropic `/code-review` plugin: Sonnet checks CLAUDE.md compliance |
| `security` | sonnet | The security lane | User cost decision (the `security-guidance` plugin uses the newest Opus) |
| `verifier` | sonnet | Every verifier vote, bug or security (security findings use the security verifier prompt) | User cost decision (the plugin validates bug findings on Opus); the vote scheme is ours |
| `adjudicator` | opus | One call that settles an escalated split: a critical finding without a unanimous vote, or a security finding at major or above with no majority | Ours. `fable` works too if your account has it |
| `adjudicator_fallback` | opus | Re-runs the same adjudication when the `adjudicator` call fails (unknown model, missing entitlement, a provider alias that points elsewhere, a refusal, or any other error); skipped when it equals `adjudicator` | Ours |
| `verifier_major` | opus | Unused since 1.1 (every vote runs on `verifier`); kept so older configs load | — |
| `sweep` | sonnet | The deep-mode gap sweep, once per run | Anthropic's bundled `/code-review` max recipe ends with a sweep phase |
| `triage` | haiku | Skips trivial files before the finders (`triage.haiku: true`) | Anthropic `/code-review` plugin: Haiku runs only the skip gate and the CLAUDE.md lister |
| `rerank` | haiku | Reranks saved learnings for the packet | Ours, following the plugin's rule that Haiku does only cheap steps |
| `walkthrough` | haiku | The report's walkthrough summary | User cost decision (the plugin's summary step runs on Sonnet) |

The manifest's `models` lists the stage models used, plus `adjudicated_by` with the model that actually settled each split (for example `opus (2)`). The Run section's Cost line shows calls and USD by stage and by model, so any Opus use is visible, for example `Cost: USD 1.0330 — by stage: triage 1 call $0.00, finder 2 calls $0.30, ...; by model: haiku 2 calls $0.00, sonnet 6 calls $0.63, opus 1 call $0.40`.

## Verification

Each vote judges the input the finding names: CONFIRMED needs a quote showing the failure path for that input; a finding that names no concrete input, or whose input cannot reach the changed line, is REFUTED rather than UNKNOWN. A finding is never refuted only because it depends on a specific input.

What reaches the verifier: model findings at minor severity or above outside the nitpick lane. The rest never get a model vote:

- **Tool findings** (gitleaks, osv-scanner, secret-files, ruff, ast-grep, zizmor, actionlint, shellcheck) are reported as tool-verified (verdict SKIPPED, `Verifier: SKIPPED — deterministic tool finding (tool-verified, no model vote)`). A tool hit that clusters with a model finding goes to the verifier as evidence on that finding.
- **Nits** (severity trivial, or lane nitpick, from any model lens or the co-change check) are reported among the findings labelled `unverified-nit`, counted separately from confirmed findings (`N confirmed, K unverified nits, ...`), and never counted as verifier failures. Under `profile: chill` the nitpick caps apply to them.

Votes, in every mode but quick (`verifier.votes: 3`, all on `models.verifier`, Sonnet):

1. Two votes run in parallel. When both return the same verdict, that is the result; no third call.
2. Otherwise (they disagree, or one failed) the remaining votes run, and the verdict with more than half of the votes that returned decides.
3. Escalation: a critical finding whose votes are not unanimous, or a security finding at major or above with no majority, goes to one `adjudicator` call (Opus), which re-reads the code in a fresh context and returns CONFIRMED, REFUTED or UNKNOWN under the same evidence rule.
4. Any other finding without a majority is UNKNOWN (the Unverified section), with no Opus call.

Quick mode takes one Sonnet vote and never escalates. Each vote is scored on its own: CONFIRMED or REFUTED without a quoted line counts as UNKNOWN, and a security CONFIRMED needs `confidence_10` of 8 or more.

The tally is written into the verifier reason the report shows, for example `votes 2/2 CONFIRMED (sonnet): ...`, `votes 2/3 CONFIRMED, 1 REFUTED (sonnet): ...`, `votes 1/3 CONFIRMED, 1 REFUTED, 1 UNKNOWN (sonnet): no majority; not escalated ...` or `votes 2/3 CONFIRMED, 1 REFUTED (sonnet); adjudicated by opus: REFUTED — ...`.

- A vote that fails is left out of the tally. Only when every vote fails is the finding a verifier failure (UNKNOWN, severity lowered one step, run marked incomplete).
- When the adjudicator and its fallback both fail, the finding is UNKNOWN without a severity change, and the run is marked incomplete.
- Severity is lowered only when every vote behind the verdict lowered it, and then by the smallest step any of them asked for.
- Every vote and adjudication is its own entry in the run's cost (`verifier:<id>#v1..#vN`, `adjudicator:<id>`, `adjudicator:<id>#fallback`).
- An UNKNOWN from the adjudicator leaves the finding unverified.
- The verdict cache stores the aggregated result, keyed also by vote count and verifier model, so verdicts from another setup are not reused. Results with a failed vote or adjudication are not cached.

The security verifier prompt keeps Anthropic's hard exclusions (labelled E1-E18) and precedents (P1-P12), with five overrides that take precedence: a secret or env file committed in the diff is not "stored on disk, otherwise secured" (E2) and is not covered by "environment variables are trusted" (P3); untrusted content that reaches a system or instruction prompt, a tool-using agent, or model output the code trusts to decide something is not covered by "user content in AI prompts" (E14); a wildcard or reflected CORS origin with credentials allowed is a vulnerability, not "a lack of hardening" (E7); CI, deploy and cluster trust findings (an unchecked `workflow_run` or `pull_request_target` deploy, missing `permissions:`, persisted checkout credentials, an unrestricted OIDC binding, RBAC that can create Jobs or Pods, runbook commands) set aside E6 and E17 and P7 and P12 (but not E7: an extra gate such as environment protection, approval or a concurrency group on a workflow whose missing trigger check is already reported stays a hardening exclusion); and a logged URL or DSN carrying a password, token or key is a logged secret, not a URL (P1). The seeded cases `committed_env_file`, `prompt_injection_grade` and `cors_wildcard_credentials` cover them.

## Measured cost and time

All figures are notional list-price cost under subscription OAuth, not a charge. Full log: `docs/measurements.md`.

| Run | Diff | Cost | Time | Result |
|---|---|---|---|---|
| Demo (shell=True case) | 1 file, +2/−1 | $0.19 | 33 s | 1 confirmed, true positive |
| Seeded eval (8 cases) | 1 file each | $1.43 total | 3 m 30 s | recall 7/7, precision 9/9, 0 on the benign control |
| Run 7 self-review | 202 files, ~17k lines | $4.80 (budget $5) | 168 s | 52/202 reviewed (all `src/`), incomplete, 1 confirmed true positive |

These runs predate 1.1 (mode standard, with a run budget). The 1.1 seeded eval with the earlier exhaustive defaults (Opus finders, 3 Opus votes, deep mode, 30 cases) cost $26.94; see `docs/measurements.md`. The lean defaults (Sonnet finders and votes, Opus only on escalated splits, one sweep per run, tool findings and nits unverified) have not been measured yet.

### Opting back into caps

1.1 has no coverage caps by default; its cost levers are the model choices and the depth menu. Each one can be turned back on. Config keys are read from `.preflight/config.yaml` on the base branch, so a change takes effect once it is merged there; for this run use the flags (`--budget-usd`, `--mode`).

- Run budget: `--budget-usd N` (this run), or `budget_usd: N` in `.preflight/config.yaml` on the base branch. This also gives every model call a per-call `--max-budget-usd`.
- Verifier: `verifier.cap: N` verifies only the first N candidates by severity, inline lane first (the rest are reported as not verified); `verifier.votes: 1` takes one Sonnet vote per finding (no third vote, no escalation).
- Nitpicks: `nitpicks.per_file` and `nitpicks.per_run`, which apply under `profile: chill`.
- Breadth: `--mode standard` or `--mode quick` (quick also uses one vote), `sweep: false`, `profile: chill`. `mode: auto` (the default) already runs standard unless a crucial path changed.
- Asking first: `confirm_above_usd: N` sets the estimate above which the depth menu appears (or, without a terminal, the run does not start).
- Models: set any key under `models:`, for example `finder: opus` to go back to Opus finders (Anthropic's plugin practice) or `adjudicator: fable`.

## Eval

```sh
preflight-review eval --set seeded [--backend cli|fake] [--no-judge] [--judge-models sonnet,opus] [--cases-dir DIR] [--out eval/results]
preflight-review eval --set clean  [--backend cli|fake]
preflight-review eval --set golden --repo-path <checkout of the reviewed repo> [--backend cli|fake]
preflight-review eval --set seeded --compare-bundled
```

- `seeded`: each case is `eval/seeded/cases/<name>/{before.py,after.py,expected.json}` (or a multi-commit tree, see `eval/README.md`). Measures whether a known bug is found.
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
- `ruff`, `ast-grep` or `zizmor` missing: the tool reports "not installed" and the review continues. When the diff had work for it, the report is marked `DEGRADED`. Install with `uv tool install ruff`, `brew install ast-grep` or `uv tool install zizmor`.
- `gh` missing or no open PR: the report filename uses `PR-<branch-slug>` instead of `PR<##>`.
- Exit code `1`: the run was incomplete (a lane failed, files were not reviewed, or a budget cap ran out, from `--budget-usd` or `budget_usd`), not started (estimate above `confirm_above_usd`; re-run with `--yes` or a lighter `--mode`), paused (Claude usage limit; run `preflight-review review --resume` after the reset), or an error occurred (one-line message). The report is still written for incomplete, paused and not-started runs. There is no exit code 2.

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
