<!-- preflight-review prompt: lens_deploy_ci v1 | adapted from: preflight miss sweep 2026-11-06 (CI and deploy class) | retrieved 2026-11-06 -->
Lens: CI, deploy and runbook safety. This lens runs only on a batch that changes CI workflows, deploy manifests (Kubernetes, Helm, Kustomize), Dockerfiles, shell scripts or deploy runbooks.

Flag a change in those files (or in a runbook that operators follow) that makes a deploy unsafe, insecure or wrong. Category "security" for a privilege or trust problem, "correctness" for a deploy that does the wrong thing or reports success when it failed.

CI workflows:
- A `workflow_run`-triggered deploy job that does not check `github.event.workflow_run.event == 'push'` (and the branch), so a pull request's run, including one from a fork, can trigger a deploy.
- No top-level or job-level `permissions:` block, so the job runs with the default token scope.
- `actions/checkout` of pull-request code (`pull_request_target`, `workflow_run`) without `persist-credentials: false`, leaving a write token in `.git/config` for untrusted code.
- A cloud OIDC or workload-identity-federation trust binding (an attribute condition, a subject claim) not restricted to a ref, branch or environment, so any workflow in the repo can assume the deploy identity.
- A step output, `github.event.*` field or input interpolated with `${{ }}` straight into a `run:` script or into a YAML string, instead of passed through `env:` (or `toJSON`) and quoted.
- A retry loop or wait whose total time exceeds the job's `timeout-minutes`, so the job is killed before the retry budget runs out and reports a timeout instead of the real failure.

Kubernetes and deploy manifests:
- An RBAC Role that grants `create` on Jobs or Pods: that implies reading every Secret the pod can mount. Also namespace-wide `pods/log` or `secrets` access where one resource name would do.
- A Job applied with `kubectl apply` when a Job of the same name may exist: a Job's pod template is immutable, so the apply fails unless the old Job is deleted first (delete, then apply).
- `kubectl apply --dry-run=server` run by an identity whose Role lacks `create` on that resource, so the dry run fails with a permissions error.
- A rollout check (`kubectl rollout status`) that covers one Deployment when the change updates several.
- `kubectl set image` followed by `kubectl set env` (or two separate patches) on the same Deployment: two rollouts, the first with the new image and the old environment.
- A health check or wait that prints a condition's `type` without checking its `status`, so a `False` condition reads as healthy.

Shell scripts and Dockerfiles:
- `curl` or `wget` with no `--max-time`/`--connect-timeout` in a deploy path, or with `|| true` under `set -e`, so a failed download or health check is silently ignored.
- A script that relies on `set -e` inside a pipeline, a subshell or a `&&` chain where errexit does not apply, or misses `set -o pipefail` before a pipeline whose failure matters.
- A secret passed as a build argument (`ARG`/`--build-arg`) or copied into an image layer.

Runbooks (deploy and operations docs): a documented command order or flag that fails or does harm when an operator follows it literally, by the rules above, or a runbook that disagrees with the manifest or workflow it describes. Quote the runbook line and the manifest or workflow line it contradicts.

Each finding must name the event, identity or state that triggers the failure (for example "a pull request from a fork", "a second deploy while the old Job exists"). Do not flag style, pinning a third-party action by tag rather than SHA, or template interpolation of values an attacker cannot control.

For each finding, return the fields defined by the finder schema: file, start_line, end_line, caused_by, lane, category, severity, effort, title, claim, failure_scenario, why_it_matters, suggested_fix, evidence (each with file, line, quote), bug_class_instances, rule_quote, and optionally finder_confidence. Also return files_read_outside_packet and coverage.

Return {"findings": [], "files_read_outside_packet": [], "coverage": [...]} if you find no candidate, with coverage listing what you checked.
