# Runbooks — Common Issues and Fixes

## GPU Pod Stuck in Pending

**Symptoms:** `kubectl get pod <name> -n gpu-workloads` shows `Pending` with no node assigned.

**Diagnosis:**
```bash
kubectl describe pod <name> -n gpu-workloads | grep -A 20 "Events:"
kubectl describe pod <name> -n gpu-workloads | grep "Insufficient"
```

**Common causes:**
1. No GPU node available → `kubectl get nodes -l nvidia.com/gpu=true`
2. Missing toleration → pod spec lacks `nvidia.com/gpu:NoSchedule` toleration
3. GPU quota exhausted → `kubectl describe resourcequota -n gpu-workloads`
4. GPU Operator not ready → `kubectl get pods -n infra | grep gpu-operator`

**Fix:**
```bash
# Check GPU Operator status
kubectl get pods -n infra -l app=gpu-operator
# If operator pods are crashing, check logs
kubectl logs -n infra -l app=gpu-operator --tail=50
```

---

## ArgoCD Application Not Syncing

**Symptoms:** Application shows `OutOfSync` or `Unknown` in ArgoCD UI.

**Diagnosis:**
```bash
kubectl get application <name> -n argocd -o yaml | grep -A 10 conditions
argocd app get <name>  # if argocd CLI installed
```

**Common causes:**
1. Git repo unreachable → check ArgoCD repository settings
2. Helm chart error → check `argocd app get <name>` for render errors
3. RBAC insufficient → ArgoCD service account lacks permissions
4. Resource already exists with different owner

**Fix:**
```bash
# Force sync
kubectl patch application <name> -n argocd --type merge \
  -p '{"operation": {"initiatedBy": {"username": "admin"}, "sync": {"revision": "HEAD"}}}'

# Hard refresh (re-render Helm)
argocd app get <name> --hard-refresh
```

---

## vLLM OOMKill / GPU Out of Memory

**Symptoms:** Pod restarts with `OOMKilled` or model fails to load.

**Diagnosis:**
```bash
kubectl logs vllm-<model>-0 -n <namespace> --previous
kubectl top pod vllm-<model>-0 -n <namespace>
```

**Fix options:**
1. Reduce `gpuMemoryUtilization` in values (e.g. 0.85 → 0.75)
2. Enable quantization: set `model.quantization: "awq"` in values
3. Reduce `maxModelLen`
4. Use a smaller model

---

## Model Download Stuck in Init Container

**Symptoms:** Pod stuck in `Init:0/1` for more than 20 minutes.

**Diagnosis:**
```bash
kubectl logs vllm-<model>-0 -n <namespace> -c model-downloader
```

**Common causes:**
1. Missing HuggingFace token (gated model) → check `hf-token` secret exists
2. Network policy blocking egress → check NetworkPolicy
3. Slow network / large model

**Fix:**
```bash
# Check secret
kubectl get secret hf-token -n <namespace>
# Re-create if missing (replace YOUR_TOKEN)
kubectl create secret generic hf-token \
  --from-literal=token=YOUR_TOKEN \
  -n <namespace>
```

---

## Rollback: Revert Bad Deployment

```bash
# Find the last good commit
git log --oneline -10

# Revert to specific commit
git revert <bad-commit-sha>
git push

# Or: force ArgoCD to use a specific revision
kubectl patch application vllm-prod -n argocd --type merge \
  -p '{"spec": {"source": {"targetRevision": "<good-commit-sha>"}}}'
```

---

## Node Drain with GPU Workloads

```bash
# Cordon the node first (stops new pods scheduling here)
kubectl cordon <node-name>

# Check PDB will allow drain
kubectl get pdb -A

# Drain (respects PDB)
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data

# Verify pods rescheduled
kubectl get pods -n gpu-workloads -o wide

# Uncordon after maintenance
kubectl uncordon <node-name>
```
