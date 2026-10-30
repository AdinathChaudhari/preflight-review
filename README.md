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
![Version 1.0.2](https://img.shields.io/badge/version-1.0.2-informational)

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

7 model calls, $0.19, 33 s. Every finding also carries a stable id and a ready-to-paste "Prompt for AI agent" block.

---

## How it works

1. **Scope** — diff against the merge base (committed, staged, unstaged, untracked), path filters, config and rules read from the **base branch** so a branch cannot rewrite its own review rules.
2. **Deterministic tools** — gitleaks, osv-scanner, ruff and ast-grep when installed. Their output is context, never restated as findings.
3. **Context packet** — changed hunks with the enclosing function, callers and callees, co-changed companions, CLAUDE.md / AGENTS.md (including parent folders), saved lessons. Large diffs are split into batches, **riskiest files first**, and the packet is a shared cached prefix so extra lenses cost cents.
4. **Find** — four Sonnet lenses (correctness, contracts and tests, data and concurrency, your rules) plus an Opus security lane that must trace source to sink.
5. **Verify** — a fresh agent per candidate (Opus for major and above) reads the code and returns CONFIRMED, REFUTED or UNKNOWN. Only confirmed findings are reported; everything dropped goes to `.dropped.jsonl`.
6. **Policy and report** — pre-existing issues hidden, no repeats across rounds, nitpicks capped, then the `.md`, `.json` and manifest are written.

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
| **Tool output and Run** | tool versions and status; models, calls, cost, prompt and packet SHAs |

Filename: `<repo>-PR<##>-preflight-review-<##>.md`. `PR<##>` is the open PR's number, or `PR-<branch-slug>` before a PR exists; the last number is the review round. A `.json` twin and `latest.md` sit beside it. Exit code `0` = complete, `1` = incomplete or error; there is no blocking exit code.

---

## Requirements

```bash
# macOS
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv
brew install gitleaks ast-grep osv-scanner          # optional tool lanes
uv tool install ruff                                # optional tool lane
```

Claude Code 2.1+ logged in with a subscription, git, Python 3.11+. `gh` is optional (used for the PR number and `import-coderabbit`).

> **Budget gotcha.** The default `budget_usd` is 1.50. A typical 5–20 file branch fits; a branch touching hundreds of files stops early, marks the run `incomplete`, and lists every unreviewed file. Raise it per run with `--budget-usd 5`.

---

## FAQ

**Does it fix my code?**
No. It writes a report and stops. Give `.preflight/reviews/latest.md` to a separate agent, then record the outcome with `preflight-review respond F1 fixed`.

**Does it send my code anywhere?**
Only to Claude, through your own `claude -p` login, the same as any Claude Code session. `gh` is asked only for the branch's PR number.

**How good is it?**
Seeded eval: 7 of 7 planted bugs found, 9 of 9 findings real, 0 findings on the benign control ($1.43). Recall on large real diffs is not measured yet; see `docs/measurements.md`.

**Why not just use the built-in `/code-review`?**
It does not run linters, persist findings across rounds, hide pre-existing issues or write a report file for another agent. `eval --set seeded --compare-bundled` runs both side by side.

---

## Status and docs

v1.0.2, public, MIT licence ([LICENSE](LICENSE); third-party attributions in [NOTICE](NOTICE)). Full command, config and eval reference: [`docs/reference.md`](docs/reference.md). Design: [`docs/design.md`](docs/design.md). Measurements: [`docs/measurements.md`](docs/measurements.md). Prompt provenance: [`docs/licences.md`](docs/licences.md). Changelog: [`CHANGELOG.md`](CHANGELOG.md).

Built on [Claude Code](https://claude.com/claude-code), [gitleaks](https://github.com/gitleaks/gitleaks), [osv-scanner](https://github.com/google/osv-scanner), [ruff](https://github.com/astral-sh/ruff), [ast-grep](https://ast-grep.github.io) and [rank-bm25](https://github.com/dorianbrown/rank_bm25).
