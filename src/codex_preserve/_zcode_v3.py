"""ZCode adapter projection into the internal schema-3 package spec."""

from __future__ import annotations

from dataclasses import asdict
from typing import List

from ._v3_package import V3PackageSpec
from ._zcode_source import ZCodeParseResult


ADAPTER_NAME = "zcode-sqlite-local"
ADAPTER_VERSION = "1"


def _render_markdown(source: ZCodeParseResult) -> str:
    lines: List[str] = [
        "# Conversation",
        "",
        "Provider: ZCode",
        "",
        "This file preserves safe readable text from the selected local ZCode "
        "SQLite session snapshot at export time.",
        "",
    ]
    for item in source.text_items:
        heading = "User" if item.role == "user" else "Assistant"
        lines.extend(["## %s" % heading, "", item.text, ""])
    return "\n".join(lines).rstrip() + "\n"


def zcode_v3_spec(source: ZCodeParseResult) -> V3PackageSpec:
    receipt = {
        "diagnostics": [asdict(item) for item in source.diagnostics],
        "text_item_count": len(source.text_items),
        "tool_events": [asdict(item) for item in source.tool_events],
        "message_count": source.message_count,
        "part_count": source.part_count,
        "hidden_message_count": source.hidden_message_count,
        "reasoning_part_count": source.reasoning_part_count,
        "sqlite_snapshot_scope": "selected_session_rows",
        "ui_completeness_attested": False,
        "session_terminal_attested": False,
    }
    return V3PackageSpec(
        provider="zcode",
        adapter_name=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        coverage_status=source.completeness,
        source_stable=source.source_stable,
        source_identity_scope="zcode:selected_session_rows",
        source_identity_sha256=source.selected_session_sha256,
        conversation_markdown=_render_markdown(source),
        provider_receipt=receipt,
    )
