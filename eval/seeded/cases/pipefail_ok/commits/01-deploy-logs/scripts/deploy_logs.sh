#!/usr/bin/env bash
set -euo pipefail
kubectl rollout status deploy/web 2>&1 | tee rollout.log
echo "rollout done"
