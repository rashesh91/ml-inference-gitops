#!/usr/bin/env bash
# Rollback test — simulates a bad deployment and verifies ArgoCD recovers
# This test modifies values-dev.yaml with an invalid image tag, then reverts

set -euo pipefail

ENV=${1:-"dev"}
APP_NAME="vllm-${ENV}"
REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || echo ".")

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

info "=== Rollback Test for ${APP_NAME} ==="

# Step 1: Record current healthy state
info "Recording current healthy commit..."
GOOD_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
info "Good commit: ${GOOD_COMMIT}"

# Step 2: Introduce a bad image tag
info "Introducing bad image tag to simulate failed deployment..."
VALUES_FILE="${REPO_ROOT}/applications/vllm/values-${ENV}.yaml"
ORIG_TAG=$(grep "tag:" "${REPO_ROOT}/applications/vllm/Chart.yaml" | awk '{print $2}')

# Temporarily patch values to a bad tag
sed -i "s|tag: .*|tag: \"v0.0.0-bad-tag\"|" "${REPO_ROOT}/applications/vllm/values-${ENV}.yaml" 2>/dev/null || true

git -C "$REPO_ROOT" add applications/vllm/values-${ENV}.yaml
git -C "$REPO_ROOT" commit -m "test: bad deployment (will be reverted)"
BAD_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
info "Bad commit pushed: ${BAD_COMMIT}"

# Step 3: Push and wait for ArgoCD to detect the change
info "Waiting for ArgoCD to sync bad deployment..."
sleep 30

SYNC_STATUS=$(kubectl get application "$APP_NAME" -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "Unknown")
info "ArgoCD sync status after bad commit: ${SYNC_STATUS}"

# Step 4: Revert
info "Reverting bad commit..."
git -C "$REPO_ROOT" revert --no-edit HEAD
git -C "$REPO_ROOT" push

REVERTED_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
info "Reverted to: ${REVERTED_COMMIT}"

# Step 5: Wait for ArgoCD to recover
info "Waiting for ArgoCD to sync the revert (up to 3 minutes)..."
for i in $(seq 1 18); do
  sleep 10
  SYNC=$(kubectl get application "$APP_NAME" -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "")
  HEALTH=$(kubectl get application "$APP_NAME" -n argocd -o jsonpath='{.status.health.status}' 2>/dev/null || echo "")
  info "  Attempt ${i}/18: sync=${SYNC} health=${HEALTH}"
  if [[ "$SYNC" == "Synced" && "$HEALTH" == "Healthy" ]]; then
    break
  fi
done

# Step 6: Verify
if [[ "$SYNC" == "Synced" && "$HEALTH" == "Healthy" ]]; then
  pass "Rollback successful! ArgoCD recovered to healthy state after git revert"
else
  fail "Rollback did not complete in time. sync=${SYNC} health=${HEALTH}"
fi

info "=== Rollback Test Complete ==="
