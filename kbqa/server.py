"""Serve two independent MCP tool sets in a single HTTP process."""

import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import tyro
import uvicorn
from loguru import logger
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import freebase, kqapro

ResultCount = Annotated[int, Field(ge=1, le=50)]
PatternCount = Annotated[int, Field(ge=1, le=200)]
VectorCount = Annotated[int, Field(ge=1, le=5000)]


# Per-tool concurrency limits, matching the production Freebase MCP defaults; override with environment variables.
LIMITS = {
    "SearchNodes": int(os.environ.get("KBQA_SEARCH_NODES_CONCURRENCY") or "64"),
    "SearchTypes": int(os.environ.get("KBQA_SEARCH_TYPES_CONCURRENCY") or "32"),
    "SearchGraphPatterns": int(
        os.environ.get("KBQA_SEARCH_GRAPH_PATTERNS_CONCURRENCY") or "64"
    ),
    "ExecuteSPARQL": int(os.environ.get("KBQA_EXECUTE_SPARQL_CONCURRENCY") or "128"),
}
# Blocking backend calls run in a dedicated pool; asyncio's default pool caps at min(32, CPUs + 4) threads.
EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("KBQA_MCP_MAX_WORKERS") or "256"),
    thread_name_prefix="kbqa-tool",
)


async def run_blocking(function, *args):
    """Run a blocking backend call in the dedicated thread pool."""
    return await asyncio.get_running_loop().run_in_executor(EXECUTOR, function, *args)


def create_mcp(name: str, backend) -> MCPServer:
    """Register shared tools and add type search for Freebase."""
    mcp = MCPServer(name)
    slots = {tool: asyncio.Semaphore(limit) for tool, limit in LIMITS.items()}

    @mcp.tool(
        name="SearchNodes",
        description="Find candidate entities or concepts from an English mention.",
    )
    async def search_nodes(
        query: str, n_results: ResultCount = 10, omit_length: int | None = None
    ) -> str:
        """Run entity search in the thread pool."""
        async with slots["SearchNodes"]:
            return await run_blocking(
                backend.search_nodes, query, n_results, omit_length
            )

    @mcp.tool(
        name="ExecuteSPARQL",
        description="Execute SPARQL against this knowledge base and return readable answers.",
    )
    async def execute_sparql(sparql: str, str_mode: bool = True) -> str:
        """Run answer queries in the thread pool."""
        async with slots["ExecuteSPARQL"]:
            return await run_blocking(backend.execute_sparql, sparql, str_mode)

    @mcp.tool(
        name="SearchGraphPatterns",
        description=(
            "Use SELECT ?e WHERE to bind entities and discover adjacent predicates with example facts. "
            "Set semantic to rank the patterns by an English description. "
            + (
                "Freebase folds CVT paths as p1 -> p2."
                if backend is freebase
                else "An unbound ?e can retrieve predicate suggestions."
            )
        ),
    )
    async def search_graph_patterns(
        sparql: str,
        semantic: str | None = None,
        topN_vec: VectorCount = 400,
        topN_return: PatternCount = 10,
    ) -> str:
        """Expand and rank graph patterns in the thread pool."""
        async with slots["SearchGraphPatterns"]:
            return await run_blocking(
                backend.search_graph_patterns, sparql, semantic, topN_vec, topN_return
            )

    if backend is freebase:

        @mcp.tool(
            name="SearchTypes",
            description="Retrieve Freebase type IDs such as people.person.",
        )
        async def search_types(query: str, n_results: ResultCount = 10) -> str:
            """Search Freebase types in the thread pool."""
            async with slots["SearchTypes"]:
                return await run_blocking(freebase.search_types, query, n_results)

    return mcp


@asynccontextmanager
async def lifespan(_app: Starlette):
    """Start the session managers for both mounted MCP apps."""
    async with AsyncExitStack() as stack:
        for mcp in SERVERS.values():
            await stack.enter_async_context(mcp.session_manager.run())
        yield


async def health(_request) -> JSONResponse:
    """Report MCP process liveness."""
    return JSONResponse({"status": "ok"})


SERVERS = {
    "freebase": create_mcp("Freebase", freebase),
    "wikidata-kqapro": create_mcp("Wikidata KQA Pro", kqapro),
}
app = Starlette(
    routes=[
        Route("/health", health),
        *(
            Mount(
                f"/{name}",
                app=mcp.streamable_http_app(stateless_http=True, json_response=True),
            )
            for name, mcp in SERVERS.items()
        ),
    ],
    lifespan=lifespan,
)


class LogHandler(logging.Handler):
    """Forward standard-library and Uvicorn logs to Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Preserve the log level and exception details."""
        # Keep the original filename and line number from the log record.
        logger.bind(source=f"{record.filename}:{record.lineno}").opt(
            exception=record.exc_info,
        ).log(record.levelname, record.getMessage())


@dataclass
class Args:
    """MCP HTTP listener settings."""

    host: str = "127.0.0.1"
    port: int = 28777


def main(args: Args) -> None:
    """Start one HTTP worker and log timestamps and source locations."""
    logger.remove()
    logger.configure(
        patcher=lambda record: record["extra"].setdefault(
            "source", f"{record['file'].name}:{record['line']}"
        )
    )
    logger.add(
        sys.stderr,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {extra[source]} | {message}",
    )
    logging.basicConfig(handlers=[LogHandler()], level=logging.INFO, force=True)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_config=None)


if __name__ == "__main__":
    main(tyro.cli(Args, description="Serve Freebase and Wikidata KQA Pro MCP tools."))
