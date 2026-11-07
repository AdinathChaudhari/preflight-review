#!/usr/bin/env bash
set -eu
make dist
git push origin "$TAG" 2>&1 | tee push.log
echo "published $TAG"
