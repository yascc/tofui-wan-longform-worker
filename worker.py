"""worker.py — WAN 2.2 Animate motion-transfer worker for tofui long-form.

Runs inside a RunPod pod spawned by tofui's services/runpodOrchestrator.js.
One worker handles ONE segment (1/6th of the reference video). Reads inputs
from R2, renders chunks of 77 frames each via ComfyUI, uploads each chunk
back to R2, and heartbeats progress to the tofui orchestrator.

Critical structure decisions (do not refactor without re-reading the plan):

  - self_terminate() is called ONLY on the success branch after a successful,
    *acknowledged* completion heartbeat. NOT in a finally block. On any error
    path the worker exits without self-terminating, leaving the orchestrator
    (Layer 1) to decide whether to replace this pod. This avoids self-
    immolation on transient hiccups + double-spawn billing overlaps.
    See plan §8 + the Day-11 bug-1 fix.

  - find_resume_point() paginates pg_tables… wait, R2's list_objects_v2.
    Paginates because list_objects_v2 caps at 1000 keys/page; a future
    longer segment could silently truncate and we'd render duplicate
    chunks. Also skips any object under 1 KB — defensive vs 0-byte ghosts.

  - In-loop heartbeats are fire-and-forget (one drop is fine, next 60s
    tick replaces it). The FINAL completion heartbeat uses
    heartbeat_with_ack() with retry — must be acknowledged before we
    self-terminate. If ack fails, exit non-zero without self-terminate;
    orchestrator's reconcile loop will detect all-chunks-present in R2
    and finalize the segment itself.

  - Day 2 scope: this file is the SKELETON. patch_for_chunk() and
    run_comfy_workflow() are stubbed — Day 11 fills them in with real
    ComfyUI graph patching after we have ComfyUI running locally to
    inspect node IDs.
"""

import os
import sys
import time
import json
import subprocess
import traceback
from pathlib import Path

import boto3
import requests


# ─── 1. Read env ──────────────────────────────────────────────────────────────

def _require_env(name):
    v = os.environ.get(name)
    if not v:
        print(f"[worker] FATAL: env var {name} is required", flush=True)
        sys.exit(2)
    return v


JOB_ID         = _require_env('JOB_ID')
SEGMENT_NUMBER = int(_require_env('SEGMENT_NUMBER'))
R2_BUCKET      = _require_env('R2_BUCKET')
R2_ACCESS_KEY  = _require_env('R2_ACCESS_KEY_ID')
R2_SECRET      = _require_env('R2_SECRET_ACCESS_KEY')
R2_ENDPOINT    = _require_env('R2_ENDPOINT')                 # https://<acct>.r2.cloudflarestorage.com
JOB_SEED       = int(_require_env('JOB_SEED'))
HEARTBEAT_URL  = _require_env('TOFUI_HEARTBEAT_WEBHOOK_URL')
WORKER_SECRET  = _require_env('TOFUI_HEARTBEAT_SECRET')
RUNPOD_POD_ID  = _require_env('RUNPOD_POD_ID')
RUNPOD_API_KEY = _require_env('RUNPOD_API_KEY')

# TEST_MODE: when set, the worker short-circuits the actual ComfyUI render
# and uploads 1 KB placeholder chunks. Used during Phase 0 Day 3 + 13 to
# validate orchestration without burning $40/job on real renders. Remove
# the consumer (and this comment) before approving for closed beta.
TEST_MODE = os.environ.get('TEST_MODE') == '1'

GPU_TYPE = os.environ.get('GPU_TYPE', 'unknown')

CHUNK_PREFIX  = f"longform/{JOB_ID}/p{SEGMENT_NUMBER}/"
SEGMENT_KEY   = f"longform/{JOB_ID}/segments/segment_{SEGMENT_NUMBER}.mp4"
CHARACTER_KEY = f"longform/{JOB_ID}/character.jpg"


# ─── 2. R2 client ─────────────────────────────────────────────────────────────

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET,
    region_name='auto',
)


# ─── 3. Resume check ──────────────────────────────────────────────────────────
# Two defensive measures vs the naive list_objects_v2 approach:
#   1. Paginate — list_objects_v2 returns ≤1000 keys per page. A future
#      longer segment could silently truncate; we'd then resume from a
#      too-low number, render duplicate chunks, and overwrite the missing
#      higher chunks. Paginator iterates all pages.
#   2. Size check — skip any chunk under 1 KB. S3 should give atomic
#      semantics (object either exists complete or not at all), but a
#      defensive check is cheap and catches future weirdness. Logs a
#      warning so we can investigate if it ever fires.

