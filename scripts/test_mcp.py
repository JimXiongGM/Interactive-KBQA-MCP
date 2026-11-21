"""Test both knowledge bases and their core tools against live MCP endpoints."""

import asyncio
import os
from dataclasses import dataclass

import httpx2
import tyro
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@dataclass
class Args:
    """End-to-end test settings."""

    base_url: str = "http://127.0.0.1:" + os.environ.get("KBQA_MCP_PORT", "28777")


CASES = {
    "freebase": [
        (
            "ExecuteSPARQL",
            {"sparql": 'ASK { ?e ns:type.object.name "Donald Trump"@en }'},
            "True",
        ),
        (
            "ExecuteSPARQL",
            {"sparql": """
            SELECT ?x WHERE {
                ?e ns:type.object.name "Donald Trump"@en ;
                   ns:people.person.place_of_birth ?x
            }
        """},
            "Queens",
        ),
        (
            "SearchNodes",
            {"query": "Donald Trump", "n_results": 2, "omit_length": 220},
            "Donald Trump",
        ),
        ("SearchTypes", {"query": "person", "n_results": 5}, "people.person"),
        (
            "SearchGraphPatterns",
            {
                "sparql": 'SELECT ?e WHERE { ?e ns:type.object.name "Donald Trump"@en }',
                "semantic": "place of birth",
                "topN_return": 12,
            },
            "people.person.place_of_birth",
        ),
        (
            "SearchGraphPatterns",
            {
                "sparql": 'SELECT ?e WHERE { ?e ns:type.object.name "Donald Trump"@en }',
                "semantic": "educational institution",
                "topN_return": 15,
            },
            "people.person.education -> education.education.institution",
        ),
    ],
    "wikidata-kqapro": [
        (
            "ExecuteSPARQL",
            {"sparql": 'ASK { ?e <pred:name> "Walt Disney Studios Motion Pictures" }'},
            "True",
        ),
        (
            "ExecuteSPARQL",
            {"sparql": """
            SELECT ?e WHERE { ?e <pred:name> "Walt Disney Studios Motion Pictures" }
        """},
            "Walt Disney Studios Motion Pictures",
        ),
        (
            "SearchNodes",
            {"query": "Walt Disney Studios Motion Pictures", "n_results": 5},
            "Walt Disney Studios Motion Pictures",
        ),
        (
            "SearchGraphPatterns",
            {
                "sparql": 'SELECT ?e WHERE { ?e <pred:name> "Walt Disney Studios Motion Pictures" }',
                "semantic": "distributor",
                "topN_return": 15,
            },
            "<distributor>",
        ),
        (
            "SearchGraphPatterns",
            {
                "sparql": 'SELECT ?e WHERE { ?e <pred:name> "Paris" }',
                "semantic": "population",
                "topN_return": 8,
            },
            '<pred:unit> "1"',
        ),
        (
            "SearchGraphPatterns",
            {
                "sparql": "SELECT ?e WHERE {}",
                "semantic": "population",
                "topN_return": 15,
            },
            "population",
        ),
    ],
}

# Check numeric ordering with LIMIT through both Virtuoso and MCP.
NUMERIC_ORDER = """
    PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT ?n WHERE {
        VALUES ?v { "63.0" "112.0" }
        BIND(STRDT(?v, xsd:double) AS ?n)
    } ORDER BY DESC(?n) LIMIT 1
"""


async def check(kb: str, base_url: str, http: httpx2.AsyncClient) -> None:
    """Initialize a live MCP session and verify the text returned by each call."""
    async with (
        streamable_http_client(f"{base_url}/{kb}/mcp", http_client=http) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        expected = {name for name, _, _ in CASES[kb]}
        if tools != expected:
            raise AssertionError(f"{kb}: expected tools {expected}, got {tools}")

        for name, arguments, expected_text in [
            *CASES[kb],
            ("ExecuteSPARQL", {"sparql": NUMERIC_ORDER}, "112.0"),
        ]:
            result = await session.call_tool(name, arguments)
            text = " ".join(part.text for part in result.content if part.type == "text")
            if result.is_error or expected_text.casefold() not in text.casefold():
                raise AssertionError(
                    f"{kb}/{name}: expected {expected_text!r}, got {text[:800]!r}"
                )
            print(f"PASS {kb}/{name}: {expected_text}")


async def main(args: Args) -> None:
    """Check process health, then test both knowledge bases through an MCP client."""
    base_url = args.base_url.rstrip("/")
    async with httpx2.AsyncClient(trust_env=False, timeout=180) as http:
        (await http.get(f"{base_url}/health")).raise_for_status()
        for kb in CASES:
            await check(kb, base_url, http)


if __name__ == "__main__":
    asyncio.run(main(tyro.cli(Args, description="Test the running KBQA MCP services.")))
