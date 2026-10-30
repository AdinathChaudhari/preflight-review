<!-- preflight-review prompt: lens_data_concurrency v2 | adapted from: original (preflight-review), bug classes informed by public CodeRabbit review categories | retrieved 2026-10-30 -->
Lens: data integrity, concurrency and error handling.

Flag only these, and only when the changed code introduces or exposes them.

Data integrity (category "data_integrity"):
- A model column added, renamed or removed with no Alembic migration, or a migration with no matching model change. Example class: a model column (`cases.islatest`) missing from the initial migration.
- A migration without a working `downgrade`, or one that hardcodes a constraint or index name that can differ on PostgreSQL (the auto-generated name differs from the SQLite one). Example class: a constraint-name cleanup migration that fails on PostgreSQL.
- A migration that rebuilds a table while SQLite foreign-key enforcement is on.

Concurrency (category "concurrency"):
- A SELECT-then-UPDATE job or outbox claim without `FOR UPDATE SKIP LOCKED` or a compare-and-set `UPDATE ... WHERE status = :expected`, so two workers can process the same row. Example class: a non-atomic reply-outbox job claim.
- A lifecycle or status transition (for example under concurrent PATCH requests) that reads the state, checks it, then writes it without a lock or a conditional update.

Error handling (category "error_handling"):
- A swallowed exception (bare `except`, `except Exception: pass`, or a handler that logs and continues when the caller needs the failure).
- Raw database exception text logged or returned to the client. Example class: an ops endpoint that logs the raw DB error.
- A dropped `await` on a coroutine, or a task created and never awaited or kept.

Each finding must show the concrete interleaving, input or schema state that breaks. Do not flag theoretical races on data that only one process touches.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote. Also return files_read_outside_packet.

Return {"findings": [], "files_read_outside_packet": []} if nothing meets the bar.
