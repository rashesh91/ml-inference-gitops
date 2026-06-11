# Quickstart Guide

## Prerequisites

- `kubectl` configured for your cluster
- `helm` v3.x
- `git`
- Optional: `kubeseal` (for secret management)

## Step 1 — Clone and Configure

```bash
git clone https://github.com/rashesh91/ml-inference-gitops.git
cd ml-inference-gitops

# If forking, replace rashesh91 with your own GitHub username
grep -r "rashesh91" --include="*.yaml" -l | xargs sed -i 's/rashesh91/YOUR_GITHUB_USERNAME/g'
```

## Step 2 — Bootstrap the Cluster (Phase 1)

```bash
# Apply namespaces and quotas first
kubectl apply -f infrastructure/namespaces/namespaces.yaml

# Apply RBAC
kubectl apply -f infrastructure/rbac/

# Install GPU Operator (skip if no GPU, or use fake-gpu-operator for testing)
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm install gpu-operator nvidia/gpu-operator \
  -n infra \
  -f infrastructure/gpu-operator/values.yaml \
  --create-namespace

# Apply PriorityClasses
kubectl apply -f infrastructure/gpu-operator/node-labels.yaml

# Label your GPU nodes
kubectl label nodes <YOUR_GPU_NODE> nvidia.com/gpu=true gpu-type=t4
kubectl taint nodes <YOUR_GPU_NODE> nvidia.com/gpu=true:NoSchedule

# Validate GPU setup
kubectl apply -f infrastructure/gpu-operator/gpu-test-pod.yaml
kubectl wait --for=condition=Ready pod/gpu-test -n gpu-workloads --timeout=120s
kubectl logs gpu-test -n gpu-workloads
kubectl delete pod gpu-test -n gpu-workloads
```

## Step 3 — Deploy vLLM Manually (Phase 2, no ArgoCD yet)

```bash
# Dev (CPU-only, small model)
helm install vllm-dev ./applications/vllm \
  -n dev \
  -f applications/vllm/values.yaml \
  -f applications/vllm/values-dev.yaml

# Wait for pod to be ready (first run downloads model — takes ~10 min)
kubectl rollout status statefulset/vllm-tinyllama -n dev --timeout=600s

# Test the API
kubectl port-forward svc/vllm-tinyllama 8000:8000 -n dev &
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "tinyllama", "prompt": "The sky is", "max_tokens": 30}'
```

## Step 4 — Install ArgoCD (Phase 3)

```bash
# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=argocd-server -n argocd --timeout=300s

# Get admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d

# Port-forward to access UI
kubectl port-forward svc/argocd-server 8080:443 -n argocd
# Open: https://localhost:8080 (admin / <password>)
```

## Step 5 — Bootstrap App of Apps

```bash
# Create AppProjects
kubectl apply -f argocd-config/projects.yaml

# Apply root app — this triggers everything else
kubectl apply -f argocd-config/root-app.yaml -n argocd

# Watch ArgoCD sync in the UI or:
kubectl get applications -n argocd -w
```

## Verification

```bash
# Run full E2E test
./tests/e2e-test.sh dev tinyllama

# Run GPU scheduling tests
./tests/gpu-scheduling-test.sh gpu-workloads
```

## No GPU? Use CPU-Only Mode

The `values-dev.yaml` sets `gpu.count: 0` which passes `--device=cpu` to vLLM. Everything works the same — just much slower inference. You can learn all Kubernetes and ArgoCD concepts without a GPU.
