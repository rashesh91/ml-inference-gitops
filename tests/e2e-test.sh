#!/usr/bin/env bash
# End-to-end validation script
# Usage: ./e2e-test.sh [namespace] [model-name]
# Example: ./e2e-test.sh gpu-workloads llama2-7b

set -euo pipefail

NAMESPACE=${1:-"dev"}
MODEL=${2:-"tinyllama"}
SERVICE_NAME="vllm-${MODEL}"
TIMEOUT=300

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

info "Running E2E tests for vllm-${MODEL} in namespace ${NAMESPACE}"
echo "================================================"

# Check 1: ArgoCD application health
info "Checking ArgoCD application sync status..."
SYNC_STATUS=$(kubectl get application "vllm-${NAMESPACE}" -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "NOT_FOUND")
HEALTH_STATUS=$(kubectl get application "vllm-${NAMESPACE}" -n argocd -o jsonpath='{.status.health.status}' 2>/dev/null || echo "NOT_FOUND")

if [[ "$SYNC_STATUS" == "Synced" ]]; then
  pass "ArgoCD sync status: Synced"
else
  fail "ArgoCD sync status: ${SYNC_STATUS} (expected: Synced)"
fi

if [[ "$HEALTH_STATUS" == "Healthy" ]]; then
  pass "ArgoCD health status: Healthy"
else
  fail "ArgoCD health status: ${HEALTH_STATUS} (expected: Healthy)"
fi

# Check 2: StatefulSet is ready
info "Checking StatefulSet readiness..."
READY=$(kubectl get statefulset "${SERVICE_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
DESIRED=$(kubectl get statefulset "${SERVICE_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")

if [[ "$READY" == "$DESIRED" && "$DESIRED" != "0" ]]; then
  pass "StatefulSet ready: ${READY}/${DESIRED} replicas"
else
  fail "StatefulSet not ready: ${READY}/${DESIRED} replicas"
fi

# Check 3: GPU allocated (skip in CPU-only mode)
info "Checking GPU node scheduling..."
POD_NODE=$(kubectl get pod "${SERVICE_NAME}-0" -n "${NAMESPACE}" -o jsonpath='{.spec.nodeName}' 2>/dev/null || echo "")
if [[ -n "$POD_NODE" ]]; then
  GPU_LABEL=$(kubectl get node "${POD_NODE}" -o jsonpath='{.metadata.labels.nvidia\.com/gpu}' 2>/dev/null || echo "")
  if [[ "$GPU_LABEL" == "true" ]]; then
    pass "Pod scheduled on GPU node: ${POD_NODE}"
  else
    info "Pod on non-GPU node (CPU-only mode): ${POD_NODE}"
  fi
else
  fail "Could not determine pod node"
fi

# Check 4: Health endpoint
info "Checking vLLM health endpoint..."
kubectl port-forward "svc/${SERVICE_NAME}" 18000:8000 -n "${NAMESPACE}" &
PF_PID=$!
sleep 3

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:18000/health 2>/dev/null || echo "000")
kill $PF_PID 2>/dev/null || true

if [[ "$HTTP_CODE" == "200" ]]; then
  pass "Health endpoint returned 200"
else
  fail "Health endpoint returned ${HTTP_CODE} (expected: 200)"
fi

# Check 5: Model inference
info "Testing inference request..."
kubectl port-forward "svc/${SERVICE_NAME}" 18000:8000 -n "${NAMESPACE}" &
PF_PID=$!
sleep 3

RESPONSE=$(curl -s -X POST http://localhost:18000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"${MODEL}"'",
    "prompt": "Hello, I am",
    "max_tokens": 20,
    "temperature": 0.1
  }' 2>/dev/null || echo "")

kill $PF_PID 2>/dev/null || true

if echo "$RESPONSE" | grep -q '"text"'; then
  pass "Inference request succeeded"
  echo "Response: $(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['text'])" 2>/dev/null || echo "$RESPONSE")"
else
  fail "Inference request failed. Response: ${RESPONSE}"
fi

# Check 6: Prometheus metrics endpoint
info "Checking metrics endpoint..."
kubectl port-forward "svc/${SERVICE_NAME}" 19090:9090 -n "${NAMESPACE}" &
PF_PID=$!
sleep 2

METRICS=$(curl -s http://localhost:19090/metrics 2>/dev/null | head -5 || echo "")
kill $PF_PID 2>/dev/null || true

if [[ -n "$METRICS" ]]; then
  pass "Prometheus metrics endpoint is available"
else
  info "Metrics endpoint not available (may need ServiceMonitor)"
fi

echo "================================================"
pass "All critical E2E checks passed for vllm-${MODEL} in ${NAMESPACE}"
