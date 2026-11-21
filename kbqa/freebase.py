"""Search Freebase entities, types, and graph patterns with CVT paths."""

import json
import re
import sqlite3
from contextlib import closing
from functools import cache

import httpx2
import tantivy
from loguru import logger

from .retrieval import DATA, GRAPH_TASK, TYPE_TASK, embed, nearest, truncate
from .sparql import (
    ENGLISH,
    ENTITY_SELECT,
    FREEBASE,
    MID,
    NS,
    execute,
    label,
    literal,
    rows,
)

DIRECTORY = DATA / FREEBASE
PREDICATE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)+")


@cache
def name_index() -> tantivy.Index:
    """Open the prebuilt English entity name index."""
    return tantivy.Index.open(str(DIRECTORY / "names"))


@cache
def cvt_predicates() -> frozenset[str]:
    """Load the Freebase predicates whose objects are CVTs."""
    return frozenset(
        (DIRECTORY / "fb_properties_expecting_cvt.txt").read_text().splitlines()
    )


def search_nodes(
    query: str, n_results: int = 10, omit_length: int | None = None
) -> str:
    """Find candidate entity names and use FACC1 and Freebase to add English descriptions."""
    query = query.strip()
    if not query:
        raise ValueError("Query is required")
    index = name_index()
    searcher = index.searcher()
    try:
        parsed = index.parse_query(query, default_field_names=["name"])
    except ValueError:
        phrase = query.replace("\\", "\\\\").replace('"', '\\"')
        parsed = index.parse_query(f'"{phrase}"', default_field_names=["name"])
    hits = searcher.search(parsed, limit=100).hits
    names = [searcher.doc(address).get_first("name") for _, address in hits]
    names = sorted(
        (name for name in names if name), key=lambda name: name.lower() != query.lower()
    )
    results = []
    seen = set()

    # Keep distinct case variants for exact matches, as in the deployed tool.
    for name in names:
        if name.lower() != query.lower() or name in seen:
            continue
        descriptions = rows(
            FREEBASE,
            f"""
            SELECT DISTINCT ?x WHERE {{
                ?e ns:type.object.name {json.dumps(name, ensure_ascii=False)}@en .
                ?e ns:common.topic.description ?x .
                FILTER {ENGLISH}
            }}
        """,
        )
        description = "".join(
            f"| Description: {row['x']['value']}"
            for row in descriptions
            if row["x"]["value"]
        )
        results.append(
            truncate(f"{name} {description or '| No description.'}", omit_length)
        )
        seen.add(name)

    # Resolve other candidates in FACC1 order before deduplicating their canonical names.
    with closing(
        sqlite3.connect(
            f"{(DIRECTORY / 'name_to_mids.db').as_uri()}?mode=ro&immutable=1", uri=True
        )
    ) as db:
        for name in names:
            record = db.execute(
                "SELECT mids FROM name_mids_en_clean WHERE name = ?", (name.lower(),)
            ).fetchone()
            description = ""
            for mid in json.loads(record[0]) if record else ():
                if not MID.fullmatch(mid):
                    continue
                matches = rows(
                    FREEBASE,
                    f"""
                    SELECT DISTINCT ?x WHERE {{
                        ns:{mid} ns:common.topic.description ?x . FILTER {ENGLISH}
                    }}
                """,
                )
                if matches and matches[0]["x"]["value"]:
                    description = matches[0]["x"]["value"]
                    name = label(FREEBASE, mid)
                    break
            if name.lower() in seen or name in seen:
                continue
            seen.add(name.lower())
            text = (
                f"{name} | Description: {description}"
                if description
                else f"{name} | No description."
            )
            results.append(truncate(text, omit_length))
            if len(results) == n_results:
                break
    return str(results)


