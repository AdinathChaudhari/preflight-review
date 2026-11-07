# workflow_run_pr_deploy

Seed for the `deploy_ci` lens. Commit 1 adds a `workflow_run` deploy that fires after every successful `ci` run. `ci` also runs on `pull_request`, and the deploy checks out `workflow_run.head_sha`, so a pull request's code is deployed with the production identity. The guard needs `github.event.workflow_run.event == 'push'` (and the branch).

Second golden: the workflow has no `concurrency` group, so two ci runs finishing close together start two production deploys and the older commit can land last. The `before/` tree carries `scripts/deploy.sh` and a `Makefile` so the commands the workflows run exist; a missing script is not a finding.

The tree stores `.github/` as `dot_github/` (the loader writes `dot_` as `.`), so this repository's own CI never sees these workflows. The `deploy_ci` lens runs because the diff touches `.github/workflows/**`; a Python-only diff would make no `deploy_ci` call.
