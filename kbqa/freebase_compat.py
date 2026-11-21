"""Handle missing Freebase dates and Virtuoso literal compatibility in simple graph patterns."""

import re
import threading

from pyparsing import ParseBaseException, ParseResults
from rdflib import Literal
from rdflib.plugins.sparql.parser import parseQuery

_PARSE_LOCK = threading.Lock()
_TOKEN = re.compile(
    r'''(?P<comment>\#[^\n]*)|(?P<string>"""(?:\\.|(?!""")[\s\S])*"""'''
    r"""|'''(?:\\.|(?!''')[\s\S])*'''"""
    r"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    r"""(?:\s*@[A-Za-z]+(?:-[A-Za-z0-9]+)*|\^\^(?:[A-Za-z_][\w-]*:[\w.-]+|<[^<>\s]+>))?"""
    r"""|<[^<>\s"{}|^`\\]*>|[?$][A-Za-z_]\w*|[A-Za-z_][\w-]*:[\w.-]*"""
    r"""|[A-Za-z_]\w*|&&|\|\||<=|>=|!=|[^\s]"""
)
_VARIABLE = re.compile(r"[?$][A-Za-z_]\w*\Z")
_DATE_LITERAL = re.compile(
    r"""["'][^"']+["']\^\^xsd:(?:date|dateTime|gYear|gYearMonth)\Z"""
)


def _query_tokens(query: str):
    """Parse source spans and require an explicit projection to keep local variables out of the result."""
    try:
        with _PARSE_LOCK:
            parsed = parseQuery(query)[1]
    except ParseBaseException:
        return None
    if parsed.name != "SelectQuery":
        return None
    pending = [(parsed, False)]
    while pending:
        value, projection_expression = pending.pop()
        if projection_expression and isinstance(value, Literal) and value.language:
            return None
        if isinstance(value, dict):
            if (
                projection_expression
                and getattr(value, "name", "") == "literal"
                and "lang" in value
            ):
                return None
            if getattr(value, "name", "") in {"SelectQuery", "SubSelect"}:
                if "projection" not in value:
                    return None
                pending.extend(
                    (entry["expr"], True)
                    for entry in value["projection"]
                    if "expr" in entry
                )
            pending.extend((child, projection_expression) for child in value.values())
        elif isinstance(value, (list, tuple, ParseResults)):
            pending.extend((child, projection_expression) for child in value)
    tokens = [m for m in _TOKEN.finditer(query) if m.lastgroup != "comment"]
    values = [token.group().casefold() for token in tokens]
    # DISTINCT * compares full mappings, so new local variables could change aggregate counts.
    if any(
        values[i : i + 5] == ["count", "(", "distinct", "*", ")"]
        for i in range(len(values))
    ):
        return None
    return parsed, tokens


def add_not_exists_or_exists_filter(query: str) -> str:
    """Relax paired dates on one CVT while preserving other filters, casts, and query scope."""
    if ".from" not in query or ".to" not in query:
        return query
    parsed_tokens = _query_tokens(query)
    if parsed_tokens is None:
        return query
    parsed, tokens = parsed_tokens
    # Apply date compatibility to a flat graph pattern without crossing UNION, OPTIONAL, or subqueries.
    parts = parsed["where"].get("part", [])
    braces = [i for i, token in enumerate(tokens) if token.group() in {"{", "}"}]
    if (
        any("var" not in entry or "expr" in entry for entry in parsed["projection"])
        or any(part.name not in {"TriplesBlock", "Filter"} for part in parts)
        or len(braces) != 2
        or tokens[braces[0]].group() != "{"
    ):
        return query
    left, right = braces
    triples, filters = {}, []
    i = left + 1
    while i < right:
        # Keep unsupported filters intact and protect their variables with reference checks.
        if tokens[i].group().casefold() == "filter":
            start = i
            if i + 1 >= right or tokens[i + 1].group() != "(":
                return query
            depth, end = 1, i + 2
            while end < right and depth:
                depth += (tokens[end].group() == "(") - (tokens[end].group() == ")")
                end += 1
            if depth:
                return query
            body = tokens[i + 2 : end - 1]
            clauses, chunk = [], []
            for token in body:
                if token.group() == "&&":
                    clauses.append(chunk)
                    chunk = []
                else:
                    chunk.append(token)
            clauses.append(chunk)
            conditions = []
            for clause in clauses:
                values = [token.group() for token in clause]
                if len(values) == 3 and _VARIABLE.fullmatch(values[0]):
                    var, op, literal = values
                elif (
                    len(values) == 6
                    and values[0] in {"xsd:dateTime", "xsd:date", "xsd:datetime"}
                    and values[1] == "("
                    and _VARIABLE.fullmatch(values[2])
                    and values[3] == ")"
                ):
                    var, op, literal = values[2], values[4], values[5]
                else:
                    conditions = []
                    break
                if op not in {"<", "<=", ">", ">="} or not _DATE_LITERAL.fullmatch(
                    literal
                ):
                    conditions = []
                    break
                conditions.append(
                    (var, op, query[clause[0].start() : clause[-1].end()])
                )
            if conditions:
                filters.append(
                    (tokens[start].start(), tokens[end - 1].end(), conditions)
                )
            i = end
            continue
        if i + 2 < right:
            subject, predicate, variable = [
                token.group() for token in tokens[i : i + 3]
            ]
            if (
                _VARIABLE.fullmatch(subject)
                and _VARIABLE.fullmatch(variable)
                and predicate.startswith("ns:")
                and predicate.endswith((".from", ".to"))
            ):
                end = i + 3
                if end < right and tokens[end].group() == ".":
                    end += 1
                triples.setdefault(variable, []).append(
                    (
                        subject,
                        predicate,
                        tokens[i].start(),
                        tokens[end - 1].end(),
                        query[tokens[i].start() : tokens[i + 2].end()],
                    )
                )
                i = end
                continue
        i += 1
    # Relax only date variables used in one edge and one comparison. Preserve projected, sorted, or reused variables.
    conditions = {}
    for _, _, group in filters:
        for variable, op, expression in group:
            conditions.setdefault(variable, []).append((op, expression))
    pairs = {}
    for variable, entries in triples.items():
        if (
            len(entries) != 1
            or len(conditions.get(variable, [])) != 1
            or sum(token.group() == variable for token in tokens) != 2
        ):
            continue
        subject, predicate, *_ = entries[0]
        base, suffix = predicate.rsplit(".", 1)
        op = conditions[variable][0][0]
        if (suffix == "from" and op in {"<", "<="}) or (
            suffix == "to" and op in {">", ">="}
        ):
            pairs.setdefault((subject, base), {}).setdefault(suffix, []).append(
                variable
            )
    selected = set()
    for pair in pairs.values():
        if len(pair.get("from", [])) == len(pair.get("to", [])) == 1:
            selected.update(pair["from"] + pair["to"])
    # Keep a mixed FILTER intact if some of its conditions cannot be rewritten.
    for _, _, group in filters:
        variables = {var for var, _, _ in group}
        if variables & selected and not variables <= selected:
            return query
    if not selected:
        return query
    replacements = []
    for variable in selected:
        _, _, start, end, _ = triples[variable][0]
        replacements.append((start, end, ""))
    for start, end, group in filters:
        if not all(var in selected for var, _, _ in group):
            continue
        rewritten = []
        for variable, _, expression in group:
            triple = triples[variable][0][-1]
            rewritten.append(
                f"FILTER (NOT EXISTS {{ {triple} . }} || "
                f"EXISTS {{ {triple} . FILTER ({expression}) }})"
            )
        replacements.append((start, end, " ".join(rewritten)))
    for start, end, text in sorted(replacements, reverse=True):
        query = query[:start] + text + query[end:]
    return query
