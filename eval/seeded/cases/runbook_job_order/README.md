# runbook_job_order

A runbook-only diff (`deploy/k8s/README.md`). Step 1 applies the `migrate` Job and no step ever deletes it. From the second deploy on, the completed Job from the last release still exists: with the unchanged `:latest` template, `kubectl apply` reports it `unchanged`, step 2 passes at once on the old completed Job, and the new release's migrations never run. Any template change instead fails the apply, because a Job's pod template is immutable.

Before 1.1 the docs-only diff and the prefilter's docs drop lost this class. Now the `deploy_ci` lens is path-gated to `deploy/**`, a docs-only diff that touches its paths is reviewed by it (even under `docs_review: off`), and its findings on files inside its gate are exempt from the docs drop.
