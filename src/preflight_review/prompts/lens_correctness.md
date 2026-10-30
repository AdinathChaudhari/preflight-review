<!-- preflight-review prompt: lens_correctness v2 | adapted from: anthropics/claude-plugins-official plugins/code-review (Apache-2.0) | retrieved 2026-10-30 -->
Lens: correctness and root cause.

Find inputs or states where the changed code gives a wrong result, raises, corrupts data, or breaks a caller listed in the packet. Then ask of each fix or workaround in the change: does it address the root cause, or only hide the symptom? Report a symptom-only fix when you can show the cause still fires.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote. Also return files_read_outside_packet.

Return {"findings": [], "files_read_outside_packet": []} if nothing meets the bar.
