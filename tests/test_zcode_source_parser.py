"""Synthetic-contract tests for the internal ZCode source parser."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_preserve._zcode_source import parse_zcode_session


ROOT = Path(__file__).parent / "fixtures" / "zcode"


def database_from_fixture(testcase, fixture):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / "db.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript((ROOT / fixture).read_text(encoding="utf-8"))
    connection.close()
    return path


def codes(result):
    return [item.code for item in result.diagnostics]


def texts(result):
    return [(item.role, item.semantic_kind, item.text)
            for item in result.text_items]


class ZCodeSourceParserContract(unittest.TestCase):
    def test_linear_visible_session_exports_only_visible_text(self):
        path = database_from_fixture(self, "linear_visible.sql")
        result = parse_zcode_session(path, "session-synthetic-zcode-linear")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertTrue(result.source_stable)
        self.assertEqual(len(result.selected_session_sha256), 64)
        self.assertEqual(
            texts(result),
            [
                ("user", "ordinary_user", "synthetic ZCode user prompt"),
                ("assistant", "ordinary_assistant",
                 "synthetic ZCode assistant answer"),
            ],
        )
        self.assertEqual((result.message_count, result.part_count), (2, 2))
        self.assertEqual(result.hidden_message_count, 0)
        self.assertEqual(result.reasoning_part_count, 0)

    def test_hidden_reasoning_and_raw_tool_payloads_do_not_leak(self):
        path = database_from_fixture(self, "privacy_hidden_tool.sql")
        result = parse_zcode_session(path, "session-synthetic-zcode-privacy")
        self.assertEqual(result.completeness, "COMPLETE")
        self.assertEqual(
            texts(result),
            [
                ("user", "ordinary_user", "synthetic safe user text"),
                ("assistant", "ordinary_assistant",
                 "synthetic safe assistant text"),
            ],
        )
        self.assertEqual(result.hidden_message_count, 1)
        self.assertEqual(result.reasoning_part_count, 1)
        self.assertEqual(
            [(event.tool_name, event.tool_ref, event.status)
             for event in result.tool_events],
            [("SyntheticTool", "tool-000001", "completed")],
        )
        safe = json.dumps(asdict(result), sort_keys=True)
        raw = (ROOT / "privacy_hidden_tool.sql").read_text(encoding="utf-8")
        for token in (
            "SYNTHETIC_ZCODE_HIDDEN_TEXT_CANARY",
            "SYNTHETIC_ZCODE_REASONING_CANARY",
            "SYNTHETIC_ZCODE_TOOL_INPUT_CANARY",
            "SYNTHETIC_ZCODE_TOOL_OUTPUT_CANARY",
        ):
            self.assertIn(token, raw)
            self.assertNotIn(token, safe)

    def test_unknown_part_is_fail_visible_without_raw_value_leak(self):
        path = database_from_fixture(self, "unknown_part.sql")
        result = parse_zcode_session(path, "session-synthetic-zcode-unknown")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("UNKNOWN_PART_TYPE", codes(result))
        self.assertEqual(texts(result),
                         [("user", "ordinary_user", "synthetic known text")])
        self.assertNotIn("SYNTHETIC_ZCODE_UNKNOWN_PART_CANARY", repr(result))

    def test_session_not_found_is_explicit(self):
        path = database_from_fixture(self, "linear_visible.sql")
        result = parse_zcode_session(path, "session-synthetic-not-here")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertFalse(result.source_stable)
        self.assertIsNone(result.selected_session_sha256)
        self.assertIn("SESSION_NOT_FOUND", codes(result))

    def test_invalid_database_schema_is_explicit(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "db.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        result = parse_zcode_session(path, "session-synthetic")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("UNSUPPORTED_DATABASE_SCHEMA", codes(result))
        self.assertIsNone(result.selected_session_sha256)

    def test_selected_session_digest_ignores_unrelated_session_rows(self):
        path = database_from_fixture(self, "linear_visible.sql")
        first = parse_zcode_session(path, "session-synthetic-zcode-linear")
        connection = sqlite3.connect(path)
        connection.execute(
            """
            INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("session-synthetic-other", "other", "/Users/testowner/other",
             "3.synthetic", 1, 2),
        )
        connection.execute(
            """
            INSERT INTO message VALUES (?, ?, ?, ?)
            """,
            ("message-other", "session-synthetic-other",
             json.dumps({
                 "role": "user",
                 "semantics": {
                     "kind": "user_prompt",
                     "origin": "real_user",
                     "providerVisibility": "visible",
                     "transcriptVisibility": "visible",
                     "uiVisibility": "visible",
                 },
                 "time": {"created": 1},
             }),
             1),
        )
        connection.execute(
            """
            INSERT INTO part VALUES (?, ?, ?, ?, ?)
            """,
            ("part-other", "message-other", "session-synthetic-other",
             json.dumps({"type": "text", "text": "unrelated session text"}),
             1),
        )
        connection.commit()
        connection.close()
        second = parse_zcode_session(path, "session-synthetic-zcode-linear")
        self.assertEqual(first.selected_session_sha256,
                         second.selected_session_sha256)
        self.assertEqual(texts(first), texts(second))

    def test_duplicate_message_sequence_blocks_complete(self):
        path = database_from_fixture(self, "linear_visible.sql")
        connection = sqlite3.connect(path)
        data = json.dumps({
            "role": "assistant",
            "parentID": "message-synthetic-user",
            "semantics": {
                "kind": "assistant_response",
                "origin": "agent_runtime",
                "providerVisibility": "visible",
                "transcriptVisibility": "visible",
                "uiVisibility": "visible",
            },
            "time": {"created": 3, "completed": 4},
        })
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("message-sequence-collision",
             "session-synthetic-zcode-linear", data, 2),
        )
        connection.commit()
        connection.close()
        result = parse_zcode_session(path, "session-synthetic-zcode-linear")
        self.assertEqual(result.completeness, "NON_COMPLETE")
        self.assertIn("DUPLICATE_MESSAGE_SEQUENCE", codes(result))

    def test_result_is_deterministic(self):
        for fixture, session_id in (
            ("linear_visible.sql", "session-synthetic-zcode-linear"),
            ("privacy_hidden_tool.sql", "session-synthetic-zcode-privacy"),
            ("unknown_part.sql", "session-synthetic-zcode-unknown"),
        ):
            with self.subTest(fixture=fixture):
                path = database_from_fixture(self, fixture)
                first = parse_zcode_session(path, session_id)
                second = parse_zcode_session(path, session_id)
                self.assertEqual(asdict(first), asdict(second))

    def test_path_and_raw_session_id_are_not_returned(self):
        path = database_from_fixture(self, "linear_visible.sql")
        result = parse_zcode_session(path, "session-synthetic-zcode-linear")
        safe = repr(result)
        self.assertNotIn(str(path), safe)
        self.assertNotIn("session-synthetic-zcode-linear", safe)


if __name__ == "__main__":
    unittest.main()
