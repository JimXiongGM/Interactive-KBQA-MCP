"""Query both local graph databases and format their SPARQL results."""

import itertools
import json
import os
import re
import threading
from functools import cache, lru_cache

from .freebase_compat import add_not_exists_or_exists_filter
from .retrieval import HTTP

FREEBASE = "freebase"
KQAPRO = "wikidata_kqapro"
NS = "http://rdf.freebase.com/ns/"
XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIX_NS = f"PREFIX ns: <{NS}>\n"
ENGLISH = "(!isLiteral(?x) || lang(?x) = '' || langMatches(lang(?x), 'en'))"
MID = re.compile(r"[mg]\.[A-Za-z0-9_]+")
QID = re.compile(r"Q[0-9]+")
ENTITY_SELECT = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?\?e\s+WHERE\b", re.IGNORECASE)
KQA_PREDS = {
    "value",
    "instance_of",
    "name",
    "unit",
    "year",
    "date",
    "fact_h",
    "fact_r",
    "fact_t",
}
FLOAT_VALUES = re.compile(
    r'VALUES\s+(\?\w+)\s*\{\s*"([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)"\^\^'
    r"(?:xsd:|<http://www\.w3\.org/2001/XMLSchema#)(?:float|double)>?\s*\}",
    re.IGNORECASE,
)
IRI_OR_LITERAL = re.compile(
    r'''"""(?:\\.|(?!""")[\s\S])*"""|'''
    r"""'''(?:\\.|(?!''')[\s\S])*'''|"""
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|<pred:([^<>\s]+)>|<[^<>\s]*>|\#[^\n]*"""
)


@cache
def _endpoints(kb: str) -> tuple[itertools.cycle, threading.Lock]:
    """Read one or more SPARQL endpoints (space- or comma-separated) and rotate across them."""
    if kb == FREEBASE:
        raw = os.environ.get(
            "FREEBASE_SPARQL_ENDPOINT", "http://127.0.0.1:29501/sparql"
        )
    else:
        raw = os.environ.get(
            "WIKIDATA_KQAPRO_SPARQL_ENDPOINT", "http://127.0.0.1:29500/sparql"
        )
    urls = [url for url in re.split(r"[\s,]+", raw) if url]
    if not urls:
        raise ValueError(f"No SPARQL endpoint configured for {kb}")
    return itertools.cycle(urls), threading.Lock()


def _next_endpoint(kb: str) -> str:
    """Pick the next endpoint in round-robin order; thread-safe."""
    urls, lock = _endpoints(kb)
    with lock:
        return next(urls)


