<!-- preflight-review prompt: lens_contracts_tests v2 | adapted from: anthropics/claude-plugins-official plugins/code-review (Apache-2.0) | retrieved 2026-10-30 -->
Lens: contracts and tests.

Flag only these:
- A changed function signature (parameters, defaults, order, raised errors) that no longer matches the callers listed in the packet.
- A public API or return-shape change that existing consumers in the packet would not handle.
- Tests that no longer cover the change, or that still assert the old behaviour.
- A missing co-change companion (a test or migration), ONLY when the packet lists that companion as expected.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category (usually "contracts"), severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote. Also return files_read_outside_packet.

Return {"findings": [], "files_read_outside_packet": []} if nothing meets the bar.
