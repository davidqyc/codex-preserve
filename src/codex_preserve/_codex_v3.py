"""Codex adapter projection into the internal schema-3 package spec."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple

from . import exporter
from ._v3_package import (
    ARTIFACT_INDEX_FILENAME,
    V3Member,
    V3PackageSpec,
)


ADAPTER_NAME = "openai-codex-local"
ADAPTER_VERSION = "1"


def _new_member_path(kind: str, old_member: str) -> str:
    parts = PurePosixPath(old_member).parts
    relative = "/".join(parts[1:]) if len(parts) > 1 else parts[-1]
    root = "attachments" if kind == "attachment" else "artifacts"
    return "%s/%s" % (root, relative)


def _payload_members(context: Dict[str, Any]) -> Tuple[V3Member, ...]:
    members: List[V3Member] = []
    attachment_receipt = []
    artifact_receipt = []
    for kind, rows in (
        ("attachment", context["attachments"]),
        ("artifact", context["artifacts"]),
    ):
        for row in rows:
            data = row.get("_materialized_bytes")
            old_member = row.get("materialized_member")
            if not isinstance(data, bytes) or not isinstance(old_member, str):
                continue
            new_path = _new_member_path(kind, old_member)
            members.append(V3Member(new_path, kind, data))
            public = {
                "member": new_path,
                "bytes": len(data),
                "sha256": row.get("materialized_sha256"),
                "payload_complete": row.get("payload_complete"),
                "materialization_status": row.get("materialization_status"),
            }
            (attachment_receipt if kind == "attachment"
             else artifact_receipt).append(public)
    if artifact_receipt:
        members.append(V3Member(
            ARTIFACT_INDEX_FILENAME,
            "artifact_index",
            context["output_index"].encode("utf-8"),
        ))
    context["_v3_attachment_receipt"] = attachment_receipt
    context["_v3_artifact_receipt"] = artifact_receipt
    return tuple(members)


def _provider_receipt(context: Dict[str, Any]) -> dict:
    receipt = exporter.build_receipt(context)
    session = receipt["session"]
    safe_session = {
        key: session.get(key)
        for key in (
            "cli_version", "originator", "source", "thread_source",
            "model_provider", "history_mode", "model", "reasoning_effort",
            "approval_policy", "sandbox_policy", "workspace",
            "workspace_roots", "repo", "branch", "head", "started_at",
            "last_activity_at", "turn_count", "completed_turn_count",
            "models", "efforts", "aggregate_token_usage",
        )
    }
    safe_session["partial_turn_count"] = len(session.get("partial_turn_ids") or [])
    safe_session["aborted_turn_count"] = len(session.get("aborted_turns") or [])
    safe_session["turn_outcome_conflict_count"] = len(
        session.get("turn_outcome_conflicts") or [])

    return {
        "export_core_version": exporter.EXPORTER_VERSION,
        "provider_export_status": context["export_status"],
        "session": safe_session,
        "schema": receipt["schema"],
        "counts": receipt["counts"],
        "duplicate_breakdown": receipt["duplicate_breakdown"],
        "reasoning_selection": receipt["reasoning_selection"],
        "tool_timeline": receipt["tool_timeline"],
        "privacy": receipt["privacy"],
        "reasoning_boundary": receipt["reasoning_boundary"],
        "attachments": context.get("_v3_attachment_receipt", []),
        "artifacts": context.get("_v3_artifact_receipt", []),
        "git_provenance": receipt["git_provenance"],
        "warnings": receipt["warnings"],
        "ui_completeness_attested": False,
        "session_terminal_attested": False,
    }


def _conversation_markdown(context: Dict[str, Any]) -> str:
    markdown = context["markdown"]
    markdown = markdown.replace(
        "# Codex Conversation Export", "# Conversation", 1)
    markdown = markdown.replace(
        "<!-- codex-conversation-export exporter_version=",
        "<!-- session-preserve provider=codex adapter_exporter_version=",
        1,
    )
    return markdown


def codex_v3_spec(context: Dict[str, Any]) -> V3PackageSpec:
    source = context["source"]
    if source.get("changed"):
        raise ValueError("Codex source changed; cannot build schema-3 package")
    pre = source.get("pre") or {}
    post = source.get("post") or {}
    if pre.get("sha256") != post.get("sha256") or \
            pre.get("bytes") != post.get("bytes"):
        raise ValueError("Codex source snapshot is not stable")
    digest = pre.get("sha256")
    if not isinstance(digest, str):
        raise ValueError("Codex source snapshot has no sha256")

    extras = _payload_members(context)
    return V3PackageSpec(
        provider="codex",
        adapter_name=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        coverage_status=(
            "COMPLETE"
            if context["export_status"] == exporter.STATUS_COMPLETE
            else "NON_COMPLETE"
        ),
        source_stable=True,
        source_identity_scope="codex:selected_rollout_jsonl",
        source_identity_sha256=digest,
        conversation_markdown=_conversation_markdown(context),
        provider_receipt=_provider_receipt(context),
        extra_members=extras,
    )
