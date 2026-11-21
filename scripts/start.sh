#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

session=interactive-kbqa
if tmux has-session -t "=$session" 2>/dev/null; then
    echo "tmux session already exists: $session"
    exit 0
fi

# Pass the caller's settings to the new tmux session.
export FREEBASE_PORT=${FREEBASE_PORT:-29501} KQAPRO_PORT=${KQAPRO_PORT:-29500}
export KB_EMBEDDING_PORT=${KB_EMBEDDING_PORT:-27100} KBQA_MCP_PORT=${KBQA_MCP_PORT:-28777}
# FREEBASE_SPARQL_ENDPOINT may list existing endpoints (space- or comma-separated); then no local Freebase Virtuoso is started.
local_freebase=0
if [[ -z ${FREEBASE_SPARQL_ENDPOINT:-} ]]; then
    local_freebase=1
    export FREEBASE_SPARQL_ENDPOINT="http://127.0.0.1:$FREEBASE_PORT/sparql"
fi
freebase_probe=${FREEBASE_SPARQL_ENDPOINT%%[ ,]*}
export WIKIDATA_KQAPRO_SPARQL_ENDPOINT="http://127.0.0.1:$KQAPRO_PORT/sparql"
local_embedding=0
if [[ -z ${KB_EMBEDDING_BASE_URL:-} ]]; then
    local_embedding=1
    export KB_EMBEDDING_BASE_URL="http://127.0.0.1:$KB_EMBEDDING_PORT/v1"
fi
export KB_EMBEDDING_BASE_URL="${KB_EMBEDDING_BASE_URL%/}"

# Use the configured interpreter or the local virtual environment.
python=${KBQA_PYTHON:-.venv/bin/python}
"$python" -c 'import kbqa.server'
if (( local_embedding )); then
    "$python" -c 'import importlib.util; assert importlib.util.find_spec("sglang"), "SGLang is required"'
fi
ports=("$KQAPRO_PORT" "${KQAPRO_SQL_PORT:-39500}" "$KBQA_MCP_PORT")
if (( local_freebase )); then
    ports+=("$FREEBASE_PORT" "${FREEBASE_SQL_PORT:-39501}")
fi
if (( local_embedding )); then
    ports+=("$KB_EMBEDDING_PORT")
fi
# Check port availability before starting the services.
"$python" - "${ports[@]}" <<'PYPORT'
import socket
import sys
from contextlib import ExitStack

with ExitStack() as stack:
    for port in sys.argv[1:]:
        listener = stack.enter_context(socket.socket())
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", int(port)))
            listener.listen()
        except OSError as error:
            raise SystemExit(f"Port {port} is unavailable: {error}") from None
PYPORT
databases=(wikidata_kqapro)
(( local_freebase )) && databases+=(freebase)
for database in "${databases[@]}"; do
    test -s "data/$database/virtuoso/virtuoso.db"
done
scripts/virtuoso.sh install
mkdir -p log

# Start each component in a separate window and retain its logs under log/.
tmux_env=()
for name in KBQA_PYTHON KBQA_MCP_PORT \
    FREEBASE_PORT KQAPRO_PORT FREEBASE_SQL_PORT KQAPRO_SQL_PORT \
    FREEBASE_SPARQL_ENDPOINT WIKIDATA_KQAPRO_SPARQL_ENDPOINT \
    KB_EMBEDDING_BASE_URL KB_EMBEDDING_API_KEY KB_EMBEDDING_PORT \
    KB_EMBEDDING_TP_SIZE KB_EMBEDDING_MEM_FRACTION KBQA_MCP_MAX_WORKERS \
    KBQA_SEARCH_NODES_CONCURRENCY KBQA_SEARCH_TYPES_CONCURRENCY \
    KBQA_SEARCH_GRAPH_PATTERNS_CONCURRENCY KBQA_EXECUTE_SPARQL_CONCURRENCY; do
    tmux_env+=(-e "$name=${!name-}")
done
if [[ -v CUDA_VISIBLE_DEVICES ]]; then
    tmux_env+=(-e "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES")
fi
printf -v mcp_command '%q ' "$python" -u -m kbqa.server --port "$KBQA_MCP_PORT"

tmux new-session -d -s "$session" -n wikidata-kqapro -c "$PWD" "${tmux_env[@]}" \
    'exec scripts/virtuoso.sh wikidata_kqapro >>log/wikidata-kqapro.log 2>&1'
if (( local_freebase )); then
    tmux new-window -t "$session:" -n freebase -c "$PWD" \
        'exec scripts/virtuoso.sh freebase >>log/freebase.log 2>&1'
fi
if (( local_embedding )); then
    tmux new-window -t "$session:" -n embedding -c "$PWD" \
        'exec scripts/embedding.sh >>log/embedding.log 2>&1'
fi
tmux new-window -t "$session:" -n mcp -c "$PWD" "exec $mcp_command >>log/mcp.log 2>&1"

# Wait for all backends to respond before reporting readiness.
auth=()
if [[ -n ${KB_EMBEDDING_API_KEY:-} ]]; then
    auth=(-H "Authorization: Bearer $KB_EMBEDDING_API_KEY")
fi
for ((attempt=0; attempt<180; attempt++)); do
    if curl --noproxy '*' -fsS --max-time 2 "${auth[@]}" "$KB_EMBEDDING_BASE_URL/models" >/dev/null 2>&1 \
        && curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$KBQA_MCP_PORT/health" >/dev/null 2>&1 \
        && curl --noproxy '*' -fsS --max-time 2 --data-urlencode 'query=ASK WHERE { ?s ?p ?o }' "$freebase_probe" >/dev/null 2>&1 \
        && curl --noproxy '*' -fsS --max-time 2 --data-urlencode 'query=ASK WHERE { ?s ?p ?o }' "$WIKIDATA_KQAPRO_SPARQL_ENDPOINT" >/dev/null 2>&1; then
        printf 'MCP ready at http://127.0.0.1:%s/{freebase,wikidata-kqapro}/mcp\n' "$KBQA_MCP_PORT"
        exit 0
    fi
    windows=$(tmux list-windows -t "=$session" -F '#{window_name}' 2>/dev/null || true)
    required=(wikidata-kqapro mcp)
    (( local_freebase )) && required+=(freebase)
    for name in "${required[@]}"; do
        if ! grep -Fxq "$name" <<<"$windows"; then
            echo "Startup failed for $name. See log/$name.log." >&2
            exit 1
        fi
    done
    if (( local_embedding )) && ! grep -Fxq embedding <<<"$windows"; then
        echo 'Startup failed for embedding. See log/embedding.log.' >&2
        exit 1
    fi
    sleep 2
done
echo 'Services did not become ready. See log/.' >&2
exit 1
