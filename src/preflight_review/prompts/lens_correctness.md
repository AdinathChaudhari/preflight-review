<!-- preflight-review prompt: lens_correctness v3 | adapted from: research/v1/anthropic/02-code-review-plugin-prompt.md; preflight miss sweep 2026-11-06 | retrieved 2026-11-06 -->
Lens: correctness and root cause.

Find inputs or states where the changed code gives a wrong result, raises, corrupts data, or breaks a caller listed in the packet. Then ask of each fix or workaround in the change: does it address the root cause, or only hide the symptom? Report a symptom-only fix when you can show the cause still fires.

Check each of these on changed code. Each item names its bug class: when a class recurs, report one finding and list the other places in bug_class_instances.

- (a) `[untrusted-parse]` Parsing untrusted input: a `json.loads` result used as a dict without an isinstance check; `int()`/`float()` on an empty, None or huge value; NaN or Infinity accepted (Python's `json` needs `parse_constant` to reject them); CRLF line endings or a BOM; non-string entries in a list of strings. Flag this only when the packet shows the value comes from outside the process (a CLI argument, a file, an HTTP body, an env var, subprocess output). Never flag it for values the codebase builds itself.
- (b) `[exit-code]` A CLI, validator or audit script that prints errors but exits 0, so a caller or CI step treats the failure as success.
- (c) `[error-contract]` A function that promises a named error lets KeyError, TypeError, OSError or UnicodeDecodeError escape instead, or a broad except remaps a server fault to a 4xx client error. (Plainly swallowed exceptions belong to the data_concurrency lens; do not report them here.)
- (d) `[shell]` `cmd | tail` (or any pipe) without `set -o pipefail`, so the pipeline's status is the last command's; `x=$(curl ...)` under `set -e`; success decided by elapsed time (`SECONDS`) instead of a flag or exit status. Flag curl without `--max-time`, or retries × timeout, only in CI or job code where a job timeout is visible in the packet.
- (e) `[version-sort]` A lexical sort of versioned names, so `v10` sorts before `v9`.
- (f) `[rerun]` Re-running a repair, migration or seed script on its own output: the second run duplicates, corrupts or fails.
- (g) `[overwrite]` An overwrite path that keeps stale fields from the old record; a soft-delete flag that list or lookup queries do not filter on.
- (h) `[cwd-path]` A path resolved against the current working directory where sibling scripts resolve it from `__file__`, so the script breaks when run from another directory.

These are exceptions to the input-dependence rule only when the failing input is shown to be reachable. For each one, name a concrete failing input in failure_scenario (`null`, `NaN`, a CRLF line, `v10`, a second run) and quote the line that parses, sorts, exits or resolves it.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote, and optionally finder_confidence. Also return files_read_outside_packet and coverage.

Return {"findings": [], "files_read_outside_packet": [], "coverage": [...]} if you find no candidate, with coverage listing what you checked.
