# tofui-wan-longform-worker

Docker image for the 6 GPU worker pods that run WAN 2.2 Animate motion
transfer for tofui long-form video generation.

Each pod handles one segment (1/6th of the user's reference video). Reads
inputs from R2, renders chunks of 77 frames at 368×640 via ComfyUI, uploads
each chunk back to R2, heartbeats progress to the tofui orchestrator on
Railway, and self-terminates on successful completion. Full pipeline +
design rationale lives in the main tofui repo at
`docs/longform-video-integration-plan.md`.

## Layout

```
Dockerfile                       — image definition (~38 GB final)
worker.py                        — single-segment worker entry point
workflow_template.json           — ComfyUI graph (placeholder until Day 11)
requirements-worker.txt          — boto3 + requests
.github/workflows/build-and-push.yml  — GHA: tag push → GHCR
WORKER_VERSION                   — bumped before each tag (semver)
```

## Release procedure

After v1.0.0 exists in GHCR (initial manual push, see below), every new
release is:

1. Make changes on `master`.
2. Bump `WORKER_VERSION` (semver — patch for bug fix, minor for new
   feature, major for breaking).
3. Commit + push.
4. `git tag v$(cat WORKER_VERSION) && git push --tags`.
5. GHA workflow runs (~15 min subsequent builds thanks to layer cache).
   Watch via `gh run watch` if you want.
6. Once green, update the tofui Railway env var `WAN_WORKER_IMAGE` to
   the new tag. The orchestrator picks up new pods with the new image
   on the next job. **Never set `WAN_WORKER_IMAGE` to `:latest` in
   production** — that tag exists for debugging convenience only.

## Initial build (one-time, manual)

GHA can't run the very first build because the runner only has ~14 GB free
disk and the no-cache initial image is ~38 GB. Run from an engineer's
machine with Docker installed + ~100 GB free disk + a few hours of bandwidth:

```bash
# Authenticate to GHCR. PAT needs `write:packages` scope.
echo "$GHCR_PAT" | docker login ghcr.io -u yascc --password-stdin

# Build for linux/amd64 (RunPod GPUs are amd64) and push.
docker buildx build \
  --platform linux/amd64 \
  -t ghcr.io/yascc/tofui-wan-longform-worker:v1.0.0 \
  -t ghcr.io/yascc/tofui-wan-longform-worker:latest \
  --push .
```

Expect:
- ~3 hours for the model bake step (downloads ~25 GB from HuggingFace).
- ~30-90 minutes for the GHCR push (~38 GB upload).
- After it's done, verify with `docker pull` from a clean machine.

## Runtime env vars (set by tofui orchestrator at pod spawn)

| Var | Purpose |
|---|---|
| `JOB_ID` | UUID of the long-form job this pod is part of |
| `SEGMENT_NUMBER` | 0-5; which segment this pod handles |
| `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` | R2 access |
| `JOB_SEED` | hash(jobId) mod 2^32 — locked across all 6 pods for character consistency |
| `TOFUI_HEARTBEAT_WEBHOOK_URL` | Where to POST heartbeats |
| `TOFUI_HEARTBEAT_SECRET` | Sent as `X-Tofui-Worker-Secret` header |
| `RUNPOD_POD_ID` | Auto-set by RunPod; needed for self-terminate |
| `RUNPOD_API_KEY` | For self-terminate REST call (Layer 2) |
| `GPU_TYPE` | Reported in heartbeats; `RTX 4090` / `RTX 5090` / `L40S` |
| `TEST_MODE` | `1` short-circuits ComfyUI for $0.05 orchestration tests — remove before closed beta |
