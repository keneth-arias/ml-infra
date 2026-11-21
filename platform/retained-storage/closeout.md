# Verify the retained disks after worker release

Run this read-only check after bm and fw13 have reconciled the last worker group. Supply the digest recorded in the protected operator archive when the final acquisition receipt was issued. Use the same managed cluster, independently pinned CA and operator identity as at acquisition.

```bash
python3 platform/retained-storage/closeout.py \
  --all-receipt "$work/all-bound.json" --all-receipt-sha256 "$archived_acquisition_sha256" \
  --approval "$approval" --baseline "$work/inventory.json" --output "$work/retained-at-release.json" \
  --context "$context" --kubeconfig "$kubeconfig" --endpoint "$endpoint" \
  --ca-sha256 "$ca_sha256" --profile "$profile"
```

The receipt ties every disk ID, PVC and PV identity, size and CSI handle back to the original acquisition. The check requires zero workers, zero attachments and exactly the healthy detached disks in the workload project recorded in the approved receipt. The current layout retains two disks. An archived split-training acquisition retains its three original disks under its original approval. Run it again with a fresh output path if provider detach is still reconciling. A missing or replaced disk requires investigation before closing the worker window.

Preserve the receipt alongside the independent backend-lock, billing and retention records. The retention owner tracks disk charges through the recorded disposal date. This check can run after the acquisition deadline.
