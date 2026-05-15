# tofui-wan-longform-worker — Docker image for the 6 GPU worker pods that
# run WAN 2.2 Animate motion transfer for long-form video generation.
#
# Image is ~38 GB (CUDA base ~6 GB + Comfy ~1 GB + models ~25 GB + Python
# deps ~3 GB + buffer). First build downloads models from HuggingFace — runs
# ~3 hours wall-time. Subsequent rebuilds layer-cache the model bake step.
#
# Tagged ghcr.io/yascc/tofui-wan-longform-worker:vX.Y.Z. Never use :latest in
# production — orchestrator pins a specific version via WAN_WORKER_IMAGE.

FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

# System deps. ffmpeg is used by worker.py for ffprobe (segment duration);
# git is needed for the shallow clones below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg wget curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# ComfyUI + WanVideoWrapper (Kijai's WAN nodes). --depth 1 keeps git history
# out of the image. Pin to specific commits later once Day 11 confirms which
# revisions work; using HEAD for now to make the first build attempt simple.
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /workspace/ComfyUI && \
    git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git \
        /workspace/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper && \
    pip install --no-cache-dir -r /workspace/ComfyUI/requirements.txt && \
    pip install --no-cache-dir -r /workspace/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt

# Model bake — downloads ~25 GB from Kijai's HuggingFace repos. Cached by
# Docker layer caching: subsequent rebuilds skip this step entirely unless
# this RUN command itself changes (e.g., a new model URL).
#
# URLs corrected during Day 2 first-build debugging (plan §7 had stale paths
# from before Kijai split fp8-quantized models into a separate repo). Current
# paths verified via HF API + HEAD checks 2026-05-15:
#   - Main diffusion: Kijai/WanVideo_comfy_fp8_scaled/Wan22Animate/...
#     Picked the "_v2" variant (16.5 GB) over v1 (17.5 GB) — newer, smaller.
#   - Relight LoRA: Kijai/WanVideo_comfy/LoRAs/Wan22_relight/
#     Actual filename is WanAnimate_relight_lora_fp16.safetensors (1.37 GB),
#     not the Wan22_Animate_relight_lora.safetensors the plan referenced.
# Other 3 URLs (VAE, LightX2V LoRA, umt5 text encoder) verified unchanged.
# Local output filenames kept stable so worker.py + workflow JSON don't
# need to know which mirror we pulled from.
RUN mkdir -p /workspace/ComfyUI/models/diffusion_models \
             /workspace/ComfyUI/models/vae \
             /workspace/ComfyUI/models/loras \
             /workspace/ComfyUI/models/text_encoders && \
    wget -q -O /workspace/ComfyUI/models/diffusion_models/Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors \
        "https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/Wan22Animate/Wan2_2-Animate-14B_fp8_scaled_e4m3fn_KJ_v2.safetensors" && \
    wget -q -O /workspace/ComfyUI/models/vae/Wan2_1_VAE_bf16.safetensors \
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors" && \
    wget -q -O /workspace/ComfyUI/models/loras/Wan21_T2V_14B_lightx2v_cfg_step_distill_lora_rank32.safetensors \
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan21_T2V_14B_lightx2v_cfg_step_distill_lora_rank32.safetensors" && \
    wget -q -O /workspace/ComfyUI/models/loras/Wan22_Animate_relight_lora.safetensors \
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_relight/WanAnimate_relight_lora_fp16.safetensors" && \
    wget -q -O /workspace/ComfyUI/models/text_encoders/umt5-xxl-enc-bf16.safetensors \
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/umt5-xxl-enc-bf16.safetensors"

# Worker code + workflow template + Python deps for the worker itself
# (boto3 for R2, requests for heartbeats, runpod for self-terminate fallback).
# These are the layers that change every dev iteration — keep them last so
# the slow model-bake layer above stays cached.
COPY requirements-worker.txt /workspace/requirements-worker.txt
RUN pip install --no-cache-dir -r /workspace/requirements-worker.txt

COPY workflow_template.json /workspace/workflow_template.json
COPY worker.py /workspace/worker.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/workspace/worker.py"]
