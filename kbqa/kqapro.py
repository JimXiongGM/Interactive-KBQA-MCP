"""Search Wikidata KQA Pro entities and graph patterns."""

import json
import re

from .retrieval import EDGE_TASK, NODE_TASK, embed, nearest, truncate
from .sparql import (
    ENTITY_SELECT,
    KQAPRO,
    QID,
    execute,
    label,
    literal,
    rows,
    value_node,
)


def search_nodes(
    query: str, n_results: int = 10, omit_length: int | None = None
) -> str:
    """Return semantically matching entities or concepts with their types and descriptions."""
    if not query.strip():
        raise ValueError("Query is required")
    (vector,) = embed(NODE_TASK, query)
    results = []
    for hit, _ in nearest(KQAPRO, "nodes", vector, n_results):
        text = f'"{hit["document"]}" | {hit.get("valid_types", "")}'
        if desc := hit.get("desc"):
            text += f" | Description: {desc}"
        results.append(truncate(text, omit_length))
    return str(results)


def execute_sparql(sparql: str, str_mode: bool = True) -> str:
    """Return the Wikidata KQA Pro SPARQL answer."""
    return execute(KQAPRO, sparql, str_mode)


def search_graph_patterns(
    sparql: str, semantic: str | None = None, topN_vec: int = 400, topN_return: int = 10
) -> str:
    """Find adjacent predicates and example facts, including values with units or dates."""
    if not (select := ENTITY_SELECT.search(sparql)):
        raise ValueError(
            "Graph pattern must select ?e. Use ExecuteSPARQL for other queries."
        )
    predicates_only = len(re.findall(r"\?e\b", sparql)) == 1
    ranking = {}
    if semantic or predicates_only:
        (vector,) = embed(EDGE_TASK, (semantic or "entity").replace("_", " "))
        ranking = {
            hit["document"].replace(" ", "_"): score
            for hit, score in nearest(KQAPRO, "edges", vector, topN_vec)
        }
    if predicates_only:
        return "[" + ", ".join(f"<{p}>" for p in list(ranking)[:topN_return]) + "]"

    # Join neighborhood edges directly to the input pattern before sampling.
    prefix = sparql[: select.start()]
    _, brace, body = sparql[select.end() :].partition("{")
    if not brace:
        raise ValueError("Graph pattern must contain a WHERE block.")
    results = {}
    for incoming in (False, True):
        node, predicate = ("?_h", "?_pin") if incoming else ("?_t", "?_pout")
        triple = f"{node} {predicate} ?e" if incoming else f"?e {predicate} {node}"
        sample = f"(SAMPLE({node}) as {node})"
        columns = f"{sample} {predicate}" if incoming else f"{predicate} {sample}"
        neighbors = rows(
            KQAPRO,
            f"{prefix}SELECT DISTINCT {columns} WHERE {{ {triple} . FILTER ({node} != ?e) {body}",
            timeout=30,
        )
        for hit in neighbors:
            pred = hit[predicate[1:]]["value"]
            if pred in results or "://" in pred:
                continue
            binding = hit[node[1:]]
            value = binding["value"]

            # Show entity names, preserve literal types, and expand KQA Pro value nodes.
            if binding["type"] == "uri" and QID.fullmatch(value):
                name = label(KQAPRO, value)
                if not name:
                    continue
                value = json.dumps(name, ensure_ascii=False)
            elif (
                not incoming
                and value.startswith("nodeID://")
                and (record := value_node(value))
            ):
                value_pred = record["p"]["value"]
                term = (
                    record["v"]["value"]
                    if value_pred == "pred:year"
                    else literal(record["v"])
                )
                example = f"?e, <{pred}> -> ?pv . ?pv <{value_pred}> {term}"
                if unit := record.get("unit"):
                    example += f" . ?pv <pred:unit> {literal(unit)}"
                results[pred] = f"({example})"
                continue
            elif not incoming and binding["type"] in {"literal", "typed-literal"}:
                value = literal(binding)
            else:
                continue
            results[pred] = (
                f"({value}, <{pred}>, ?e)" if incoming else f"(?e, <{pred}>, {value})"
            )

    ordered = sorted(results, key=lambda pred: -ranking.get(pred, -2))
    return "[" + ", ".join(results[pred] for pred in ordered[:topN_return]) + "]"