@cache
def type_words() -> list[tuple[str, frozenset[str]]]:
    """Prepare lexical candidates from the entire Freebase type index."""
    directory = DIRECTORY / "vectors" / "types" / "records.jsonl"
    with directory.open() as stream:
        names = [json.loads(line)["document"] for line in stream if line.strip()]
    return [
        (name, frozenset(name.rsplit(".", 1)[-1].lower().replace("_", " ").split()))
        for name in names
    ]


def search_types(query: str, n_results: int = 10) -> str:
    """Rank all types by Jaccard overlap, then append semantic matches."""
    query = query.strip().lower().replace("_", " ").strip()
    if not query:
        raise ValueError("Query is required")
    words = set(query.split())
    lexical = [
        (len(words & tokens) / len(words | tokens), name)
        for name, tokens in type_words()
    ]
    lexical.sort(key=lambda item: item[0], reverse=True)
    result = [name for score, name in lexical[:n_results] if score > 0.3]
    try:
        (vector,) = embed(TYPE_TASK, query)
        hits = nearest(FREEBASE, "types", vector, max(50, n_results))
        result.extend(
            hit["document"]
            for hit, _ in hits[:n_results]
            if hit["document"] not in result
        )
    except (httpx2.HTTPError, ValueError) as error:
        logger.warning("Freebase type embedding failed: {}", error)
    return str(result[:n_results])


def execute_sparql(sparql: str, str_mode: bool = True) -> str:
    """Return the Freebase SPARQL answer."""
    return execute(FREEBASE, sparql, str_mode)


