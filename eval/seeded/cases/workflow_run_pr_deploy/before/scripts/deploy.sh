#!/usr/bin/env bash
# Deploy the checked-out commit to the named environment.
set -euo pipefail
env="${1:?usage: deploy.sh <environment>}"
echo "deploying $(git rev-parse --short HEAD) to $env"
