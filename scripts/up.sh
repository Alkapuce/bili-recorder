#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CUDA_PROBE_IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"
COMPOSE_FILES=(-f docker-compose.yml)
GPU_REASON=""

log() {
  printf '%s\n' "$*"
}

ensure_runtime_files() {
  if [ ! -f "$ROOT_DIR/config.ini" ]; then
    log "[deploy] missing config.ini"
    log "[deploy] Copy config.example.ini to config.ini and set RoomId before starting."
    exit 1
  fi

  if ! awk -F= '
    tolower($1) ~ /^[[:space:]]*roomid[[:space:]]*$/ {
      gsub(/[[:space:]]/, "", $2)
      if ($2 ~ /^[1-9][0-9]*$/) found=1
    }
    END { exit(found ? 0 : 1) }
  ' "$ROOT_DIR/config.ini"; then
    log "[deploy] invalid RoomId in config.ini"
    log "[deploy] Set RoomId to a positive integer before starting."
    exit 1
  fi

  mkdir -p "$ROOT_DIR/recordings"
}

docker_gpu_ready() {
  if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    GPU_REASON="docker daemon 未注册 nvidia runtime"
    return 1
  fi

  if ! command -v nvidia-smi >/dev/null 2>&1; then
    GPU_REASON="宿主机缺少 nvidia-smi"
    return 1
  fi

  if ! nvidia-smi -L >/dev/null 2>&1; then
    GPU_REASON="宿主机 GPU 当前不可用"
    return 1
  fi

  if docker image inspect "$CUDA_PROBE_IMAGE" >/dev/null 2>&1; then
    if docker run --rm --pull=never --gpus all --entrypoint /bin/sh "$CUDA_PROBE_IMAGE" -c 'nvidia-smi >/dev/null 2>&1' >/dev/null 2>&1; then
      return 0
    fi
  fi

  if docker run --rm --gpus all --entrypoint /bin/sh "$CUDA_PROBE_IMAGE" -c 'nvidia-smi >/dev/null 2>&1' >/dev/null 2>&1; then
    return 0
  fi

  GPU_REASON="docker --gpus all 探测失败"
  return 1
}

if docker_gpu_ready; then
  COMPOSE_FILES+=(-f docker-compose.gpu.yml)
  log "[deploy] GPU mode enabled via docker-compose.gpu.yml"
else
  log "[deploy] GPU unavailable: ${GPU_REASON}"
  log "[deploy] Falling back to CPU mode"
fi

if [ "$#" -eq 0 ]; then
  set -- up -d --build
fi

if [ "${1:-}" = "up" ] || [ "${1:-}" = "start" ] || [ "${1:-}" = "run" ]; then
  ensure_runtime_files
fi

# Compose's Bake backend requires an explicit grant for the configured host build network.
if [[ " $* " == *" --build "* ]]; then
  docker compose "${COMPOSE_FILES[@]}" build --print |
    docker buildx bake --allow=network.host --load -f -
  ARGS=()
  for arg in "$@"; do
    if [ "$arg" = "--build" ]; then arg="--no-build"; fi
    ARGS+=("$arg")
  done
  set -- "${ARGS[@]}"
fi

exec docker compose "${COMPOSE_FILES[@]}" "$@"
