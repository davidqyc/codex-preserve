"""G4c internal Claude payload and coverage over synthetic persisted sources."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import unittest
from unittest import mock

from codex_preserve._claude_payload import build_claude_payload
from codex_preserve._claude_readable import build_readable_graph
from codex_preserve._claude_source import parse_claude_session


ROOT = Path(__file__).parent / "fixtures" / "claude"


def payload(name):
    source = parse_claude_session(ROOT / (name + ".jsonl"))
    return build_claude_payload(build_readable_graph(source), source)


def texts(result):
    return [block.text for node in result.readable_nodes
            for block in node.text_blocks]


class ClaudePayloadContract(unittest.TestCase):
    def test_ui_lag_has_eligible_stable_source_but_no_ui_or_terminal_claim(self):
        result = payload("persisted_ui_lag_possible_cli")
        coverage = result.coverage
        self.assertTrue(coverage.source_stable)
        self.assertEqual(coverage.source_scope, "selected_top_level_jsonl")
        self.assertEqual(coverage.parser_source_classification, "COMPLETE")
        self.assertEqual(coverage.source_entrypoint, "cli")
        self.assertEqual((coverage.readable_node_count,
                          coverage.rendered_text_block_count), (1, 1))
        self.assertFalse(coverage.sidecar_bodies_included)
        self.assertFalse(coverage.ui_completeness_attested)
        self.assertFalse(coverage.session_terminal_attested)
        self.assertEqual(texts(result), ["synthetic persisted user request"])
        self.assertEqual([node.role for node in result.readable_nodes],
                         ["user", None])
        self.assertEqual(len(result.leaf_hints), 1)
        self.assertEqual(result.format_status, "INTERNAL_CANDIDATE")

    def test_snapshot_identity_is_from_parser_stable_read(self):
        path = ROOT / "persisted_ui_lag_possible_cli.jsonl"
        result = payload("persisted_ui_lag_possible_cli")
        data = path.read_bytes()
        self.assertEqual(result.provenance.source_sha256,
                         hashlib.sha256(data).hexdigest())
        self.assertEqual(result.provenance.source_size_bytes, len(data))
        self.assertNotIn(str(path), result.candidate_json_bytes().decode())

    def test_parser_non_complete_can_still_have_stable_snapshot(self):
        result = payload("unknown_block_cli")
        self.assertTrue(result.coverage.source_stable)
        self.assertEqual(result.coverage.parser_source_classification,
                         "NON_COMPLETE")
        self.assertIsNotNone(result.provenance.source_sha256)
        self.assertGreater(result.coverage.rendered_text_block_count, 0)
        self.assertFalse(result.coverage.ui_completeness_attested)

        ended = payload("linear_cli")
        self.assertEqual(ended.coverage.parser_source_classification, "COMPLETE")
        self.assertFalse(ended.coverage.session_terminal_attested)
        self.assertFalse(ended.coverage.ui_completeness_attested)

    def test_source_changed_and_unreadable_have_no_attested_identity(self):
        source = parse_claude_session(ROOT / "linear_cli.jsonl")
        changed = replace(source, source_stable=False,
                          source_sha256=None, source_size_bytes=None,
                          completeness="NON_COMPLETE")
        graph = build_readable_graph(changed)
        candidate = build_claude_payload(graph, changed)
        self.assertFalse(candidate.coverage.source_stable)
        self.assertIsNone(candidate.provenance.source_sha256)
        self.assertIsNone(candidate.provenance.source_size_bytes)
        with self.assertRaises(ValueError):
            build_claude_payload(graph, replace(changed, source_sha256="0" * 64))
        missing = parse_claude_session(ROOT / "missing.jsonl")
        self.assertIn("SOURCE_UNREADABLE", [d.code for d in missing.diagnostics])
        self.assertFalse(missing.source_stable)
        self.assertIsNone(missing.source_sha256)
        self.assertIsNone(missing.source_size_bytes)

    def test_payload_preserves_branches_rewind_gaps_and_duplicate_occurrences(self):
        branch = payload("branching_cli")
        self.assertIn("synthetic answer A", texts(branch))
        self.assertIn("synthetic answer B", texts(branch))
        self.assertEqual(branch.coverage.branch_count, 1)
        self.assertEqual(len(branch.branch_points), 1)
        self.assertFalse(branch.coverage.ui_completeness_attested)

        rewound = payload("rewound_last_prompt_cli")
        self.assertTrue(rewound.leaf_hints[0].leaf_hint_rewound)
        self.assertIn("synthetic later answer", texts(rewound))

        gap = payload("phantom_parent_after_resume_cli")
        self.assertEqual(gap.coverage.graph_gap_count, 1)
        self.assertEqual(gap.graph_gaps[0].source_line, 3)
        self.assertIn("synthetic answer after resume", texts(gap))

        replay = payload("duplicate_replay_cli")
        self.assertEqual(replay.coverage.branch_count, 0)
        self.assertEqual(replay.coverage.duplicate_uuid_count, 2)
        self.assertEqual(len(replay.duplicate_occurrences), 2)
        self.assertEqual(len(replay.readable_nodes), 4)
        self.assertEqual(texts(replay).count("synthetic replay answer"), 2)

    def test_summary_and_tool_events_remain_bounded(self):
        summary = payload("compaction_cli")
        self.assertIn("compact_summary",
                      [node.semantic_kind for node in summary.readable_nodes])
        self.assertIn("compact_boundary", [event.kind for event in summary.events])
        tools = payload("sensitive_canaries_cli")
        self.assertEqual([event.kind for event in tools.events],
                         ["tool_use", "tool_result_present"])
        self.assertEqual(tools.events[0].tool_name, "SyntheticTool")

    def test_no_source_reopen_and_candidate_bytes_are_deterministic(self):
        for path in sorted(ROOT.glob("*.jsonl")):
            with self.subTest(path=path.name):
                source = parse_claude_session(path)
                graph = build_readable_graph(source)
                with mock.patch.object(Path, "open", side_effect=AssertionError("source reopened")):
                    first = build_claude_payload(graph, source)
                    second = build_claude_payload(graph, source)
                self.assertEqual(first.as_dict(), second.as_dict())
                self.assertEqual(first.candidate_json_bytes(),
                                 second.candidate_json_bytes())
                self.assertEqual(json.loads(first.candidate_json_bytes()),
                                 first.as_dict())

    def test_privacy_canaries_do_not_enter_payload_or_coverage(self):
        for path in sorted(ROOT.glob("*.jsonl")):
            with self.subTest(path=path.name):
                raw = path.read_text(encoding="utf-8")
                canaries = set(re.findall(r"SYNTHETIC_[A-Z_]+_CANARY", raw))
                result = payload(path.stem)
                views = (repr(result), json.dumps(asdict(result), sort_keys=True),
                         result.candidate_json_bytes().decode("utf-8"))
                for canary in canaries:
                    for view in views:
                        self.assertNotIn(canary, view)
                for forbidden in (str(path), path.parent.name,
                                  "session-synthetic-"):
                    for view in views:
                        self.assertNotIn(forbidden, view)
        sensitive = payload("sensitive_canaries_cli").candidate_json_bytes().decode()
        for raw_id in ('"user-1"', '"assistant-1"', '"attachment-1"',
                       '"tool-result-1"', '"tool-use-1"'):
            self.assertNotIn(raw_id, sensitive)


if __name__ == "__main__":
    unittest.main()
