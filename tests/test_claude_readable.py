"""G4b preservation projection over hand-authored G4a parser fixtures."""

import contextlib
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from codex_preserve._claude_readable import build_readable_graph
from codex_preserve._claude_source import parse_claude_session


ROOT = Path(__file__).parent / "fixtures" / "claude"


def graph(name):
    return build_readable_graph(parse_claude_session(ROOT / (name + ".jsonl")))


def texts(result):
    return [block.text for node in result.nodes for block in node.text_blocks]


def node_at(result, line):
    return next(node for node in result.nodes if node.source_line == line)


class ClaudeReadableGraphContract(unittest.TestCase):
    def test_parallel_stale_leaf_retains_every_persisted_answer(self):
        result = graph("parallel_stale_leaf_cli")
        self.assertEqual(result.source_completeness, "NON_COMPLETE")
        self.assertIsNone(result.active_head_id)
        self.assertEqual(result.presentation_order, "persisted_source_line")
        self.assertEqual(texts(result), [
            "synthetic initial user", "synthetic initial assistant answer",
            "synthetic follow-up user", "synthetic complete follow-up answer",
            "synthetic update request", "synthetic updated complete answer",
        ])
        self.assertEqual([node.source_line for node in result.nodes],
                         [1, 2, 3, 5, 6, 7, 8])
        self.assertEqual(result.leaf_hints[0].leaf_hint_id, node_at(result, 3).node_id)
        self.assertIsNone(result.leaf_hints[0].leaf_hint_explicit)
        self.assertIsNone(result.leaf_hints[0].leaf_hint_rewound)
        self.assertEqual(result.branch_count, 1)
        self.assertEqual(result.branch_points[0].parent_record_id,
                         node_at(result, 2).record_id)
        self.assertEqual(result.branch_points[0].child_record_ids,
                         (node_at(result, 3).record_id,
                          node_at(result, 5).record_id))
        self.assertEqual([(event.kind, event.source_line) for event in result.events],
                         [("tool_use", 2), ("tool_result_present", 3)])

    def test_leaf_hint_flags_cannot_change_retained_text(self):
        parsed = parse_claude_session(ROOT / "parallel_stale_leaf_cli.jsonl")
        original = build_readable_graph(parsed)
        changed_records = tuple(
            replace(record, leaf_hint_id=None, leaf_hint_explicit=True,
                    leaf_hint_rewound=True) if record.kind == "last-prompt"
            else record for record in parsed.records
        )
        changed = build_readable_graph(replace(parsed, records=changed_records))
        self.assertEqual(texts(original), texts(changed))
        self.assertEqual(original.branch_points, changed.branch_points)
        self.assertIsNone(changed.active_head_id)

    def test_both_branches_remain_without_active_path_selection(self):
        result = graph("branching_cli")
        self.assertEqual(result.source_completeness, "NON_COMPLETE")
        self.assertEqual(result.branch_count, 1)
        self.assertIn("synthetic answer A", texts(result))
        self.assertIn("synthetic answer B", texts(result))
        self.assertEqual(result.readable_node_count, 6)
        self.assertEqual(result.rendered_text_block_count, 6)
        self.assertEqual(result.branch_points[0].child_record_ids,
                         (node_at(result, 3).record_id,
                          node_at(result, 5).record_id))
        self.assertIsNone(result.active_head_id)

    def test_explicit_rewind_is_a_pointer_fact_only(self):
        result = graph("rewound_last_prompt_cli")
        self.assertTrue(result.leaf_hints[0].leaf_hint_explicit)
        self.assertTrue(result.leaf_hints[0].leaf_hint_rewound)
        self.assertEqual(result.leaf_hints[0].leaf_hint_id, node_at(result, 2).node_id)
        self.assertIn("synthetic later prompt", texts(result))
        self.assertIn("synthetic later answer", texts(result))

    def test_phantom_parent_remains_gap_with_safe_descendants(self):
        result = graph("phantom_parent_after_resume_cli")
        self.assertEqual(result.source_completeness, "NON_COMPLETE")
        self.assertEqual((result.graph_gap_count, result.diagnostics.graph_gap_count),
                         (1, 1))
        self.assertIn(("GRAPH_LINK_GAP", 1), result.diagnostics.counts_by_code)
        self.assertEqual((node_at(result, 3).parent_link,
                          node_at(result, 3).parent_id,
                          node_at(result, 3).parent_record_id),
                         ("missing", None, None))
        self.assertEqual(node_at(result, 5).parent_record_id,
                         node_at(result, 4).record_id)
        self.assertIn("synthetic prompt after resume", texts(result))
        self.assertIn("synthetic answer after resume", texts(result))

    def test_compaction_summary_keeps_its_semantics_and_boundary(self):
        result = graph("compaction_cli")
        summary = node_at(result, 4)
        self.assertEqual((summary.role, summary.semantic_kind,
                          summary.text_blocks[0].semantic_kind),
                         ("user", "compact_summary", "compact_summary"))
        self.assertEqual(summary.text_blocks[0].text, "synthetic compact summary")
        self.assertIn("synthetic pre-compaction answer", texts(result))
        self.assertIn(("compact_boundary", 3),
                      [(event.kind, event.source_line) for event in result.events])

    def test_source_completeness_and_readable_coverage_are_distinct(self):
        result = graph("unknown_block_cli")
        self.assertEqual(result.source_completeness, "NON_COMPLETE")
        self.assertEqual(result.unknown_count, 1)
        self.assertGreater(result.rendered_text_block_count, 0)
        self.assertIn("synthetic known text", texts(result))

    def test_record_occurrence_identity_differs_from_graph_identity(self):
        parsed = parse_claude_session(ROOT / "linear_cli.jsonl")
        duplicated = replace(parsed, records=parsed.records +
                             (replace(parsed.records[1], line=99),))
        result = build_readable_graph(duplicated)
        first, second = node_at(result, 2), node_at(result, 99)
        self.assertNotEqual(first.record_id, second.record_id)
        self.assertEqual(first.node_id, second.node_id)
        self.assertIsNone(result.leaf_hints[0].leaf_hint_explicit)
        self.assertEqual(texts(result).count("synthetic assistant text"), 2)

    def test_persisted_duplicate_replay_is_not_a_false_branch(self):
        result = graph("duplicate_replay_cli")
        self.assertEqual(result.source_completeness, "NON_COMPLETE")
        self.assertEqual(result.branch_count, 0)
        self.assertEqual(result.branch_points, ())
        self.assertEqual(texts(result), [
            "synthetic replay user", "synthetic replay answer",
            "synthetic replay user", "synthetic replay answer",
        ])
        self.assertEqual(len({node.record_id for node in result.nodes}), 4)
        self.assertEqual(result.nodes[0].node_id, result.nodes[2].node_id)
        self.assertEqual(result.nodes[1].node_id, result.nodes[3].node_id)
        self.assertIn(("DUPLICATE_UUID", 2), result.diagnostics.counts_by_code)

    def test_duplicate_parent_occurrence_does_not_invent_record_edge(self):
        parsed = parse_claude_session(ROOT / "linear_cli.jsonl")
        duplicated = replace(parsed, records=parsed.records +
                             (replace(parsed.records[0], line=99),))
        result = build_readable_graph(duplicated)
        self.assertEqual(node_at(result, 2).parent_link, "linked")
        self.assertEqual(node_at(result, 2).parent_id, node_at(result, 1).node_id)
        self.assertIsNone(node_at(result, 2).parent_record_id)

    def test_projection_reads_only_parse_result(self):
        parsed = parse_claude_session(ROOT / "linear_cli.jsonl")
        with mock.patch.object(Path, "open", side_effect=AssertionError("source reopened")):
            result = build_readable_graph(parsed)
        self.assertEqual(result.source_completeness, "COMPLETE")

    def test_privacy_canaries_absent_from_all_graph_views_and_output(self):
        names = ("sensitive_canaries_cli", "known_ignored_cli",
                 "unknown_record_cli", "unknown_block_cli",
                 "unknown_attachment_cli", "phantom_parent_after_resume_cli",
                 "parallel_stale_leaf_cli")
        for name in names:
            with self.subTest(name=name):
                raw = (ROOT / (name + ".jsonl")).read_text(encoding="utf-8")
                canaries = set(re.findall(r"SYNTHETIC_[A-Z_]+_CANARY", raw))
                self.assertTrue(canaries)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = graph(name)
                views = (repr(result), json.dumps(asdict(result), sort_keys=True),
                         repr(result.diagnostics), stdout.getvalue(), stderr.getvalue())
                for canary in canaries:
                    for view in views:
                        self.assertNotIn(canary, view)

    def test_deterministic_across_all_synthetic_sources(self):
        for path in sorted(ROOT.glob("*.jsonl")):
            with self.subTest(path=path.name):
                parsed = parse_claude_session(path)
                first = build_readable_graph(parsed)
                second = build_readable_graph(parsed)
                self.assertEqual(asdict(first), asdict(second))


if __name__ == "__main__":
    unittest.main()
