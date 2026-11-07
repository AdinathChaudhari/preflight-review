<!--
GitHub metadata — authored here, applied by hand / via gh in repo Settings (NOT rendered on the page):
About:  Find, verify & report bugs in your branch before it reaches GitHub — a local CLI, Claude Code skill & pre-push hook running parallel Claude reviewers via claude -p.
Topics (ranked): code-review, ai-code-review, claude-code, claude-code-plugin, pre-push-hook, git-hooks, security-review, static-analysis, coderabbit-alternative, llm, claude, anthropic, python, cli, gitleaks, ruff, developer-tools, macos
Social preview: docs/media/hero-glass.jpg (a compressed copy of docs/media/hero.png, not committed), uploaded by hand.
-->

# preflight-review — Pre-push AI Code Review

**Catch the bug before it reaches GitHub.**  
*A local reviewer that runs parallel Claude reviewers over your branch, verifies every candidate against the real code, and writes one report a fixing agent can act on.*

![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Version 1.1.0](https://img.shields.io/badge/version-1.1.0-informational)

<img src="docs/media/hero.png" width="720" alt="preflight-review: a paper airplane on a runway of code lines beside a checklist">

It takes CodeRabbit's thoroughness (linters, callers and callees, rules files, saved lessons) and Anthropic's review discipline (find, then verify in a fresh context, then report only what was confirmed). It never fixes code and never blocks a push: its only output is `<repo>-PR<##>-preflight-review-<##>.md`.

## The map

| I want to… | Use | What happens |
|---|---|---|
| **Review my branch now** | `preflight-review review --base main` | Writes the report to `.preflight/reviews/` and prints its path |
| **Review from Claude Code** | [`/preflight`](skills/preflight/SKILL.md) | Runs the review and lists each finding with `file:line` |
| **Review on every push** | `preflight-review install-hook` | Writes a report on `git push`; the push always goes through |
| **Teach it a false alarm** | `preflight-review respond F2 wrong "reason" --learn` | Saves a lesson so the finding does not come back |
| **Measure it** | `preflight-review eval --set seeded` | Scores recall and precision on planted bugs |

## Quick start

```bash
uv tool install git+https://github.com/AdinathChaudhari/preflight-review
cd your-repo && preflight-review init
preflight-review review --base main
```

1. **Optional: install the Claude Code plugin.** Run `claude plugin marketplace add AdinathChaudhari/preflight-review`, then `/plugin install preflight@preflight-review` inside Claude Code.
2. **Log in to Claude Code** first (`claude`, then `/login`). It uses your subscription; no API key.
3. **Open the report** at `.preflight/reviews/latest.md`, or hand it to an agent: "fix the findings in `.preflight/reviews/latest.md`".

That's it — everything below is detail.

---

## Demo

A real run on a branch that switched a `subprocess.run` call to `shell=True` with an f-string:

```
$ preflight-review review --base main
preflight: complete: 1 confirmed (1 major+), 0 unverified → .preflight/reviews/demo-repo-PR-feature-export-archive-preflight-review-01.md

$ sed -n '/^### F1/,/^Suggested fix/p' .preflight/reviews/latest.md
### F1 · Major · Security · quick win · app/tools/export.py:6-10

Title: Avoid shell=True with interpolated report_id and dest_dir (command injection)
Claim: The command is built with an f-string and run through the shell, so unquoted report_id/dest_dir are interpreted by the shell.

Evidence:
- `app/tools/export.py:7` `        f"pdfgen --id {report_id} --out {dest_dir}",`
- `app/tools/export.py:8` `        shell=True,`
- Verifier: CONFIRMED — The code matches the claim. This change replaced the list-form argv call with an f-string run under shell=True ...

Why it matters: Arbitrary command execution and broken behavior for paths with spaces.
Suggested fix: Revert to the list form without a shell: subprocess.run(["pdfgen", "--id", str(report_id), "--out", dest_dir], check=True).
```

7 model calls, $0.19, 33 s on 1.0 (standard mode, one vote); for 1.1's defaults see Cost and time below. Every finding also carries a stable id and a ready-to-paste "Prompt for AI agent" block.

---

## How it works

1. **Scope** — diff against the merge base (committed, staged, unstaged, untracked), path filters, config and rules read from the **base branch** so a branch cannot rewrite its own review rules.
2. **Deterministic tools** — gitleaks, osv-scanner, ruff, ast-grep and zizmor (GitHub Actions) when installed, with actionlint and shellcheck as opt-ins, plus a built-in check that flags any committed `.env`, private key or credential file. Their findings are reported as tool-verified and never sent to a model verifier; ruff's test-noise rules (assert, hardcoded test passwords, temp paths, non-crypto random) are not reported on test files. A tool that should have run but is missing or fails marks the report `DEGRADED`.
3. **Context packet** — changed hunks with the enclosing function, callers and callees, co-changed companions, CLAUDE.md / AGENTS.md (including parent folders), saved lessons. Large diffs are split into batches, **riskiest files first**, and the packet is a shared cached prefix so extra lenses cost cents.
4. **Find** — a Haiku triage call per file first approves trivial files. Then four Sonnet lenses (correctness, contracts and tests, data and concurrency, your rules); a Sonnet `deploy_ci` lens that runs only when the diff touches workflows, deploy manifests, Dockerfiles, shell scripts or runbooks; and a Sonnet security lane that must trace source to sink. Deep mode gives them more turns and higher effort, not a bigger model. Finders aim for recall: they report every candidate with a concrete failing input and leave filtering to the verifier. An empty answer must list what it checked; a lens that answers empty without reading anything is retried once.
5. **Verify** — fresh Sonnet agents read the code and vote CONFIRMED, REFUTED or UNKNOWN: two votes run first and, when they agree, that is the verdict; otherwise a third vote runs and the majority decides. Only a split on a critical finding (or a security finding at major or above) goes to one Opus adjudicator; any other split is reported as Unverified. Quick mode uses one Sonnet vote. Trivial and nitpick findings skip the verifier and are reported as unverified nits. In deep mode one Sonnet sweep per run then looks for anything the lenses missed, and its finds go through the same votes. Everything dropped goes to `.dropped.jsonl`.
6. **Policy and report** — pre-existing issues hidden, no repeats across rounds, then the `.md`, `.json` and manifest are written.

| | CodeRabbit idea | Anthropic idea |
|---|---|---|
| **Inputs** | linters, code graph, guidelines, learnings | — |
| **Output filter** | — | verify every finding, hide pre-existing, 3-round cap, no style noise |

---

## The report

| Section | Holds |
|---|---|
| **Scope line** | files reviewed N/M, what was not reviewed and why, result `complete` or `incomplete` |
| **Merge risk** | `unknown` whenever any file was not reviewed |
| **Findings** | severity · category · effort · `file:line`, quoted evidence, verifier verdict, fix, agent prompt |
| **Tool output and Run** | tool versions and status (`DEGRADED` when a needed tool did not run); models, calls, cost, prompt and packet SHAs |

Filename: `<repo>-PR<##>-preflight-review-<##>.md`. `PR<##>` is the open PR's number, or `PR-<branch-slug>` before a PR exists; the last number is the review round. A `.json` twin and `latest.md` sit beside it. Exit code `0` = complete, `1` = incomplete, paused or error; there is no blocking exit code. When the Claude usage limit is hit mid-run, the report says `PAUSED` and `preflight-review review --resume` finishes it after the reset, reusing every finished call.

---

## Requirements

```bash
# macOS
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv
brew install gitleaks ast-grep osv-scanner          # optional tool lanes
uv tool install zizmor                              # optional: GitHub Actions audit
uv tool install ruff                                # optional tool lane
```

Claude Code 2.1+ logged in with a subscription, git, Python 3.11+. `gh` is optional (used for the PR number and `import-coderabbit`).

### Cost and time

The defaults are lean: cheap models do the bulk of the work and Opus only settles disputes on critical findings. Finders, the security lane and the sweep run on Sonnet; votes run on Sonnet (two, plus a third only when they disagree); triage, learnings rerank and the walkthrough run on Haiku. Tool findings (ruff, gitleaks, zizmor and the rest) never reach a model verifier, and trivial or nitpick findings are reported as unverified nits. Nothing is cut short once a review starts: profile `assertive`, no run budget, no per-call cap, no nitpick caps.

**Auto depth.** The default mode `auto` runs `deep` when a crucial path changed (migrations, auth and security, secrets and env files, CI and deploy, prompts and agent instructions, scoring and billing code; see `auto.deep_paths`) and `standard` otherwise. Tests, eval cases, fixtures and docs never count: a migration under `tests/fixtures/` or `eval/` does not make the run deep. Deep mode differs from standard by more turns, `high` effort and one sweep per run.

**The pre-run menu.** Before the first model call preflight prints a rough estimate with its planned calls, for example `~$1.9, ~7 min for 10 files in standard mode (2 batches; ~36 calls: 8 finders, 2 security, ~15 verify (~1 adjudicated), 10 triage, 1 walkthrough; rough)`. Above `confirm_above_usd` ($5) it offers a choice instead of running:

```text
preflight: 74 files, 10 batches. Choose depth:
  1) quick     ~$4.2   ~165 calls  ~13 min  (Sonnet finders at low effort, 1 Sonnet vote, no sweep)
  2) standard  ~$11    ~206 calls  ~35 min  (Sonnet finders at medium effort, up to 3 Sonnet votes, opus on critical splits, no sweep)
  3) deep      ~$16    ~211 calls  ~59 min  (Sonnet finders at high effort, up to 3 Sonnet votes, opus on critical splits, sweep)  (auto)
  4) cancel
Choose [3]:
```

Enter runs the mode `auto` chose. Where it cannot ask (hook, skill, CI) it does not start; the "not started" report lists the same per-mode estimates and the command for each (`--mode quick|standard|deep --yes`). `--yes` skips the menu. The Run section of each report has a `Cost` line with calls and USD by stage and by model (Haiku, Sonnet, Opus). Cost is notional list price under a Claude subscription, not a charge.

The estimate is an order of magnitude, fitted to Opus-era runs and scaled by model price. Measured on 1.1 with the earlier exhaustive defaults (Opus finders, 3 Opus votes): the 30-case seeded eval in deep mode cost $26.94, and a 74-file deep run planned 1214 calls and hit the usage limit; 773 of them were Opus votes on ruff findings. The lean defaults have not been re-measured yet.

To cap a run, pass `--budget-usd 5`, or set `budget_usd` in `.preflight/config.yaml` on the base branch (config is read from the base branch, so an edit on your branch takes effect once it is merged; use `--budget-usd` for this run). A capped run that runs out stops early, marks the run `incomplete` and lists every unreviewed file. Other levers (`--mode standard`, `verifier.votes`, `verifier.cap`, `sweep: false`, `models.*`): [`docs/reference.md`](docs/reference.md#opting-back-into-caps).

---

## FAQ

**Does it fix my code?**
No. It writes a report and stops. Give `.preflight/reviews/latest.md` to a separate agent, then record the outcome with `preflight-review respond F1 fixed`.

**Does it send my code anywhere?**
Only to Claude, through your own `claude -p` login, the same as any Claude Code session. `gh` is asked only for the branch's PR number.

**How good is it?**
Seeded eval on 1.0: 7 of 7 planted bugs found, 9 of 9 findings real, 0 findings on the benign control ($1.43). The seeded set has grown to 25 planted bugs and 5 clean controls for 1.1 and has not been re-run on real models yet. Recall on large real diffs is not measured yet; see `docs/measurements.md`.

**Why not just use the built-in `/code-review`?**
It does not run linters, persist findings across rounds, hide pre-existing issues or write a report file for another agent. `eval --set seeded --compare-bundled` runs both side by side.

---

## Status and docs

v1.1.0, public, MIT licence ([LICENSE](LICENSE); third-party attributions in [NOTICE](NOTICE)). Changes: [`CHANGELOG.md`](CHANGELOG.md). Full command, config and eval reference: [`docs/reference.md`](docs/reference.md). Design: [`docs/design.md`](docs/design.md). Measurements: [`docs/measurements.md`](docs/measurements.md).

Built on [Claude Code](https://claude.com/claude-code), [gitleaks](https://github.com/gitleaks/gitleaks), [osv-scanner](https://github.com/google/osv-scanner), [ruff](https://github.com/astral-sh/ruff), [ast-grep](https://ast-grep.github.io) and [rank-bm25](https://github.com/dorianbrown/rank_bm25).
