"""Internal Claude payload candidate over persisted, G4a/G4b-safe facts.

This is not a package schema or a claim about the current Claude UI. G4a's
``COMPLETE`` classifies the selected persisted source under its parser contract;
it does not attest that all visible replies were flushed or that a session ended.
No source path or raw JSONL is accepted or read here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional, Tuple

from ._claude_readable import (
    ReadableBranchPoint, ReadableDiagnosticSummary, ReadableEvent,
    ReadableGraph, ReadableLeafHint, ReadableNode,
)
from ._claude_source import ParseResult


@dataclass(frozen=True)
class ClaudeProvenance:
    """Content identity of a stable selected top-level JSONL snapshot, not authenticity."""

    source_sha256: Optional[str]
    source_size_bytes: Optional[int]


@dataclass(frozen=True)
class ClaudeCoverage:
    """Separate source stability, parser classification, persisted text, and UI claims."""

    source_stable: bool
    parser_source_classification: str
    source_entrypoint: str
    readable_node_count: int
    rendered_text_block_count: int
    branch_count: int
    graph_gap_count: int
    unknown_count: int
    duplicate_uuid_count: int
    diagnostic_counts_by_code: Tuple[Tuple[str, int], ...]
    source_scope: str = field(init=False, default="selected_top_level_jsonl")
    sidecar_bodies_included: bool = field(init=False, default=False)
    ui_completeness_attested: bool = field(init=False, default=False)
    session_terminal_attested: bool = field(init=False, default=False)


@dataclass(frozen=True)
class ClaudeGraphGap:
    record_id: str
    source_line: int
    node_id: Optional[str]


@dataclass(frozen=True)
class ClaudeDuplicateOccurrence:
    node_id: str
    record_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ClaudeProviderPayload:
    """Loss-averse internal candidate; occurrence order is persisted source order."""

    readable_nodes: Tuple[ReadableNode, ...]
    events: Tuple[ReadableEvent, ...]
    branch_points: Tuple[ReadableBranchPoint, ...]
    leaf_hints: Tuple[ReadableLeafHint, ...]
    graph_gaps: Tuple[ClaudeGraphGap, ...]
    duplicate_occurrences: Tuple[ClaudeDuplicateOccurrence, ...]
    diagnostics: ReadableDiagnosticSummary
    coverage: ClaudeCoverage
    provenance: ClaudeProvenance
    presentation_order: str
    format_status: str = field(init=False, default="INTERNAL_CANDIDATE")

    def as_dict(self) -> dict:
        """Deterministic safe candidate shape; G5 may replace it without migration."""
        def json_shape(value):
            if isinstance(value, dict):
                return {key: json_shape(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [json_shape(item) for item in value]
            return value

        return json_shape(asdict(self))

    def candidate_json_bytes(self) -> bytes:
        """Deterministic internal bytes, with no promised package member name."""
        return (json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")


def build_claude_payload(graph: ReadableGraph,
                         source: ParseResult) -> ClaudeProviderPayload:
    """Project G4b graph plus only G4a's attested snapshot facts.

    The caller supplies the G4a result used to build ``graph``. This function
    reads only its classification and snapshot fields; it never reads records,
    diagnostics, a source path, or any local file.
    """
    if not isinstance(graph, ReadableGraph) or not isinstance(source, ParseResult):
        raise TypeError("expected Claude ReadableGraph and ParseResult")
    if (graph.source_completeness != source.completeness or
            graph.source_entrypoint != source.entrypoint):
        raise ValueError("graph and source classification disagree")
    if source.source_stable:
        if source.source_sha256 is None or source.source_size_bytes is None:
            raise ValueError("stable source lacks snapshot identity")
    elif source.source_sha256 is not None or source.source_size_bytes is not None:
        raise ValueError("unstable source cannot attest snapshot identity")

    occurrences = {}
    for node in graph.nodes:
        if node.node_id is not None:
            occurrences.setdefault(node.node_id, []).append(node.record_id)
    duplicate_occurrences = tuple(
        ClaudeDuplicateOccurrence(node_id, tuple(record_ids))
        for node_id, record_ids in occurrences.items() if len(record_ids) > 1
    )
    graph_gaps = tuple(
        ClaudeGraphGap(node.record_id, node.source_line, node.node_id)
        for node in graph.nodes if node.parent_link == "missing"
    )
    diagnostic_counts = dict(graph.diagnostics.counts_by_code)
    coverage = ClaudeCoverage(
        source_stable=source.source_stable,
        parser_source_classification=graph.source_completeness,
        source_entrypoint=graph.source_entrypoint,
        readable_node_count=graph.readable_node_count,
        rendered_text_block_count=graph.rendered_text_block_count,
        branch_count=graph.branch_count,
        graph_gap_count=graph.graph_gap_count,
        unknown_count=graph.unknown_count,
        duplicate_uuid_count=diagnostic_counts.get("DUPLICATE_UUID", 0),
        diagnostic_counts_by_code=graph.diagnostics.counts_by_code,
    )
    return ClaudeProviderPayload(
        graph.nodes, graph.events, graph.branch_points, graph.leaf_hints,
        graph_gaps, duplicate_occurrences, graph.diagnostics, coverage,
        ClaudeProvenance(source.source_sha256, source.source_size_bytes),
        graph.presentation_order,
    )
