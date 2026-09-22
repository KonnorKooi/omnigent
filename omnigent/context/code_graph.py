"""Loading and querying a graphify code graph (``graph/graph.json``).

graphify writes a networkx node-link document: ``nodes[]`` (``id``, ``label``,
``community``, ``source_file``, ``source_location``, ...) and ``links[]``
(``source``, ``target``, ``relation``). The graph is a *navigation aid* for
agents, not a source of truth (``designs/PROJECT_CONTEXT.md`` §2), so the
query here is deliberately simple: label/path token matching to pick seed
nodes, then a bounded breadth-first expansion rendered as text.

graphify itself is not a dependency of Omnigent; this module never imports it.
Parsed graphs are cached per ``(path, mtime_ns, size)`` so repeated tool calls
do not re-read a multi-megabyte JSON file.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from collections import deque
from pathlib import Path
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

#: Largest graph.json the server will parse.
MAX_GRAPH_BYTES = 64 * 1024 * 1024
#: Hard cap on nodes returned to the UI in one response.
MAX_UI_NODES = 5000
#: Hard cap on neighbours returned by :func:`neighbors`.
MAX_NEIGHBORS = 200
#: Seeds picked by :func:`query` before expansion.
_QUERY_SEEDS = 6

_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "what",
        "where", "which", "how", "does", "are", "was", "were", "who", "why",
        "when", "use", "used", "uses", "using", "about", "there", "their",
        "have", "has", "can", "not", "all", "any", "get", "set",
    }
)  # fmt: skip
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclasses.dataclass
class CodeGraph:
    """An in-memory graphify graph with adjacency precomputed.

    :param nodes: Node dicts keyed by id (original attributes preserved).
    :param edges: ``(source, target, relation, attrs)`` tuples.
    :param adjacency: id → list of ``(neighbor_id, relation, direction, attrs)``
        where direction is ``"out"`` or ``"in"``.
    """

    nodes: dict[str, dict[str, Any]]
    edges: list[tuple[str, str, str, dict[str, Any]]]
    adjacency: dict[str, list[tuple[str, str, str, dict[str, Any]]]]

    def degree(self, node_id: str) -> int:
        """Return a node's undirected degree.

        :param node_id: The node id.
        :returns: Number of incident edges.
        """
        return len(self.adjacency.get(node_id, ()))


_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int], CodeGraph]] = {}


def _parse(document: Any) -> CodeGraph:
    """Build a :class:`CodeGraph` from a node-link JSON document.

    :param document: The decoded JSON.
    :returns: The parsed graph.
    :raises OmnigentError: 400 when the document is not node-link shaped.
    """
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise OmnigentError(
            "graph.json is not a node-link graph (missing nodes[])",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_links = document.get("links")
    if raw_links is None:
        raw_links = document.get("edges", [])
    nodes: dict[str, dict[str, Any]] = {}
    for raw in document["nodes"]:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        node_id = str(raw["id"])
        nodes[node_id] = raw
    edges: list[tuple[str, str, str, dict[str, Any]]] = []
    adjacency: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {}
    for raw in raw_links if isinstance(raw_links, list) else []:
        if not isinstance(raw, dict):
            continue
        source, target = raw.get("source"), raw.get("target")
        if source is None or target is None:
            continue
        source, target = str(source), str(target)
        if source not in nodes or target not in nodes:
            continue
        relation = str(raw.get("relation") or "related")
        edges.append((source, target, relation, raw))
        adjacency.setdefault(source, []).append((target, relation, "out", raw))
        adjacency.setdefault(target, []).append((source, relation, "in", raw))
    return CodeGraph(nodes=nodes, edges=edges, adjacency=adjacency)


def load_code_graph(path: Path) -> CodeGraph | None:
    """Load (or return the cached) graph at ``path``.

    :param path: The ``graph.json`` file.
    :returns: The parsed graph, or ``None`` when the file does not exist.
    :raises OmnigentError: 400 when the file is too large or malformed.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size > MAX_GRAPH_BYTES:
        raise OmnigentError(
            f"graph.json is too large ({stat.st_size} bytes)", code=ErrorCode.INVALID_INPUT
        )
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OmnigentError(
            f"graph.json could not be read: {exc}", code=ErrorCode.INVALID_INPUT
        ) from exc
    graph = _parse(document)
    with _cache_lock:
        _cache[key] = (stamp, graph)
    return graph


