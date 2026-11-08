---
name: preflight
description: Run the local preflight code review on the current branch and point to the report file for an agent to work on
argument-hint: "[--base origin/main] [--full] [--mode auto|quick|standard|deep] [--yes] [--resume] [--committed-only]"
disable-model-invocation: true
allowed-tools: "Bash(preflight-review *), Bash(preflight *), Bash(uv tool *), Bash(uv run *), Read"
---

# preflight

requires: preflight-review >= 1.1.0

Report-only review of the current branch. Arguments: `$ARGUMENTS`.

1. Run `preflight-review version --json`. If the command is missing or the version is below 1.1.0, run `uv tool install --force git+https://github.com/AdinathChaudhari/preflight-review`.
2. If `.preflight/config.yaml` is missing, run `preflight-review init`.
3. Run `preflight-review review $ARGUMENTS --json`, passing the user's arguments through unchanged. The default `--mode auto` runs a deep review when a crucial path changed (migrations, auth, secrets, CI/deploy, prompts, scoring/billing; tests, eval cases, fixtures and docs never count) and a standard one otherwise, per packet: only packets holding a crucial file go deep. Reviews are incremental by default: file changes already reviewed under the same prompts are skipped and listed under "Reviewed earlier"; pass `--full` only if the user asks to review everything again. A started review is never stopped for cost. Pass `--mode quick` or `--budget-usd N` only if the user asked for a faster or capped run, and never add `--yes` yourself. Capture the `.md` report path and the `outcome` from the JSON line.
4. If `outcome` is `not_started`, no model was called: the estimate was above `confirm_above_usd` and this skill has no terminal for the depth menu. Read the report and show the user its per-depth table (quick, standard, deep: estimated USD, calls and minutes each) and the command it lists for each (`--mode quick|standard|deep --yes`). Ask which depth they want, and re-run `/preflight` with that `--mode <depth> --yes` only after they choose. Stop there until they do.
4a. If `outcome` is `paused`, the Claude usage limit was reached mid-run: preflight stopped calling the model, so the remaining lanes are marked `paused: usage limit` and some candidates stay Unverified. Read the report, print its `Result: PAUSED ...` line (reset time, calls done, resume command), any findings it already has, and tell the user to run `/preflight --resume` (with the same other arguments) after the reset. That reuses every finished call and runs only the missing ones; if the diff changed meanwhile it runs a normal review instead. Do not re-run it yourself before the reset.
5. Otherwise read the report `.md`. Print the summary line, each finding id with its severity and `file:line`, and the absolute report path.
6. State explicitly: "This skill does not fix code. To act on the report, open a separate agent on `<path>`; after fixing, call `preflight-review respond F<n> fixed|wrong|intentional|didnt_matter "reason"`. After committing the fixes, run `/preflight` again before pushing (it reviews only what changed): a fix commit can leave docs written earlier in the branch describing the old design." Also mention that exit code 1 means the run was incomplete (a lane failed, files were not reviewed, or a `--budget-usd` cap ran out: see the report's Not reviewed section) not started (step 4) or paused by the usage limit (step 4a); the report is still written.
