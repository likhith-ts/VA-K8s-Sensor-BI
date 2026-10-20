#!/usr/bin/env bash
# Remove the stack, including database and stream volumes.
set -euo pipefail
kubectl delete namespace envbi --wait=true