def _node_summary(graph: CodeGraph, node_id: str) -> dict[str, Any]:
    """Project a node onto the fields the UI and tools use.

    :param graph: The graph.
    :param node_id: Node id.
    :returns: A JSON-safe summary dict.
    """
    raw = graph.nodes[node_id]
    return {
        "id": node_id,
        "label": str(raw.get("label") or node_id),
        "community": raw.get("community"),
        "community_name": raw.get("community_name"),
        "source_file": raw.get("source_file"),
        "source_location": raw.get("source_location"),
        "file_type": raw.get("file_type"),
        "degree": graph.degree(node_id),
    }


def ui_graph(graph: CodeGraph, limit: int) -> dict[str, Any]:
    """Return the highest-degree ``limit`` nodes and the edges among them.

    :param graph: The graph.
    :param limit: Requested node cap (clamped to ``1..MAX_UI_NODES``).
    :returns: ``{"nodes", "edges", "total_nodes", "total_edges", "truncated"}``.
    """
    limit = max(1, min(limit, MAX_UI_NODES))
    ranked = sorted(graph.nodes, key=lambda nid: (-graph.degree(nid), nid))
    kept = ranked[:limit]
    kept_set = set(kept)
    edges = [
        {"source": s, "target": t, "relation": rel}
        for s, t, rel, _ in graph.edges
        if s in kept_set and t in kept_set
    ]
    return {
        "nodes": [_node_summary(graph, nid) for nid in kept],
        "edges": edges,
        "total_nodes": len(graph.nodes),
        "total_edges": len(graph.edges),
        "truncated": len(kept) < len(graph.nodes),
    }


def _tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens of ``text`` minus stop words.

    Identifiers are split on case and underscores so ``MetricAccumulator``
    matches a question mentioning "metric".

    :param text: Free text or an identifier.
    :returns: Tokens, in order.
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [
        tok
        for tok in _TOKEN_RE.findall(spaced.lower())
        if len(tok) >= 3 and tok not in _STOP_WORDS
    ]


