# Provenance

preflight-review is MIT licensed (see [LICENSE](../LICENSE)). Third-party
attributions and the required licence notices are in [NOTICE](../NOTICE).

| Source | Licence | Used in | How |
|---|---|---|---|
| anthropics/claude-plugins-official, `plugins/code-review` | Apache-2.0 | `system.md`, `lens_correctness.md`, `lens_contracts_tests.md`, `lens_rules_compliance.md`, `verifier.md` | Prompt text adapted, rewritten and extended |
| anthropics/claude-code-security-review | MIT | `security.md`, `verifier_security.md` | Prompt text adapted |
| CodeRabbit | Not applicable (no code copied) | `render/agent_prompt.py` preamble, importer severity/category parsing | Public output formats and categories studied and paraphrased |
| PR-Agent, Aider, coderabbitai/ai-pr-reviewer | Not applicable (no code copied) | Context engine (hunk extension, repo-map ranking), file triage | Ideas reimplemented from scratch |
| Original work | MIT | `rerank.md`, `walkthrough.md`, `propose.md`, `judge.md`, `triage.md`, `lens_data_concurrency.md` | Written for this project |

## Prompt header convention

Every file in `src/preflight_review/prompts/` starts with a one-line marker
beginning `adapted from:`. It names the upstream source and its licence, or
says the prompt is original. When you adapt a new upstream prompt, add its
entry here and in NOTICE before merging.