def find_resume_point():
    paginator = s3.get_paginator('list_objects_v2')
    chunks = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=CHUNK_PREFIX):
        for o in page.get('Contents', []):
            if 'chunk_' not in o['Key']:
                continue
            if o['Size'] < 1024:
                print(f"[resume] skipping suspect chunk {o['Key']} (size {o['Size']})", flush=True)
                continue
            try:
                chunks.append(int(o['Key'].rsplit('chunk_', 1)[1].split('.', 1)[0]))
            except (ValueError, IndexError):
                print(f"[resume] unparseable chunk key {o['Key']}", flush=True)
    return max(chunks) + 1 if chunks else 0


# ─── 4. Heartbeats ────────────────────────────────────────────────────────────

def _hb_body(chunks_completed, chunks_total, status, error=None):
    body = {
        'jobId':           JOB_ID,
        'podId':           RUNPOD_POD_ID,
        'segmentNumber':   SEGMENT_NUMBER,
        'chunksCompleted': chunks_completed,
        'chunksTotal':     chunks_total,
        'status':          status,
        'gpuType':         GPU_TYPE,
    }
    if error is not None:
        body['errorMessage'] = error
    return body


def heartbeat(chunks_completed, chunks_total, status='running', error=None):
    """Fire-and-forget. Used for in-loop progress heartbeats only — losing one
    is fine because the next 60s tick will replace it. Do NOT use for the
    final completion heartbeat (which must be acked)."""
    try:
        requests.post(
            HEARTBEAT_URL,
            json=_hb_body(chunks_completed, chunks_total, status, error),
            headers={'X-Tofui-Worker-Secret': WORKER_SECRET},
            timeout=10,
        )
    except Exception as e:
        print(f"[heartbeat] failed: {e}", flush=True)        # non-fatal


def heartbeat_with_ack(chunks_completed, chunks_total, status, max_retries=5):
    """Used ONLY for the final completion heartbeat. Returns True iff the
    server responded with 200 + {'ok': True} within max_retries. Exponential
    backoff: 1, 2, 4, 8, 16 s (~31 s total).

    If this returns False the worker exits WITHOUT self-terminating — the
    orchestrator's reconcile loop (Layer 1) will detect all-chunks-present
    in R2 and finalize the segment itself.
    """
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                HEARTBEAT_URL,
                json=_hb_body(chunks_completed, chunks_total, status),
                headers={'X-Tofui-Worker-Secret': WORKER_SECRET},
                timeout=10,
            )
            if resp.status_code == 200:
                try:
                    if resp.json().get('ok') is True:
                        return True
                except ValueError:
                    pass
            print(f"[heartbeat-ack] attempt {attempt + 1}: status={resp.status_code}", flush=True)
        except Exception as e:
            print(f"[heartbeat-ack] attempt {attempt + 1}: {e}", flush=True)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    return False


# ─── 5. Self-terminate ────────────────────────────────────────────────────────

def self_terminate():
    """Layer 2 shutdown. Called ONLY after a successful, acknowledged
    completion heartbeat — never on the error path. If this fails, Layer 1
    (orchestrator-driven pods.terminate from tofui) catches it."""
    try:
        requests.delete(
            f'https://rest.runpod.io/v1/pods/{RUNPOD_POD_ID}',
            headers={'Authorization': f'Bearer {RUNPOD_API_KEY}'},
            timeout=10,
        )
        print("[self-terminate] sent DELETE to RunPod", flush=True)
    except Exception as e:
        print(f"[self-terminate] failed (orchestrator Layer 1 will catch): {e}", flush=True)


# ─── 6. ComfyUI integration ───────────────────────────────────────────────────
# DAY 11 FILL-IN. These two functions are stubs — Day 2 only needs the file
# to import cleanly + the Dockerfile COPY to succeed. Day 11 implements them
# against the real ComfyUI graph after inspecting node IDs locally.

def patch_for_chunk(workflow, chunk_i, seed):
    """Patch the workflow JSON for a specific chunk offset.

    Day 11 TODO: locate the nodes for
      - input video path (chunk-i-th window of /tmp/inputs/segment.mp4)
      - character image (/tmp/inputs/character.jpg)
      - output dir (/tmp/outputs/chunk_NNNN.mp4)
      - sampler seed (locked to JOB_SEED across all chunks of all segments —
        same character look)
      - width=368, height=640, frames=77 (already in template, just verify)
    and return a new dict.
    """
    raise NotImplementedError("patch_for_chunk is a Day 11 deliverable")


def run_comfy_workflow(workflow):
    """POST the patched workflow to ComfyUI's /prompt endpoint, poll until
    the chunk is written to /tmp/outputs/, return the path.

    Day 11 TODO: implement the /prompt → /history poll loop with sane
    timeouts. ComfyUI's queue is single-tenant within a pod so we don't
    need to worry about prompt_id collisions.
    """
    raise NotImplementedError("run_comfy_workflow is a Day 11 deliverable")