def _score_nodes(graph: CodeGraph, question: str) -> list[tuple[float, str]]:
    """Rank nodes by how well their label and file match ``question``.

    :param graph: The graph.
    :param question: The natural-language query.
    :returns: ``(score, node_id)`` pairs with positive score, best first.
    """
    q_tokens = set(_tokens(question))
    q_lower = question.lower()
    if not q_tokens:
        return []
    scored: list[tuple[float, str]] = []
    for node_id, raw in graph.nodes.items():
        label = str(raw.get("label") or node_id)
        label_tokens = set(_tokens(label))
        file_tokens = set(_tokens(str(raw.get("source_file") or "")))
        score = 3.0 * len(q_tokens & label_tokens) + 1.0 * len(q_tokens & file_tokens)
        if len(label) >= 3 and label.lower() in q_lower:
            score += 5.0
        if score > 0:
            # Prefer well-connected nodes on ties: they are better entry points.
            scored.append((score + min(graph.degree(node_id), 50) / 100.0, node_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return scored


def _location(raw: dict[str, Any]) -> str:
    """Format ``source_file:source_location`` for display.

    :param raw: A node or edge attribute dict.
    :returns: e.g. ``"evaluation/common.py:L35"``, or ``""``.
    """
    source_file = raw.get("source_file")
    if not source_file:
        return ""
    loc = raw.get("source_location")
    return f"{source_file}:{loc}" if loc else str(source_file)


def query(graph: CodeGraph, question: str, budget_tokens: int) -> dict[str, Any]:
    """Answer a question with relevant nodes and edges, rendered as text.

    :param graph: The graph.
    :param question: Natural-language question, e.g. "how is PSNR computed".
    :param budget_tokens: Approximate output budget (4 chars ≈ 1 token).
    :returns: ``{"text", "seeds", "node_ids", "truncated"}``.
    """
    budget_chars = max(400, min(budget_tokens, 20000) * 4)
    seeds = [nid for _, nid in _score_nodes(graph, question)[:_QUERY_SEEDS]]
    if not seeds:
        return {
            "text": f"No code-graph nodes matched {question!r}.",
            "seeds": [],
            "node_ids": [],
            "truncated": False,
        }
    lines: list[str] = ["Matching nodes:"]
    for nid in seeds:
        summary = _node_summary(graph, nid)
        loc = _location(graph.nodes[nid])
        lines.append(f"- {summary['label']} [{loc}] (degree {summary['degree']})")
    lines.append("")
    lines.append("Relationships:")
    visited: set[str] = set(seeds)
    seen_edges: set[tuple[str, str, str]] = set()
    frontier: deque[tuple[str, int]] = deque((nid, 0) for nid in seeds)
    used = sum(len(line) + 1 for line in lines)
    truncated = False
    while frontier and not truncated:
        node_id, depth = frontier.popleft()
        if depth >= 2:
            continue
        # Visit strongest-connected neighbours first so the budget buys signal.
        neighbours = sorted(
            graph.adjacency.get(node_id, ()), key=lambda item: -graph.degree(item[0])
        )
        for other, relation, direction, attrs in neighbours:
            src, dst = (node_id, other) if direction == "out" else (other, node_id)
            key = (src, dst, relation)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            src_label = graph.nodes[src].get("label") or src
            dst_label = graph.nodes[dst].get("label") or dst
            loc = _location(attrs) or _location(graph.nodes[other])
            line = f"- {src_label} --{relation}--> {dst_label}" + (f" [{loc}]" if loc else "")
            if used + len(line) + 1 > budget_chars:
                truncated = True
                break
            lines.append(line)
            used += len(line) + 1
            if other not in visited:
                visited.add(other)
                frontier.append((other, depth + 1))
    if truncated:
        lines.append("[... truncated at budget; use graph_neighbors on a node to go deeper]")
    return {
        "text": "\n".join(lines),
        "seeds": seeds,
        "node_ids": sorted(visited),
        "truncated": truncated,
    }


def find_node(graph: CodeGraph, node: str) -> tuple[str | None, list[str]]:
    """Resolve a user/agent node reference to an id.

    Tries exact id, then case-insensitive exact label, then label substring.

    :param graph: The graph.
    :param node: Id or label text.
    :returns: ``(node_id, candidates)`` — ``node_id`` is ``None`` when the
        reference is unknown or ambiguous; ``candidates`` lists up to 10 ids.
    """
    if node in graph.nodes:
        return node, []
    lowered = node.lower()
    exact = [
        nid for nid, raw in graph.nodes.items() if str(raw.get("label", "")).lower() == lowered
    ]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, sorted(exact, key=lambda nid: -graph.degree(nid))[:10]
    partial = [
        nid
        for nid, raw in graph.nodes.items()
        if lowered and lowered in str(raw.get("label", nid)).lower()
    ]
    if len(partial) == 1:
        return partial[0], []
    return None, sorted(partial, key=lambda nid: -graph.degree(nid))[:10]


def neighbors(graph: CodeGraph, node: str, depth: int) -> dict[str, Any]:
    """List the neighbourhood of a node up to ``depth`` hops (max 2).

    :param graph: The graph.
    :param node: Node id or label.
    :param depth: Hop count, clamped to ``1..2``.
    :returns: ``{"node", "neighbors", "truncated", "candidates"}``; ``node`` is
        ``None`` (with ``candidates``) when the reference did not resolve.
    """
    depth = max(1, min(depth, 2))
    node_id, candidates = find_node(graph, node)
    if node_id is None:
        return {
            "node": None,
            "neighbors": [],
            "truncated": False,
            "candidates": [_node_summary(graph, nid) for nid in candidates],
        }
    results: list[dict[str, Any]] = []
    visited = {node_id}
    frontier: deque[tuple[str, int]] = deque([(node_id, 0)])
    truncated = False
    while frontier and not truncated:
        current, level = frontier.popleft()
        if level >= depth:
            continue
        for other, relation, direction, attrs in graph.adjacency.get(current, ()):
            if other in visited:
                continue
            if len(results) >= MAX_NEIGHBORS:
                truncated = True
                break
            visited.add(other)
            entry = _node_summary(graph, other)
            entry.update(
                {
                    "via": current,
                    "relation": relation,
                    "direction": direction,
                    "depth": level + 1,
                    "edge_location": _location(attrs) or None,
                }
            )
            results.append(entry)
            frontier.append((other, level + 1))
    return {
        "node": _node_summary(graph, node_id),
        "neighbors": results,
        "truncated": truncated,
        "candidates": [],
    }
