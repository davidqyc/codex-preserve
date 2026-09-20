"""Synthetic compatibility tests for current Codex lifecycle records."""

import json
import zipfile

from codex_preserve import exporter
from tests.test_codex_conversation_export import (
    ExporterTestCase,
    RolloutBuilder,
    TURN_1,
    TURN_2,
)


def add_mcp(builder, status="completed", **extra):
    item = {
        "type": "McpToolCall",
        "id": "mcp-item-1",
        "server": "example-server",
        "tool": "lookup",
        "status": status,
    }
    item.update(extra)
    builder.add("event_msg", {
        "type": "item_completed",
        "thread_id": builder.session_id,
        "turn_id": TURN_1,
        "started_at_ms": 1,
        "completed_at_ms": 2,
        "item": item,
    })


def add_abort(builder, turn_id=TURN_1, reason="interrupted", **extra):
    payload = {
        "type": "turn_aborted",
        "turn_id": turn_id,
        "reason": reason,
        "started_at": 1,
        "completed_at": 2,
        "duration_ms": 1,
    }
    payload.update(extra)
    builder.add("event_msg", payload)


def basic_builder():
    builder = RolloutBuilder()
    builder.turn_context()
    builder.task_started()
    builder.owner_message("inspect the synthetic session")
    builder.assistant("synthetic final", phase="final_answer")
    return builder


