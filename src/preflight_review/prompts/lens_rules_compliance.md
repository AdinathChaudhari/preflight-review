<!-- preflight-review prompt: lens_rules_compliance v2 | adapted from: anthropics/claude-plugins-official plugins/code-review (Apache-2.0) | retrieved 2026-10-30 -->
Lens: rules compliance.

Report ONLY a changed line that breaks a written guideline in this repo (CLAUDE.md, AGENTS.md, .claude/rules/*.md, .preflight/RULES.md, or a path instruction from the packet).

Hard requirements, for every finding:
- `rule_quote` is REQUIRED. Copy the exact guideline sentence, word for word. A finding without `rule_quote` is discarded.
- Name the guideline file path where the sentence appears (for example "CLAUDE.md" or "app/CLAUDE.md") in the claim.
- Scope: a guideline file in directory X applies only to files under X. A root-level guideline applies to the whole repo. Do not apply a rule from one directory to files elsewhere.
- Category is "rules". Severity follows how the guideline words the rule (a "must" or "never" is major; a "prefer" is minor or trivial).

Do not report a rule that is explicitly silenced in the code, a rule a linter enforces, or a rule you can only paraphrase.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote. Also return files_read_outside_packet.

Return {"findings": [], "files_read_outside_packet": []} if nothing meets the bar.
