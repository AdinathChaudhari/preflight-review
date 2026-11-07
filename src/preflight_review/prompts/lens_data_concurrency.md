<!-- preflight-review prompt: lens_data_concurrency v4 | adapted from: research/v1/round2/02-real-output-golden-set.md; preflight miss sweep 2026-11-06 | retrieved 2026-11-06 -->
Lens: data integrity, concurrency and error handling.

Flag only these, and only when the changed code introduces or exposes them.

Data integrity (category "data_integrity"):
- A model column added, renamed or removed with no Alembic migration, or a migration with no matching model change. Example class: a model column (`orders.archived_at`) missing from the initial migration.
- A migration without a working `downgrade`, or one that hardcodes a constraint or index name that can differ on PostgreSQL (the auto-generated name differs from the SQLite one). Example class: a constraint-name cleanup migration that fails on PostgreSQL.
- A migration that rebuilds a table while SQLite foreign-key enforcement is on.
- A new migration not mirrored in every hand-maintained bootstrap or snapshot schema file (the same columns, CHECK constraints and server defaults), so a fresh database built from the snapshot differs from a migrated one. The bootstrap's revision stamp must replace the old stamp, not insert a second row, and must never be written before the DDL it records. Anchor the finding on the added migration line and cite the snapshot as evidence, so it is not treated as pre-existing.
- A migration that returns early (a missing env var, an empty or absent schema) while the migration tool still stamps the revision, so the revision is recorded as applied without its effect.
- A destructive upgrade or downgrade step (dropping a table, column or data) with no explicit opt-in gate. Also docs that tell operators to downgrade through a table-dropping revision; cite the table-dropping migration file as evidence alongside the docs line.
- PostgreSQL-only semantics hidden by SQLite tests, only when the repo shows SQLite tests and a PostgreSQL target: NULLs are distinct in a UNIQUE constraint, identifiers are truncated at 63 bytes, `integer` (int4) overflows. Others, e.g. `max()` on a uuid column, a `search_path` that misses the schema, offline `--sql` setup. The failure scenario must name the PostgreSQL behaviour.
- A full UNIQUE constraint over columns of rows that can be soft-retired, so a retired row blocks a new one; it needs a partial index (`WHERE retired_at IS NULL`).
- Overwriting or deleting an already-published artifact before its replacement is validated: `write_text` truncating the old file before the new content is known good, delete-before-write, a glob prefix that also matches a sibling's files.

Concurrency (category "concurrency"):
- A SELECT-then-UPDATE job or outbox claim without `FOR UPDATE SKIP LOCKED` or a compare-and-set `UPDATE ... WHERE status = :expected`, so two workers can process the same row. Example class: a non-atomic reply-outbox job claim.
- A lifecycle or status transition (for example under concurrent PATCH requests) that reads the state, checks it, then writes it without a lock or a conditional update. Also a job queued when no dispatcher is running to claim it, so it waits until the next process restart.

Error handling (category "error_handling"):
- A swallowed exception (bare `except`, `except Exception: pass`, or a handler that logs and continues when the caller needs the failure).
- Raw database exception text logged or returned to the client. Example class: an ops endpoint that logs the raw DB error.
- A dropped `await` on a coroutine, or a task created and never awaited or kept.
- An except block that catches a database error and does more database work on the same session without `session.rollback()` first.

Each finding must show the concrete interleaving, input or schema state that breaks. Do not flag theoretical races on data that only one process touches.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote, and optionally finder_confidence. Also return files_read_outside_packet and coverage.

Return {"findings": [], "files_read_outside_packet": [], "coverage": [...]} if you find no candidate, with coverage listing what you checked.
