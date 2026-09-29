"""Synthetic-contract tests for the internal Kimi Code source parser."""

import contextlib
from dataclasses import asdict
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from codex_preserve._kimi_source import parse_kimi_session


ROOT = Path(__file__).parent / "fixtures" / "kimi"


def parsed(name):
    return parse_kimi_session(ROOT / name)


def texts(result):
    return [(item.role, item.semantic_kind, item.text)
            for item in result.text_items]


def codes(result):
    return [item.code for item in result.diagnostics]


class KimiSourceParserContract(unittest.TestCase):
    def test_linear_session_uses_canonical_user_and_assistant_surfaces(self):
        result = parsed("linear_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertEqual(result.protocol_version, "1.4")
        self.assertEqual(result.mirrored_turn_prompt_count, 1)
        self.assertEqual(
            texts(result),
            [
                ("user", "ordinary_user", "synthetic hello"),
                ("assistant", "ordinary_assistant", "synthetic Kimi answer"),
            ],
        )
        self.assertTrue(result.source_stable)
        self.assertEqual(len(result.state_sha256), 64)
        self.assertEqual(len(result.wire_sha256), 64)

    def test_privacy_tool_fixture_keeps_only_safe_text_and_tool_structure(self):
        result = parsed("privacy_tool_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertEqual(
            texts(result),
            [
                ("user", "ordinary_user", "synthetic tool request"),
                ("assistant", "ordinary_assistant", "synthetic safe answer"),
            ],
        )
        self.assertEqual(
            [(event.kind, event.tool_name, event.tool_ref, event.is_error)
             for event in result.tool_events],
            [
                ("tool_call", "SyntheticTool", "tool-000001", None),
                ("tool_result", None, "tool-000001", False),
            ],
        )

    def test_compaction_bodies_do_not_become_readable_text(self):
        result = parsed("compaction_cli")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertEqual(
            texts(result),
            [
                ("user", "ordinary_user", "synthetic after compaction"),
                ("assistant", "ordinary_assistant",
                 "synthetic answer after compaction"),
            ],
        )
        safe = json.dumps(asdict(result), sort_keys=True)
        self.assertNotIn("SYNTHETIC_KIMI_COMPACTION_SUMMARY_CANARY", safe)
        self.assertNotIn("SYNTHETIC_KIMI_CONTEXT_SUMMARY_CANARY", safe)

    def test_unknown_top_level_record_is_fail_visible_without_raw_value_leak(self):
        result = parsed("unknown_record_cli")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("UNKNOWN_RECORD", codes(result))
        self.assertNotIn("SYNTHETIC_KIMI_UNKNOWN_CANARY", repr(result))

    def test_all_privacy_canaries_are_absent_from_safe_model_and_output(self):
        for name in ("privacy_tool_cli", "compaction_cli", "unknown_record_cli"):
            with self.subTest(name=name):
                raw = ""
                for path in (ROOT / name).rglob("*"):
                    if path.is_file():
                        raw += path.read_text(encoding="utf-8")
                canaries = set(re.findall(r"SYNTHETIC_KIMI_[A-Z_]+_CANARY", raw))
                self.assertTrue(canaries)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = parsed(name)
                views = (
                    repr(result),
                    json.dumps(asdict(result), sort_keys=True),
                    stdout.getvalue(),
                    stderr.getvalue(),
                )
                for canary in canaries:
                    for view in views:
                        self.assertNotIn(canary, view)

    def test_turn_prompt_without_canonical_user_message_blocks_complete(self):
        source = ROOT / "linear_cli"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "session"
            (target / "agents" / "main").mkdir(parents=True)
            (target / "state.json").write_bytes((source / "state.json").read_bytes())
            rows = [
                json.loads(line)
                for line in (source / "agents" / "main" / "wire.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ]
            rows = [
                row for row in rows
                if row.get("type") != "context.append_message"
            ]
            (target / "agents" / "main" / "wire.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            result = parse_kimi_session(target)
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("UNMATCHED_TURN_PROMPT", codes(result))

    def test_result_is_deterministic(self):
        for session in sorted(path for path in ROOT.iterdir() if path.is_dir()):
            with self.subTest(session=session.name):
                first = parse_kimi_session(session)
                second = parse_kimi_session(session)
                self.assertEqual(asdict(first), asdict(second))

    def test_invalid_selection_does_not_read_arbitrary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state.json").write_text(
                "SYNTHETIC_KIMI_ARBITRARY_STATE_CANARY", encoding="utf-8")
            result = parse_kimi_session(root / "missing")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("INVALID_SOURCE_SELECTION", codes(result))
        self.assertNotIn("SYNTHETIC_KIMI_ARBITRARY_STATE_CANARY", repr(result))


if __name__ == "__main__":
    unittest.main()