def query(kb: str, sparql: str, *, timeout: float = 120) -> dict:
    """Query Virtuoso over POST and preserve column order, language tags, and datatypes."""
    endpoint = _next_endpoint(kb)
    if kb == FREEBASE:
        if not re.search(r"\bPREFIX\s+ns\s*:", sparql, re.IGNORECASE):
            sparql = PREFIX_NS + sparql
    else:

        def replace_predicate(match: re.Match[str]) -> str:
            """Map KQA Pro query predicates to their IRIs in the snapshot."""
            pred = match[1]
            return f"<{pred}>" if pred and pred not in KQA_PREDS else match[0]

        sparql = IRI_OR_LITERAL.sub(replace_predicate, sparql)

    response = HTTP.post(
        endpoint,
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    if response.is_error:
        raise ValueError(
            f"SPARQL HTTP {response.status_code}: {response.text.strip()[:400]}"
        )
    result = response.json()
    # Virtuoso build 3242 serializes ASK as a SELECT result with an internal variable.
    if result.get("head", {}).get("vars") == ["__ASK_RETVAL"]:
        bindings = result["results"]["bindings"]
        return {
            "boolean": bool(
                bindings and bindings[0]["__ASK_RETVAL"]["value"] in {"1", "true"}
            )
        }
    return result


def rows(kb: str, sparql: str, *, timeout: float = 120) -> list[dict]:
    """Extract SELECT bindings for entity lookup and graph expansion."""
    return query(kb, sparql, timeout=timeout)["results"]["bindings"]


@lru_cache(maxsize=262144)
def label(kb: str, identifier: str) -> str:
    """Resolve the name of a Freebase MID or a KQA Pro QID."""
    if kb == FREEBASE:
        pattern = f"ns:{identifier} ns:type.object.name ?x . FILTER {ENGLISH}"
    else:
        pattern = f"<{identifier}> <pred:name> ?x"
    result = rows(kb, f"SELECT ?x WHERE {{ {pattern} }} LIMIT 1")
    return result[0]["x"]["value"] if result else ""


def literal(binding: dict) -> str:
    """Preserve escaping, language tags, and datatypes in graph examples."""
    value = json.dumps(binding["value"], ensure_ascii=False)
    language = binding.get("xml:lang") or binding.get("lang")
    if language:
        return f"{value}@{language}"
    if datatype := binding.get("datatype"):
        suffix = (
            f"xsd:{datatype.removeprefix(XSD)}"
            if datatype.startswith(XSD)
            else f"<{datatype}>"
        )
        return f"{value}^^{suffix}"
    return value


@lru_cache(maxsize=4096)
def value_node(identifier: str) -> dict:
    """Read KQA Pro value nodes with their units and date predicates."""
    if not re.fullmatch(r"nodeID://[\w-]+", identifier):
        return {}
    result = rows(
        KQAPRO,
        f"""
        SELECT ?p ?v ?unit WHERE {{
            <{identifier}> ?p ?v .
            FILTER (?p IN (<pred:value>, <pred:date>, <pred:year>))
            OPTIONAL {{ <{identifier}> <pred:unit> ?unit }}
        }} LIMIT 1
    """,
    )
    return result[0] if result else {}


def execute(kb: str, sparql: str, str_mode: bool = True) -> str:
    """Run an answer query and render entity IDs and KQA Pro value nodes as readable results."""
    if not sparql.strip():
        raise ValueError("SPARQL query is required")
    # Apply date-range compatibility and retry empty float VALUES matches with a tolerance.
    if kb == FREEBASE:
        sparql = (
            add_not_exists_or_exists_filter(PREFIX_NS + sparql)
            if not re.search(r"\bPREFIX\s+ns\s*:", sparql, re.IGNORECASE)
            else add_not_exists_or_exists_filter(sparql)
        )
    result = query(kb, sparql)
    if kb == FREEBASE and result.get("results", {}).get("bindings") == []:
        retry = FLOAT_VALUES.sub(r"FILTER (abs(xsd:double(\1) - \2) < 1e-4)", sparql)
        if retry != sparql:
            result = query(kb, retry)
    if "boolean" in result:
        return str(result["boolean"])

    # Preserve SELECT column order and Virtuoso row order, including unbound OPTIONAL values.
    output = []
    columns = result["head"]["vars"]
    for row in result["results"]["bindings"]:
        values = []
        for column in columns:
            binding = row.get(column)
            if binding is None:
                values.append(None)
                continue
            value = binding["value"]
            if kb == FREEBASE:
                identifier = value.removeprefix(NS)
                if binding["type"] == "uri" and MID.fullmatch(identifier):
                    value = label(kb, identifier)
                elif binding["type"] == "typed-literal" and binding.get("datatype") in {
                    XSD + name
                    for name in (
                        "date",
                        "dateTime",
                        "float",
                        "gYear",
                        "gYearMonth",
                        "integer",
                    )
                }:
                    value = literal(binding)
                elif binding["type"] == "uri":
                    value = identifier
            elif binding["type"] == "uri" and QID.fullmatch(value):
                value = label(kb, value) or value
            elif value.startswith("nodeID://") and (record := value_node(value)):
                value = record["v"]["value"]
                if "unit" in record or record["p"]["value"] == "pred:date":
                    datatype = "double" if "unit" in record else "date"
                    value = f'"{value}"^^xsd:{datatype}'
            values.append(value)
        if kb == FREEBASE:
            output.extend(value for value in values if value)
        elif len(values) == 1:
            output.extend(values)
        else:
            output.append(tuple(values))

    text = str(
        list(dict.fromkeys(output)) if kb == FREEBASE or len(columns) == 1 else output
    )
    return f"{text[:2000]}..." if str_mode and len(text) > 2000 else text
