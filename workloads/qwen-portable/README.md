# Qwen3.5-0.8B portable forecast exports

This release uses Seed 27, step 15,745, with a 24-hour PM2.5 history and six hourly outputs. Inference applies the frozen scaler and the `first` input view of the two-channel PM2.5/PM10 model. The merged forecast head runs on numeric histories. Upstream tokenizer files accompany each bundle.

The [release manifest](release.json) identifies every file in the six verified exports. The reference master is full precision SafeTensors. SafeTensors, MLX, and OpenVINO each have FP32 and FP16 weight storage versions. The enrolled runners use FP32 computation. The [Linux build receipts](evidence/build/) and [Apple M4 parity receipts](evidence/metal/) are tied to those bytes. The [reference fixture](reference.json) uses nine synthetic input histories.

The release bundles are stored on bm, with both verified MLX bundles also staged on m4. For a transfer, verify the bundle's `SHA256SUMS` against the release manifest. Model weights and private release inputs are distributed through the existing artifact custody path. The repository tracks their identities, build recipes, and verification records.

## Reconstruct the verified release

| Input | Identity |
| --- | --- |
| Checkpoint manifest | `ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf` |
| Base model revision | `2fc06364715b967f1860aea9cf38778875588b17` |
| Base weight manifest | `1d76cc3cfd4c15f9c749372611e5ca6e0b6f910b1fcf0551ce9157e9bfef29c1` |
| Scaler | `c8ebd87bce74ea0157e85093e4c9c81d981af34fb62f1cbc2c12ce653b2c3863` |
| OpenTSLM source | `a4def7e420e2ecc271137bb3077341d39878d6c0` |
| Runtime | PyTorch 2.9.1, Transformers 5.5.4, PEFT 0.18.1 |

1. Verify the local `microk8s` context and usable capacity on bm. The build jobs use the local `airaware-joint` image pinned in the manifests. The master mounts the pre-existing `qwen-08b-release` claim read-only. It expects `weights/`, `SHA256SUMS`, `scaler-15745.json`, and `step-00015745/` at the claim root. Review the jobs with a server dry run before submission.
2. Prepare the scratch claim with [prepare-pvc.yaml](build/prepare-pvc.yaml) and [prepare-job.yaml](build/prepare-job.yaml). The job creates `deps/`, `source/`, `assets/`, and `exports/` and installs the pinned OpenVINO and MLX CPU dependencies. Stage the public assets and exact training source at the claim root as shown below.
3. Run [master-job.yaml](build/master-job.yaml) after the stage is complete. It verifies and restores the frozen checkpoint, validates all LoRA pairs, preserves the custom forecast tensors, merges the FP32 master, and reloads both SafeTensors bundles.
4. After the master completes, run [derivative-jobs.json](build/derivative-jobs.json). The two jobs build the FP32 and FP16 MLX and OpenVINO derivatives and check forecast parity against the master.
5. After all four derivatives complete, apply [verify-network.yaml](build/verify-network.yaml) and run [verify-job.json](build/verify-job.json). Verify the standalone CLI in every bundle, the package inventory, and the byte hashes. Treat a new digest as a distinct build until it is reviewed against the reference. Use a fresh scratch claim and unused job names for a rebuild.

On bm, set `PVC_ROOT` to the verified scratch claim's backing directory and
`OPENTSLM` to the reviewed OpenTSLM Git checkout. From this directory, stage
the content before launching the master.

```sh
set -eu
revision=a4def7e420e2ecc271137bb3077341d39878d6c0
base=https://huggingface.co/Qwen/Qwen3.5-0.8B/resolve/2fc06364715b967f1860aea9cf38778875588b17
manifest="$PWD/build/assets.sha256"
mkdir -p "$PVC_ROOT/source/opentslm"
cp build/*.py build/requirements-torch.txt "$PVC_ROOT/source/"
for module in forecast forecast_checkpoint forecast_inference; do
  git -C "$OPENTSLM" show "$revision:src/opentslm/$module.py" > "$PVC_ROOT/source/opentslm/$module.py"
done
cp "$PVC_ROOT/source/opentslm/forecast.py" "$PVC_ROOT/source/qwen_architecture.py"
for file in LICENSE config.json tokenizer.json tokenizer_config.json vocab.json merges.txt chat_template.jinja; do
  curl -fsSL "$base/$file" -o "$PVC_ROOT/assets/$file"
done
git -C "$OPENTSLM" show "$revision:LICENSES/MIT.txt" > "$PVC_ROOT/assets/LICENSE-OpenTSLM"
(cd "$PVC_ROOT/assets" && sha256sum --strict --check "$manifest")
```

## Run the Metal parity check

Use native arm64 Python on macOS, with `mlx==0.32.4`. Set `BUNDLE` to the
checksum-verified bundle directory and `WEIGHTS_SHA256` to its release
manifest weight hash. Run `python verify_metal.py "$BUNDLE" "$WEIGHTS_SHA256"`.
The script verifies the weight file, selects Metal explicitly, compares
single and batched inference against the frozen reference, and reports
timing and peak memory.

Production serves MLX FP32 through the [OrbStack service runbook](../web-refresh/qwen/README.orbstack.md).

## Qualification still open

- Measure startup latency and memory on additional PyTorch and OpenVINO devices. Compare FP16 forecasts with FP32 on matched archive windows, with separate overall and high-PM2.5 errors per archive.
- Qualify ONNX FP32 on desktop and Arm64 before trying mobile ORT or reduced-precision exports. Core ML, ExecuTorch, LiteRT, TensorRT, browser runtime and GGUF are feasibility tracks.
