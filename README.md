# Interactive KBQA MCP

[![ACL 2024 paper](https://img.shields.io/badge/Paper-ACL%202024-1f6feb.svg)](https://aclanthology.org/2024.acl-long.569/)
[![Original GitHub repository](https://img.shields.io/badge/GitHub-Original%20repo-24292f.svg?logo=github)](https://github.com/JimXiongGM/Interactive-KBQA)

This repository brings together and upgrades the knowledge-base tools introduced in Interactive-KBQA, published at ACL 2024.

- Uses Qwen3-Embedding-4B for semantic retrieval.
- Adds `SearchTypes` for Freebase type discovery.
- Exposes the tools through standard MCP Streamable HTTP.
- Improves concurrent request handling with bounded tool concurrency, HTTP connection reuse, and shared search indexes.

| Knowledge base | Streamable HTTP MCP URL | Tools |
| --- | --- | --- |
| Freebase | `http://127.0.0.1:28777/freebase/mcp` | `SearchNodes`, `SearchTypes`, `SearchGraphPatterns`, `ExecuteSPARQL` |
| Wikidata KQA Pro | `http://127.0.0.1:28777/wikidata-kqapro/mcp` | `SearchNodes`, `SearchGraphPatterns`, `ExecuteSPARQL` |

## Data and Software

Download both Zstandard archives from either provider and extract them from the repository root. Each archive contains its `data/` subdirectory, including `virtuoso.db` and `virtuoso.ini`. [Google Drive](https://drive.google.com/drive/folders/11Oz4i5HsYrBemJ2BEf86o_ukbrp_Mj-l?usp=drive_link) or [Baidu Netdisk](https://pan.baidu.com/s/1VloQsioBCuhVz1-vL6zbXw?pwd=caey)

| Data directory | Archive |
| --- | --- |
| `data/freebase` | `interactive-kbqa-freebase.tar.zst` |
| `data/wikidata_kqapro` | `interactive-kbqa-kqapro.tar.zst` |

```bash
tar --zstd -xf interactive-kbqa-freebase.tar.zst
tar --zstd -xf interactive-kbqa-kqapro.tar.zst
```

Install Virtuoso with one command. The installer verifies the official release checksum and stores the binary in `.cache/`.

```bash
./scripts/virtuoso.sh install
```

Install the Python dependencies. The scripts target Linux x86-64 and require `uv`, `tmux`, `curl`, and `flock`. Local SGLang requires a GPU.

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

For local embedding, install SGLang as well.

```bash
uv pip install --python .venv/bin/python -r requirements.txt 'sglang==0.5.23'
```

Download the embedding model from Hugging Face.

```bash
uvx --from huggingface-hub hf download Qwen/Qwen3-Embedding-4B \
  --local-dir data/models/Qwen3-Embedding-4B
```

Start the full stack. The script runs one Virtuoso instance per knowledge base, one embedding service, and one MCP process.

```bash
./scripts/start.sh
```

Run a SPARQL query against each database. The responses should contain `Queens` and `Walt Disney Studios Motion Pictures`, respectively.

```bash
curl --noproxy 127.0.0.1 --fail-with-body -sS -H 'Accept: application/sparql-results+json' \
  --data-urlencode 'query=PREFIX ns: <http://rdf.freebase.com/ns/>
SELECT ?name WHERE {
  ?person ns:type.object.name "Donald Trump"@en ;
          ns:people.person.place_of_birth ?place .
  ?place ns:type.object.name ?name .
  FILTER(langMatches(lang(?name), "en"))
} LIMIT 1' \
  http://127.0.0.1:29501/sparql

curl --noproxy 127.0.0.1 --fail-with-body -sS -H 'Accept: application/sparql-results+json' \
  --data-urlencode 'query=SELECT ?name WHERE {
  ?entity <pred:name> ?name .
  FILTER(?name = "Walt Disney Studios Motion Pictures")
} LIMIT 1' \
  http://127.0.0.1:29500/sparql
```

## Run

Add either URL from the table to your MCP client. Run `.venv/bin/python scripts/test_mcp.py` to smoke-test both MCP endpoints. The tmux session is `interactive-kbqa`, and logs are in `log/`.

To reuse an existing Qwen3-Embedding-4B service, set `KB_EMBEDDING_BASE_URL` to its OpenAI-compatible `/v1` URL before starting. Set `KB_EMBEDDING_API_KEY` if needed. This mode only requires the dependencies in `requirements.txt`.