def render_chunk_test_mode(chunk_i):
    """TEST_MODE shortcut: skip ComfyUI entirely, write a 1 KB placeholder.
    Used Phase 0 Day 3 / Day 13 to validate orchestration on $0.05 test
    runs instead of $40 full renders. Remove the consumer before closed beta."""
    Path('/tmp/outputs').mkdir(parents=True, exist_ok=True)
    out_path = f'/tmp/outputs/chunk_{chunk_i:04d}.mp4'
    with open(out_path, 'wb') as f:
        f.write(b'TEST_MODE placeholder chunk ' + str(chunk_i).encode() + b'\n' + b'\x00' * 1100)
    time.sleep(0.5)                     # pretend to do some work
    return out_path


# ─── 7. Main ──────────────────────────────────────────────────────────────────

def main():
    print(f"[worker] Starting — job={JOB_ID} segment={SEGMENT_NUMBER} gpu={GPU_TYPE} test_mode={TEST_MODE}", flush=True)

    # Resume check: find where to pick up if a previous pod for this segment
    # uploaded some chunks before being killed.
    start_chunk = find_resume_point()
    print(f"[worker] Resuming from chunk {start_chunk}", flush=True)

    # Download inputs to local disk (faster than streaming on every chunk).
    Path('/tmp/inputs').mkdir(parents=True, exist_ok=True)
    s3.download_file(R2_BUCKET, SEGMENT_KEY,   '/tmp/inputs/segment.mp4')
    s3.download_file(R2_BUCKET, CHARACTER_KEY, '/tmp/inputs/character.jpg')

    # Compute chunks_total from segment duration. 77 frames @ 16 fps = 4.8125 s/chunk.
    duration = float(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', '/tmp/inputs/segment.mp4',
    ]).strip())
    chunks_total = int((duration * 16 + 76) // 77)

    # Load workflow template — Day 11 implements the actual patching.
    with open('/workspace/workflow_template.json') as f:
        workflow = json.load(f)

    # ComfyUI boot: in production we exec the server as a subprocess. Day 11
    # adds the boot + /system_stats wait. Day 2 leaves this as a comment so
    # the skeleton runs without ComfyUI present (TEST_MODE renders fine).
    # comfy = subprocess.Popen([
    #     'python', '/workspace/ComfyUI/main.py', '--listen', '127.0.0.1', '--port', '8188',
    # ], stdout=sys.stdout, stderr=sys.stderr)

    # Loop chunks. self_terminate() is intentionally on the SUCCESS branch
    # only, NOT in a finally — see file docstring.
    last_heartbeat_at = 0
    try:
        for chunk_i in range(start_chunk, chunks_total):
            if TEST_MODE:
                output_path = render_chunk_test_mode(chunk_i)
            else:
                workflow_patched = patch_for_chunk(workflow, chunk_i, JOB_SEED)
                output_path = run_comfy_workflow(workflow_patched)

            chunk_key = f"{CHUNK_PREFIX}chunk_{chunk_i:04d}.mp4"
            s3.upload_file(output_path, R2_BUCKET, chunk_key)
            os.unlink(output_path)

            # Heartbeat every ≥60 s, or immediately on first/last chunk.
            now = time.time()
            if now - last_heartbeat_at >= 60 or chunk_i in (start_chunk, chunks_total - 1):
                heartbeat(chunk_i + 1, chunks_total, 'running')
                last_heartbeat_at = now

        # Final completion heartbeat — MUST be acked before self-terminate.
        print("[worker] All chunks uploaded; awaiting completion ack", flush=True)
        if not heartbeat_with_ack(chunks_total, chunks_total, 'completed'):
            print("[worker] Completion not acked; exiting WITHOUT self-terminate", flush=True)
            sys.exit(1)

        # Ack received — safe to self-terminate (Layer 2).
        print("[worker] Completion acknowledged; self-terminating", flush=True)
        self_terminate()
        sys.exit(0)

    except Exception as e:
        err = ''.join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
        # Best-effort error heartbeat — no retry; we want orchestrator to
        # decide quickly whether to replace this pod.
        try:
            heartbeat(start_chunk, chunks_total, 'error', error=err)
        except Exception:
            pass
        # Do NOT self-terminate on error. The orchestrator evaluates whether
        # the error is replaceable (most are) or fatal (max replacements
        # exceeded → fail job).
        print(f"[worker] Error path; exiting WITHOUT self-terminate. tail={err[-200:]}", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
