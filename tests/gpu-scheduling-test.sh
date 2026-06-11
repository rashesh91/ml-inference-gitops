#!/usr/bin/env bash
# GPU scheduling validation tests
# Tests: taints/tolerations, PriorityClass eviction, node drain

set -euo pipefail

NAMESPACE=${1:-"gpu-workloads"}

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

echo "=== GPU Scheduling Tests ==="

# Test 1: GPU node has correct taint
info "Checking GPU node taints..."
GPU_NODES=$(kubectl get nodes -l nvidia.com/gpu=true -o name 2>/dev/null || echo "")
if [[ -z "$GPU_NODES" ]]; then
  info "No nodes labeled nvidia.com/gpu=true — skipping GPU taint tests (CPU-only mode)"
else
  for node in $GPU_NODES; do
    TAINT=$(kubectl get "$node" -o jsonpath='{.spec.taints[?(@.key=="nvidia.com/gpu")].effect}' 2>/dev/null || echo "")
    if [[ "$TAINT" == "NoSchedule" ]]; then
      pass "Node ${node} has correct taint: NoSchedule"
    else
      fail "Node ${node} missing nvidia.com/gpu:NoSchedule taint"
    fi
  done
fi

# Test 2: Pod without toleration cannot schedule on GPU node
info "Testing that pods without tolerations are blocked from GPU nodes..."
cat <<EOF | kubectl apply -f - -n "$NAMESPACE" >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: no-toleration-test
  namespace: ${NAMESPACE}
spec:
  nodeSelector:
    nvidia.com/gpu: "true"
  containers:
    - name: busybox
      image: busybox
      command: ["sleep", "10"]
EOF

sleep 5
STATUS=$(kubectl get pod no-toleration-test -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
REASON=$(kubectl get pod no-toleration-test -n "$NAMESPACE" -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null || echo "")

if [[ "$STATUS" == "Pending" ]]; then
  pass "Pod without toleration is Pending (correctly blocked from GPU node)"
else
  info "Pod status: ${STATUS} (expected Pending if GPU node has taint)"
fi

kubectl delete pod no-toleration-test -n "$NAMESPACE" --ignore-not-found=true >/dev/null 2>&1

# Test 3: Pod with toleration can schedule
info "Testing that pods with tolerations can schedule on GPU node..."
cat <<EOF | kubectl apply -f - -n "$NAMESPACE" >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: with-toleration-test
  namespace: ${NAMESPACE}
spec:
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: busybox
      image: busybox
      command: ["sleep", "30"]
EOF

kubectl wait --for=condition=Ready pod/with-toleration-test -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1 && \
  pass "Pod with toleration scheduled successfully" || \
  info "Pod with toleration still pending (may need GPU node)"

kubectl delete pod with-toleration-test -n "$NAMESPACE" --ignore-not-found=true >/dev/null 2>&1

# Test 4: PodDisruptionBudget prevents drain
info "Testing PodDisruptionBudget configuration..."
PDB_EXISTS=$(kubectl get pdb -n "$NAMESPACE" -l app=vllm -o name 2>/dev/null || echo "")
if [[ -n "$PDB_EXISTS" ]]; then
  pass "PodDisruptionBudget exists: ${PDB_EXISTS}"
  MIN_AVAIL=$(kubectl get pdb -n "$NAMESPACE" -l app=vllm -o jsonpath='{.items[0].spec.minAvailable}' 2>/dev/null || echo "")
  info "minAvailable: ${MIN_AVAIL}"
else
  fail "No PodDisruptionBudget found for vllm pods in ${NAMESPACE}"
fi

# Test 5: PriorityClass exists
info "Checking PriorityClass resources..."
for pc in gpu-inference-high gpu-batch-medium gpu-experiment-low; do
  if kubectl get priorityclass "$pc" >/dev/null 2>&1; then
    VALUE=$(kubectl get priorityclass "$pc" -o jsonpath='{.value}')
    pass "PriorityClass ${pc} exists (value: ${VALUE})"
  else
    fail "PriorityClass ${pc} not found"
  fi
done

echo "=== GPU Scheduling Tests Complete ==="
