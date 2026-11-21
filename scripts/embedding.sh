#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

# Use the configured interpreter or the local virtual environment.
python=${KBQA_PYTHON:-.venv/bin/python}

# Load SGLang from the local model files.
model="$PWD/data/models/Qwen3-Embedding-4B"
test -s "$model/model.safetensors.index.json"
args=()
if [[ -n ${KB_EMBEDDING_API_KEY:-} ]]; then
    args+=(--api-key "$KB_EMBEDDING_API_KEY")
fi

# Release local GPU sentry reservations when the sentry is available.
curl --noproxy '*' -fsS --connect-timeout 1 --max-time 3 http://127.0.0.1:8765/release >/dev/null 2>&1 || true
export HF_HUB_OFFLINE=1
exec "$python" -m sglang.launch_server \
    --model-path "$model" --served-model-name Qwen3-Embedding-4B \
    --host 127.0.0.1 --port "${KB_EMBEDDING_PORT:-27100}" \
    --is-embedding --dtype bfloat16 --tp-size "${KB_EMBEDDING_TP_SIZE:-1}" \
    --context-length 512 --max-running-requests 32 \
    --mem-fraction-static "${KB_EMBEDDING_MEM_FRACTION:-0.30}" \
    --chunked-prefill-size -1 --disable-radix-cache --prefill-only-disable-kv-cache \
    "${args[@]}"