def search_graph_patterns(
    sparql: str, semantic: str | None = None, topN_vec: int = 400, topN_return: int = 10
) -> str:
    """Bind ?e and enumerate incoming edges, outgoing edges, and semantically ranked CVT paths."""
    if not ENTITY_SELECT.search(sparql):
        raise ValueError(
            "Graph pattern must select ?e. Use ExecuteSPARQL for other queries."
        )

    # Limit neighborhood expansion to a bounded set of valid MIDs.
    entities = list(
        dict.fromkeys(
            identifier
            for row in rows(FREEBASE, sparql, timeout=60)
            if (term := row.get("e", {})).get("type") == "uri"
            and (value := term["value"]).startswith(NS)
            and MID.fullmatch(identifier := value.removeprefix(NS))
        )
    )[:50]
    if not entities:
        return "[]"
    entity_values = " ".join(f"ns:{identifier}" for identifier in entities)

    # Use semantic hits to constrain the query and rank one-hop and two-hop paths together.
    rank = {}
    direct_terms = set()
    pair_terms = set()
    if semantic:
        relation = " ".join(semantic.replace(".", " . ").replace("_", " ").split())
        direct, walk = embed(GRAPH_TASK, relation, f'a predicate pair of "{relation}"')
        direct_hits = nearest(FREEBASE, "predicates", direct, topN_vec)
        pair_hits = nearest(FREEBASE, "cvt_predicate_pairs", walk, topN_vec)
        predicates = [
            (hit["document"].replace(" ", ""), score) for hit, score in direct_hits
        ]
        pairs = [
            (tuple(hit["document"].replace(" ", "").split("->")), score)
            for hit, score in pair_hits
        ]
        rank.update(predicates)
        rank.update((" -> ".join(pair), score) for pair, score in pairs)
        direct_terms = {p for p, _ in predicates[:150] if PREDICATE.fullmatch(p)}
        pair_terms = {
            (pair[0], pair[1])
            for pair, _ in pairs
            if len(pair) == 2 and all(map(PREDICATE.fullmatch, pair))
        }
        pair_terms = set(
            list(dict.fromkeys(pair for pair, _ in pairs if pair in pair_terms))[:80]
        )
        remaining = 150 - len(direct_terms)
        direct_terms.update(
            sorted({first for first, _ in pair_terms} - direct_terms)[:remaining]
        )

    failures = []

    def expand(incoming: bool, filtered: bool) -> list[tuple[str, str]]:
        """Sample one direction and retry without semantic constraints when it is empty."""
        facts = []
        cvt_heads = {p for p, _ in pair_terms} if filtered else set()
        for via_cvt in (False, True):
            if via_cvt and not incoming and not cvt_heads:
                continue
            if incoming:
                names = (
                    ("?_pin1", "?_pin2", "?_h0", "?_h", "?_h_name")
                    if via_cvt
                    else ("?_pin", "", "?_h0", "?_h", "?_h_name")
                )
            else:
                names = (
                    ("?_pout", "?_pout2", "?_t20", "?_t2", "?_t2_name")
                    if via_cvt
                    else ("?_pout", "", "?_t0", "?_t", "?_t_name")
                )
            p, q, raw, node, name = names
            source, target = (raw, "?e") if incoming else ("?e", raw)
            columns = f"{p} {q}" if via_cvt else p
            edges = (
                f"{source} {p} ?_c . ?_c {q} {target}"
                if via_cvt
                else f"{source} {p} {target}"
            )
            selected = (
                cvt_heads
                if via_cvt and not incoming
                else direct_terms if filtered and not via_cvt else set()
            )
            values = (
                f"VALUES {p} {{ "
                + " ".join(f"ns:{term}" for term in sorted(selected))
                + " } "
                if selected
                else ""
            )
            if via_cvt and filtered and pair_terms:
                values += (
                    f"VALUES ({p} {q}) {{ "
                    + " ".join(f"(ns:{a} ns:{b})" for a, b in sorted(pair_terms))
                    + " } "
                )
            pattern = (
                f"SELECT {columns} {node} {name} WHERE {{ "
                f"{{ SELECT {columns} (SAMPLE({raw}) AS {node}) WHERE {{ "
                f"VALUES ?e {{ {entity_values} }} {values}{edges} . "
                f"FILTER ({raw} != ?e) }} GROUP BY {columns} }} "
                f"OPTIONAL {{ {node} ns:type.object.name {name} . "
                f"FILTER(!isliteral({name}) or lang({name})='' or langmatches(lang({name}),'en')) }} }}"
            )
            try:
                neighbors = rows(FREEBASE, pattern, timeout=30)
            except (ValueError, httpx2.HTTPError) as error:
                failures.append(error)
                logger.warning(
                    "Freebase graph branch failed (incoming={}, cvt={}): {}",
                    incoming,
                    via_cvt,
                    error,
                )
                continue
            for hit in neighbors:
                first = hit[p[1:]]["value"].removeprefix(NS)
                if not incoming and not via_cvt and first in cvt_predicates():
                    if PREDICATE.fullmatch(first) and not first.startswith(
                        ("common.", "freebase.")
                    ):
                        cvt_heads.add(first)
                    continue
                path = first
                if via_cvt:
                    path += " -> " + hit[q[1:]]["value"].removeprefix(NS)
                if (
                    path.startswith(("common.", "freebase."))
                    or path == "type.object.name"
                    or any(
                        word in path
                        for word in ("has_no_value", "has_value", "freebase.")
                    )
                ):
                    continue
                term = hit[node[1:]]
                if found := hit.get(name[1:]):
                    value = json.dumps(found["value"], ensure_ascii=False)
                elif not incoming and term["type"] in {"literal", "typed-literal"}:
                    value = literal(term)
                else:
                    continue
                fact = (
                    f"({value}, {path}, ?e)" if incoming else f"(?e, {path}, {value})"
                )
                facts.append((path, fact))
        if filtered and not facts:
            return expand(incoming, False)
        return facts

    outgoing = expand(False, bool(semantic))
    seen = {path for path, _ in outgoing}
    incoming = expand(True, bool(semantic))
    facts = outgoing + [(path, fact) for path, fact in incoming if path not in seen]
    if not facts and len(failures) >= 4:
        raise failures[0]
    facts.sort(key=lambda item: (-rank.get(item[0], -2), item[0]))
    return "[" + ", ".join(fact for _, fact in facts[:topN_return]) + "]"
