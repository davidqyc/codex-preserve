"""Executable G4a contract against hand-authored synthetic Claude sources."""

import contextlib
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from codex_preserve._claude_source import (
    KNOWN_IGNORED_OR_SUMMARIZED, RENDER, UNKNOWN, bounded_safe_view,
    parse_claude_session,
)


ROOT = Path(__file__).parent / "fixtures" / "claude"


def parsed(name):
    return parse_claude_session(ROOT / (name + ".jsonl"))


def codes(result):
    return {item.code for item in result.diagnostics}


def record(result, kind, ordinal=0):
    return [item for item in result.records if item.kind == kind][ordinal]


class ClaudeSourceParserContract(unittest.TestCase):
    def test_linear_cli_is_eligible_without_session_end_inference(self):
        result = parsed("linear_cli")
        self.assertEqual((result.completeness, result.entrypoint), ("COMPLETE", "cli"))
        self.assertEqual(result.active_head_id, None)
        self.assertEqual(result.node_count, 2)
        self.assertEqual(record(result, "user").blocks[0].text, "synthetic user text")
        assistant = record(result, "assistant")
        self.assertEqual(assistant.parent_id, record(result, "user").node_id)
        self.assertEqual(assistant.stop_reason, "end_turn")
        self.assertEqual(record(result, "last-prompt").leaf_hint_id, assistant.node_id)

    def test_branch_is_graph_fact_without_active_head_guess(self):
        result = parsed("branching_cli")
        self.assertEqual(result.branch_count, 1)
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("GRAPH_BRANCH_PRESENT", codes(result))
        self.assertIsNone(result.active_head_id)
        users = [r for r in result.records if r.kind == "user"]
        self.assertEqual(users[1].parent_id, users[2].parent_id)
        self.assertNotEqual(users[1].node_id, users[2].node_id)

    def test_last_prompt_with_descendants_is_only_a_hint(self):
        result = parsed("stale_last_prompt_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertIn("LEAF_HINT_HAS_DESCENDANTS", codes(result))
        hint = record(result, "last-prompt").leaf_hint_id
        later = [r for r in result.records if r.kind == "user"][1]
        self.assertEqual(later.parent_id, hint)
        self.assertIsNone(result.active_head_id)

    def test_rewind_pointer_flags_are_preserved_without_selecting_head(self):
        result = parsed("rewound_last_prompt_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertIn("LEAF_HINT_HAS_DESCENDANTS", codes(result))
        hint = record(result, "last-prompt")
        self.assertTrue(hint.leaf_hint_explicit)
        self.assertTrue(hint.leaf_hint_rewound)
        self.assertIsNotNone(hint.leaf_hint_id)
        self.assertIsNone(result.active_head_id)

    def test_compaction_summary_is_labelled(self):
        result = parsed("compaction_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertEqual(record(result, "system").semantic_kind, "compact_boundary")
        summary = [r for r in result.records if r.semantic_kind == "compact_summary"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].blocks[0].semantic_kind, "compact_summary")
        self.assertEqual(summary[0].blocks[0].policy, RENDER)

    def test_known_ignored_and_fallback_never_render_bodies(self):
        result = parsed("known_ignored_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        for kind in ("queue-operation", "file-history-snapshot", "attachment", "last-prompt"):
            self.assertEqual(record(result, kind).policy, KNOWN_IGNORED_OR_SUMMARIZED)
        self.assertEqual(record(result, "attachment").attachment_kind,
                         "total_tokens_reminder")
        fallback = parsed("fallback_cli")
        self.assertEqual(fallback.completeness, "COMPLETE")
        self.assertEqual(record(fallback, "assistant").blocks[0].kind, "fallback")
        self.assertEqual(record(fallback, "assistant").blocks[0].policy,
                         KNOWN_IGNORED_OR_SUMMARIZED)

    def test_unknown_types_are_explicit_and_non_complete(self):
        expected = {
            "unknown_record_cli": ("UNKNOWN_RECORD", 2),
            "unknown_block_cli": ("UNKNOWN_BLOCK", 2),
            "unknown_attachment_cli": ("UNKNOWN_ATTACHMENT", 2),
        }
        for name, (code, line) in expected.items():
            with self.subTest(name=name):
                result = parsed(name)
                self.assertEqual(result.completeness, "NON_COMPLETE")
                self.assertIn((code, line), [(d.code, d.line) for d in result.diagnostics])
                self.assertIn(UNKNOWN, [r.policy for r in result.records])

    def test_unsupported_and_mixed_entrypoints_are_not_eligible(self):
        for name, state in (("mixed_entrypoint", "mixed"),
                            ("unsupported_sdk_cli", "unsupported")):
            with self.subTest(name=name):
                result = parsed(name)
                self.assertEqual(result.entrypoint, state)
                self.assertEqual(result.completeness, "NON_COMPLETE")
                self.assertIn("UNSUPPORTED_ENTRYPOINT", codes(result))

    def test_attachment_entrypoint_also_participates_in_claim(self):
        rows = (ROOT / "known_ignored_cli.jsonl").read_text(encoding="utf-8").splitlines()
        attachment = json.loads(rows[3])
        attachment["entrypoint"] = "sdk-cli"
        rows[3] = json.dumps(attachment)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = parse_claude_session(path)
        self.assertEqual(result.entrypoint, "mixed")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn(("UNSUPPORTED_ENTRYPOINT", 4),
                      [(d.code, d.line) for d in result.diagnostics])

    def test_truncated_tail_keeps_preceding_safe_records(self):
        result = parsed("truncated_tail_cli")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn(("MALFORMED_JSONL", 3),
                      [(d.code, d.line) for d in result.diagnostics])
        self.assertEqual(len(result.records), 2)
        self.assertEqual(record(result, "assistant").blocks[0].text,
                         "synthetic complete assistant line")

    def test_malformed_middle_line_is_visible_and_later_lines_survive(self):
        source = (ROOT / "linear_cli.jsonl").read_text(encoding="utf-8").splitlines()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text(source[0] + "\n{SYNTHETIC_MALFORMED_CANARY\n" +
                            "\n".join(source[1:]) + "\n", encoding="utf-8")
            result = parse_claude_session(path)
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn(("MALFORMED_JSONL", 2),
                      [(d.code, d.line) for d in result.diagnostics])
        self.assertEqual(result.node_count, 2)
        self.assertNotIn("SYNTHETIC_MALFORMED_CANARY", repr(result))

    def test_same_size_timestamp_restored_change_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_bytes((ROOT / "linear_cli.jsonl").read_bytes())
            before = path.stat()
            original_open = Path.open
            openings = [0]

            def mutate_before_second_open(candidate, *args, **kwargs):
                if candidate == path:
                    openings[0] += 1
                    if openings[0] == 2:
                        with original_open(candidate, "r+b") as handle:
                            payload = handle.read()
                            changed = payload.replace(b"synthetic user text",
                                                      b"synthetic uxer text")
                            self.assertEqual(len(payload), len(changed))
                            handle.seek(0)
                            handle.write(changed)
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return original_open(candidate, *args, **kwargs)

            with mock.patch.object(Path, "open", mutate_before_second_open):
                result = parse_claude_session(path)
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("SOURCE_CHANGED", codes(result))
        self.assertEqual(record(result, "user").blocks[0].text,
                         "synthetic user text")

    def test_phantom_parent_preserves_descendants_without_guessing(self):
        result = parsed("phantom_parent_after_resume_cli")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn(("GRAPH_LINK_GAP", 3),
                      [(d.code, d.line) for d in result.diagnostics])
        self.assertEqual(result.node_count, 5)
        resumed = [r for r in result.records if r.line == 3][0]
        attachment = [r for r in result.records if r.line == 4][0]
        assistant = [r for r in result.records if r.line == 5][0]
        self.assertEqual((resumed.parent_link, resumed.parent_id), ("missing", None))
        self.assertEqual(attachment.parent_id, resumed.node_id)
        self.assertEqual(assistant.parent_id, attachment.node_id)
        self.assertEqual(assistant.blocks[0].text, "synthetic answer after resume")
        self.assertNotEqual(resumed.parent_id, result.records[1].node_id)

    def test_privacy_canaries_absent_from_every_safe_view(self):
        names = ("sensitive_canaries_cli", "known_ignored_cli",
                 "unknown_record_cli", "unknown_block_cli",
                 "unknown_attachment_cli", "phantom_parent_after_resume_cli")
        for name in names:
            with self.subTest(name=name):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = parsed(name)
                raw = (ROOT / (name + ".jsonl")).read_text(encoding="utf-8")
                canaries = set(re.findall(r"SYNTHETIC_[A-Z_]+_CANARY", raw))
                self.assertTrue(canaries)
                safe_views = (repr(result), json.dumps(asdict(result), sort_keys=True),
                              json.dumps(bounded_safe_view(result), sort_keys=True),
                              repr(result.diagnostics), stdout.getvalue(), stderr.getvalue())
                for canary in canaries:
                    for view in safe_views:
                        self.assertNotIn(canary, view)
        sensitive = parsed("sensitive_canaries_cli")
        assistant = record(sensitive, "assistant")
        self.assertEqual(assistant.blocks[0].kind, "thinking")
        self.assertEqual(assistant.blocks[1].tool_name, "SyntheticTool")
        self.assertEqual(record(sensitive, "user", 1).blocks[0].tool_ref,
                         assistant.blocks[1].tool_ref)
        self.assertTrue(record(sensitive, "user", 1).blocks[0].result_present)

    def test_result_is_deterministic(self):
        for path in sorted(ROOT.glob("*.jsonl")):
            with self.subTest(path=path.name):
                first = parse_claude_session(path)
                second = parse_claude_session(path)
                self.assertEqual(asdict(first), asdict(second))

    def test_malformed_shape_and_unknown_values_do_not_escape(self):
        lines = [
            {"type": ["SYNTHETIC_BAD_TYPE_CANARY"], "uuid": "node-a",
             "parentUuid": None, "futureValue": "SYNTHETIC_UNKNOWN_VALUE_CANARY"},
            {"type": "user", "uuid": "node-b", "parentUuid": "node-a",
             "sessionId": "session-synthetic-shapes", "entrypoint": "cli",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "synthetic safe text",
                  "futureValue": "SYNTHETIC_EXTRA_TEXT_CANARY"}]}},
            {"type": "attachment", "uuid": "node-c", "parentUuid": "node-b",
             "attachment": {"type": {"secret": "SYNTHETIC_BAD_ATTACHMENT_CANARY"}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in lines),
                            encoding="utf-8")
            result = parse_claude_session(path)
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("UNKNOWN_RECORD", codes(result))
        self.assertIn("UNKNOWN_ATTACHMENT", codes(result))
        self.assertIn("EXTRA_TEXT_FIELD", codes(result))
        for canary in ("SYNTHETIC_BAD_TYPE_CANARY", "SYNTHETIC_UNKNOWN_VALUE_CANARY",
                       "SYNTHETIC_EXTRA_TEXT_CANARY", "SYNTHETIC_BAD_ATTACHMENT_CANARY"):
            self.assertNotIn(canary, repr(result))

    def test_selected_source_cannot_be_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "subagents"
            sidecar.mkdir()
            path = sidecar / "synthetic.jsonl"
            path.write_text("SYNTHETIC_SIDECAR_BODY_CANARY", encoding="utf-8")
            result = parse_claude_session(path)
        self.assertEqual(result.records, ())
        self.assertIn("INVALID_SOURCE_SELECTION", codes(result))
        self.assertNotIn("SYNTHETIC_SIDECAR_BODY_CANARY", repr(result))

    def test_sidecar_reference_is_metadata_only_and_missing_is_nonblocking(self):
        row = json.loads((ROOT / "linear_cli.jsonl").read_text(encoding="utf-8").splitlines()[0])
        row["toolUseResult"] = {"filePath": "tool-results/synthetic.txt"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "tool-results"
            folder.mkdir()
            path = root / "synthetic.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            missing = parse_claude_session(path)
            sidecar = folder / "synthetic.txt"
            sidecar.write_text("SYNTHETIC_SIDECAR_BODY_CANARY", encoding="utf-8")
            original_open = Path.open
            def guarded_open(candidate, *args, **kwargs):
                if candidate == sidecar:
                    raise AssertionError("sidecar body must not be opened")
                return original_open(candidate, *args, **kwargs)
            with mock.patch.object(Path, "open", guarded_open):
                present = parse_claude_session(path)
        self.assertEqual(missing.completeness, "COMPLETE")
        self.assertIn("SIDECAR_MISSING", codes(missing))
        self.assertEqual((missing.records[0].sidecar_relation,
                          missing.records[0].referenced_sidecar_present),
                         ("tool_result", False))
        self.assertEqual((present.records[0].sidecar_relation,
                          present.records[0].referenced_sidecar_present),
                         ("tool_result", True))
        self.assertNotIn("SYNTHETIC_SIDECAR_BODY_CANARY", repr(present))


if __name__ == "__main__":
    unittest.main()