class SchemaLifecycleCompatibility(ExporterTestCase):
    def test_registry_uses_exact_persisted_wire_values(self):
        self.assertEqual(
            exporter.MCP_TOOL_CALL_TERMINAL_STATUSES,
            frozenset(("completed", "failed")),
        )
        self.assertEqual(
            exporter.MCP_TOOL_CALL_NON_TERMINAL_STATUSES,
            frozenset(("inProgress",)),
        )
        self.assertEqual(
            exporter.TURN_ABORT_REASONS,
            frozenset(("interrupted", "replaced", "review_ended", "budget_limited")),
        )
        self.assertFalse(hasattr(exporter, "enum_token"))
        self.assertTrue(exporter.exact_wire("completed",
                                           exporter.MCP_TOOL_CALL_TERMINAL_STATUSES))
        self.assertFalse(exporter.exact_wire("Completed",
                                            exporter.MCP_TOOL_CALL_TERMINAL_STATUSES))
        self.assertFalse(exporter.exact_wire(" completed ",
                                            exporter.MCP_TOOL_CALL_TERMINAL_STATUSES))

    def test_valid_mcp_call_is_recognized_without_publishing_private_payload(self):
        sentinel = "PRIVATE_MCP_SENTINEL_ALPHA"
        builder = basic_builder()
        add_mcp(
            builder,
            arguments={"query": sentinel},
            result={"content": [{"type": "text", "text": sentinel}]},
            error={"message": sentinel},
            pluginId=sentinel,
            connectorId=sentinel,
            readOnlyHint=True,
        )
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        self.assertEqual(receipt["export_status"], exporter.STATUS_COMPLETE)
        self.assertEqual(receipt["counts"]["mcp_tool_call_records_recognized"], 1)
        self.assertNotIn("McpToolCall", receipt["schema"]["unknown_item_type_counts"])
        self.assertNotIn(
            "event_msg/item_completed/McpToolCall",
            receipt["schema"]["unknown_payload_type_counts"],
        )
        self.assertNotIn(sentinel, markdown)
        self.assertGreaterEqual(
            receipt["privacy"]["drops"].get("mcp_tool_call_arguments", 0), 1
        )
        self.assertGreaterEqual(
            receipt["privacy"]["drops"].get("mcp_tool_call_result", 0), 1
        )
        self.assertGreaterEqual(
            receipt["privacy"]["drops"].get("mcp_tool_call_error", 0), 1
        )

        package = self.package_dirs()[-1]
        for file_path in package.rglob("*"):
            if not file_path.is_file():
                continue
            data = file_path.read_bytes()
            self.assertNotIn(sentinel.encode("utf-8"), data)
            if file_path.suffix == ".zip":
                with zipfile.ZipFile(file_path) as archive:
                    for member in archive.infolist():
                        self.assertNotIn(sentinel, member.filename)
                        self.assertNotIn(
                            sentinel.encode("utf-8"), archive.read(member)
                        )

    def test_mcp_exact_nonterminal_and_aliases_stay_visibly_degraded(self):
        cases = [
            ("inProgress", "non_terminal_status"),
            ("in_progress", "unsupported_status"),
            ("InProgress", "unsupported_status"),
            ("Completed", "unsupported_status"),
            (" completed ", "unsupported_status"),
        ]
        for index, (status, suffix) in enumerate(cases):
            with self.subTest(status=status):
                # Isolate each case's output. Package selection is path-sorted,
                # while Python string hashes are intentionally randomized across
                # processes; a hash-derived filename would make this regression
                # read a different case's receipt nondeterministically.
                self.output = self.root / ("out-status-%02d" % index)
                builder = basic_builder()
                add_mcp(builder, status=status)
                builder.task_complete()
                _, receipt = self.export(builder.write(
                    self.sessions, filename="rollout-status-%02d.jsonl" % index
                ))
                self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
                key = "event_msg/item_completed/McpToolCall#%s" % suffix
                self.assertEqual(
                    receipt["schema"]["unknown_payload_type_counts"].get(key), 1
                )

    def test_wrong_type_mcp_status_degrades_without_crashing(self):
        builder = basic_builder()
        add_mcp(builder, status={"unexpected": "shape"})
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
        self.assertEqual(
            receipt["schema"]["unknown_payload_type_counts"].get(
                "event_msg/item_completed/McpToolCall#unsupported_status"
            ),
            1,
        )

    def test_complete_then_abort_resolves_to_aborted(self):
        builder = basic_builder()
        builder.task_complete()
        add_abort(builder)
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["export_status"], exporter.STATUS_PARTIAL)
        self.assertEqual(receipt["session"]["completed_turn_count"], 0)
        self.assertEqual(receipt["session"]["partial_turn_ids"], [TURN_1])
        self.assertEqual(
            receipt["session"]["aborted_turns"],
            [{"turn_id": TURN_1, "reason": "interrupted"}],
        )
        self.assertEqual(receipt["counts"]["turn_outcome_conflicts"], 1)
        self.assertFalse(receipt["partial_state"]["all_turns_completed"])
        self.assertIn("completed=false, aborted=interrupted", markdown)
        self.assertIn("synthetic final", markdown)

    def test_abort_then_complete_stays_aborted(self):
        builder = basic_builder()
        add_abort(builder)
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["export_status"], exporter.STATUS_PARTIAL)
        self.assertEqual(receipt["session"]["completed_turn_count"], 0)
        self.assertEqual(receipt["counts"]["turn_outcome_conflicts"], 1)
        self.assertEqual(receipt["partial_state"]["aborted_turn_count"], 1)

    def test_noncanonical_abort_cannot_steal_a_valid_completion(self):
        builder = basic_builder()
        builder.task_complete()
        add_abort(builder, reason="Interrupted")
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
        self.assertEqual(receipt["session"]["completed_turn_count"], 1)
        self.assertEqual(receipt["session"]["aborted_turns"], [])
        self.assertEqual(receipt["counts"]["turn_outcome_conflicts"], 0)
        self.assertEqual(
            receipt["schema"]["unknown_payload_type_counts"].get(
                "event_msg/turn_aborted#unsupported_reason"
            ),
            1,
        )

    def test_missing_abort_turn_id_is_never_guessed(self):
        builder = basic_builder()
        builder.task_complete()
        builder.add("event_msg", {
            "type": "turn_aborted",
            "reason": "interrupted",
            "started_at": 1,
            "completed_at": 2,
            "duration_ms": 1,
        })
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["session"]["completed_turn_count"], 1)
        self.assertEqual(receipt["session"]["aborted_turns"], [])
        self.assertEqual(receipt["counts"]["turn_aborts_unbound"], 1)

    def test_two_turn_completion_and_abort_are_isolated(self):
        builder = basic_builder()
        builder.task_complete(TURN_1)
        builder.turn_context(turn_id=TURN_2)
        builder.task_started(turn_id=TURN_2)
        builder.owner_message("second synthetic request", turn_id=TURN_2)
        builder.assistant("second synthetic final", phase="final_answer",
                          turn_id=TURN_2)
        add_abort(builder, turn_id=TURN_2, reason="replaced")
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["session"]["completed_turn_count"], 1)
        self.assertEqual(receipt["session"]["partial_turn_ids"], [TURN_2])
        self.assertEqual(
            receipt["session"]["aborted_turns"],
            [{"turn_id": TURN_2, "reason": "replaced"}],
        )

    def test_recognized_lifecycle_does_not_hide_future_schema_drift(self):
        builder = basic_builder()
        add_mcp(builder)
        add_abort(builder)
        builder.add("event_msg", {"type": "future_event_shape"})
        builder.add("event_msg", {
            "type": "item_completed",
            "thread_id": builder.session_id,
            "turn_id": TURN_1,
            "item": {"type": "FutureItemShape"},
            "started_at_ms": 1,
            "completed_at_ms": 2,
        })
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
        self.assertEqual(receipt["counts"]["mcp_tool_call_records_recognized"], 1)
        self.assertEqual(receipt["partial_state"]["aborted_turn_count"], 1)
        self.assertEqual(
            receipt["schema"]["unknown_payload_type_counts"].get(
                "event_msg/future_event_shape"
            ),
            1,
        )
        self.assertEqual(
            receipt["schema"]["unknown_item_type_counts"].get("FutureItemShape"),
            1,
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
