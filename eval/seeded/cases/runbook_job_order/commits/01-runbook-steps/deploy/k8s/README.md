# Deploy

Run the database migration Job, then roll out the API.

1. `kubectl apply -f deploy/k8s/migrate-job.yaml`
2. `kubectl wait --for=condition=complete job/migrate --timeout=300s`
3. `kubectl rollout restart deployment/api`
