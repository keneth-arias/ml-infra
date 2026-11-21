# Nebius managed training cluster

This root manages a worker-free MK8s foundation and optionally a private, fixed-size CPU or spot GPU group. Use a versioned, locked S3 backend per cluster. The CPU pilot uses one `cpu-d3 / 4vcpu-16gb` worker with a 128 GiB boot SSD. The fit uses one CPU support worker and one `gpu-rtx6000-a / 1gpu-24vcpu-218gb` worker with a $1.50/hour maximum spot bid. The cumulative experiment limits are $250, $100 in non-GPU costs and 100 allocated GPU-hours.

A new managed cluster starts with a private API. For public access, set the approved operator egress IPv4 /32 in private tfvars. Existing-cluster mode reads the supplied cluster and capped spot policy from their IDs. The CPU pilot and paired GPU experiment use the owned foundation. Review the state lineage and existing groups before changing any deployed configuration. Previously provisioned pools must be reconciled before adopting timed allocation windows.

## Credentials and plan

On this server, load the verified non-secret IDs with `. ~/.config/ml-infra/nebius.env`. The existing CLI profile is `ml-prod`. Run from this directory. Set `NEBIUS_SA_ID` and `NEBIUS_AUTHORIZED_KEY_ID` to the IDs supplied by IAM. The command wrapper points the provider at the protected private key in `~/.config/ml-infra/nebius-private.pem` and maps the workspace's owner-only S3 credential file into the subprocess environment. Override the paths with `NEBIUS_PRIVATE_KEY_FILE` or `ML_INFRA_ENV_FILE`. Keep these files on the runner.

1. Copy `terraform.tfvars.example` to `terraform.tfvars` and enter the project, subnet and Kubernetes version. Keep the foundation's `node_pools` empty. Confirm available GPU quota and price. Copy `backend.tfbackend.example` to `prod.tfbackend` and enter the private, versioned state bucket and regional endpoint.
2. Configure a named CLI profile using the same registered key. The final command below queries the cluster list.

   ```bash
   ./with-credentials sh -ec 'nebius profile create ml-prod --endpoint api.nebius.cloud --skip-auth --service-account-id "${NEBIUS_SA_ID:?}" --public-key-id "${NEBIUS_AUTHORIZED_KEY_ID:?}" --private-key-file-path "$NEBIUS_PRIVATE_KEY_FILE" --parent-id "${NEBIUS_PROJECT_ID:?}"'
   nebius --profile ml-prod mk8s cluster list
   ```

   Set `NEBIUS_PROJECT_ID` to the same project as `terraform.tfvars` before creating the profile.

3. Initialize the backend, check the configuration and produce a plan for review.

   ```bash
   umask 077
   ./with-credentials terraform init -reconfigure -backend-config=prod.tfbackend
   terraform validate
   ./with-credentials terraform plan -out=mk8s.tfplan
   terraform show mk8s.tfplan
   ```

4. For an approved worker-free foundation run `./with-credentials ./apply-foundation --apply mk8s.tfplan`. Use the [guarded worker commands](management/README.md) for every subsequent allocation. Both cleanup hosts need live project and state access before worker provisioning. For the private API, from a runner with subnet reachability, retrieve credentials into a dedicated kubeconfig before bootstrapping Flux.

   ```bash
   cluster_id=$(./with-credentials terraform output -raw cluster_id)
   nebius --profile ml-prod mk8s cluster get-credentials --id "$cluster_id" --internal --context-name prod-nebius --kubeconfig "$HOME/.kube/nebius-prod"
   kubectl --kubeconfig "$HOME/.kube/nebius-prod" get nodes
   ```

For offline verification, use `terraform init -backend=false`, `terraform fmt -check` and `terraform validate`. The resource layout follows the [Nebius training example](https://github.com/nebius/nebius-solution-library/tree/194faa5249939d7345f6db7f5bf60ccd2f3a6b2c/k8s-training).
