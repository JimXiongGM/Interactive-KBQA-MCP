"""Load search indexes and call the local Qwen3-Embedding service."""

import json
import os
from functools import cache
from pathlib import Path
from threading import Lock

import faiss
import httpx2
import numpy as np

DATA = Path(__file__).resolve().parent.parent / "data"
MODEL = "Qwen3-Embedding-4B"
DIMENSION = 2560
# Shared connection pool sized for high-concurrency tool calls (httpx2 defaults to 100 connections).
HTTP = httpx2.Client(
    trust_env=False,
    timeout=httpx2.Timeout(120, connect=5),
    limits=httpx2.Limits(
        max_connections=int(os.environ.get("KBQA_HTTP_MAX_CONNECTIONS") or "512"),
        max_keepalive_connections=int(
            os.environ.get("KBQA_HTTP_MAX_KEEPALIVE") or "256"
        ),
    ),
)
_INDEX_LOCK = Lock()

TYPE_TASK = (
    "Given a natural-language description, retrieve the most relevant Freebase type."
)
GRAPH_TASK = (
    "Given a natural-language relation description, retrieve the most relevant "
    "Freebase predicate or predicate path."
)
NODE_TASK = (
    "Given a natural-language mention or description, retrieve the matching "
    "knowledge graph entity or concept."
)
EDGE_TASK = (
    "Given a natural-language relation or attribute description, retrieve the most "
    "relevant knowledge graph attribute, relation, or qualifier."
)


def embed(task: str, *queries: str) -> np.ndarray:
    """Embed queries with the indexing instructions and normalize vectors for cosine search."""
    base = os.environ.get("KB_EMBEDDING_BASE_URL", "http://127.0.0.1:27100/v1")
    key = os.environ.get("KB_EMBEDDING_API_KEY")
    response = HTTP.post(
        f"{base.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {key}"} if key else None,
        json={
            "model": MODEL,
            "input": [f"Instruct: {task}\nQuery: {query.strip()}" for query in queries],
            "encoding_format": "float",
        },
    )
    response.raise_for_status()
    rows = sorted(response.json()["data"], key=lambda row: row["index"])
    vectors = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
    if vectors.shape != (len(queries), DIMENSION) or not np.isfinite(vectors).all():
        raise ValueError(f"Invalid embedding response shape or values: {vectors.shape}")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms <= 1e-12):
        raise ValueError("Embedding response contains a zero vector")
    return np.ascontiguousarray(vectors / norms)


@cache
def _load_index(kb: str, name: str) -> tuple:
    """Load an HNSW index and its matching records, then validate the model and dimensions."""
    directory = DATA / kb / "vectors" / name
    config = json.loads((directory / "config.json").read_text())
    index = faiss.read_index(str(directory / "index.faiss"))
    with (directory / "records.jsonl").open() as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    if (config["model"], config["dimension"], index.d, index.ntotal) != (
        MODEL,
        DIMENSION,
        DIMENSION,
        len(records),
    ):
        raise ValueError(f"Incompatible vector index: {directory}")
    index.hnsw.efSearch = config["ef_search"]
    return index, records


def nearest(
    kb: str, name: str, vector: np.ndarray, limit: int
) -> list[tuple[dict, float]]:
    """Return records with similarity scores and prevent concurrent first requests from reloading the index."""
    with _INDEX_LOCK:
        index, records = _load_index(kb, name)
    scores, ids = index.search(vector.reshape(1, -1), min(limit, len(records)))
    return [(records[i], float(score)) for i, score in zip(ids[0], scores[0]) if i >= 0]


def truncate(text: str, limit: int | None) -> str:
    """Truncate a candidate using the original tool's format."""
    if limit and 0 < limit < len(text):
        return f"{text[:limit]}... (omitted {len(text) - limit} chars)"
    return text
