---
name: preflight
description: Run the local preflight code review on the current branch and point to the report file for an agent to work on
argument-hint: "[--base origin/main] [--full] [--mode quick|standard|deep] [--committed-only]"
disable-model-invocation: true
allowed-tools: "Bash(preflight-review *), Bash(preflight *), Bash(uv tool *), Bash(uv run *), Read"
---

# preflight

requires: preflight-review >= 1.0.2

Report-only review of the current branch. Arguments: `$ARGUMENTS`.

1. Run `preflight-review version --json`. If the command is missing or the version is below 1.0.2, run `uv tool install --force git+https://github.com/AdinathChaudhari/preflight-review`.
2. If `.preflight/config.yaml` is missing, run `preflight-review init`.
3. Run `preflight-review review $ARGUMENTS --json`. Capture the printed `.md` report path from the JSON line.
4. Read the report `.md`. Print the summary line, each finding id with its severity and `file:line`, and the absolute report path.
5. State explicitly: "This skill does not fix code. To act on the report, open a separate agent on `<path>`; after fixing, call `preflight-review respond F<n> fixed|wrong|intentional|didnt_matter "reason"`." Also mention that exit code 1 means the run was incomplete (a lane failed, the budget ran out, or files were not reviewed: see the report's Not reviewed section); the report is still written.
