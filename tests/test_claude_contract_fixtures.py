"""Contract tests for hand-authored Claude vNext source fixtures.

These tests protect the pre-adapter fixture corpus. They deliberately do not
implement Claude export behavior.
"""

import json
from pathlib import Path
import unittest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "claude"
TRUNCATED = "truncated_tail_cli.jsonl"

EXPECTED_JSONL = {
    "branching_cli.jsonl",
    "compaction_cli.jsonl",
    "fallback_cli.jsonl",
    "known_ignored_cli.jsonl",
    "linear_cli.jsonl",
    "mixed_entrypoint.jsonl",
    "phantom_parent_after_resume_cli.jsonl",
    "sensitive_canaries_cli.jsonl",
    "stale_last_prompt_cli.jsonl",
    "truncated_tail_cli.jsonl",
    "unknown_attachment_cli.jsonl",
    "unknown_block_cli.jsonl",
    "unknown_record_cli.jsonl",
    "unsupported_sdk_cli.jsonl",
}


class ClaudeContractFixtures(unittest.TestCase):
    def test_fixture_inventory_is_explicit(self):
        actual = {path.name for path in FIXTURE_ROOT.glob("*.jsonl")}
        self.assertEqual(actual, EXPECTED_JSONL)

    def test_all_non_truncated_fixtures_are_jsonl(self):
        for path in sorted(FIXTURE_ROOT.glob("*.jsonl")):
            if path.name == TRUNCATED:
                continue
            with self.subTest(path=path.name):
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    self.assertTrue(line)
                    value = json.loads(line)
                    self.assertIsInstance(value, dict, (path.name, line_number))

    def test_truncated_fixture_fails_only_at_tail(self):
        lines = (FIXTURE_ROOT / TRUNCATED).read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines[:-1]:
            self.assertIsInstance(json.loads(line), dict)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(lines[-1])

    def test_session_ids_are_synthetic(self):
        for path in sorted(FIXTURE_ROOT.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = value.get("sessionId")
                if session_id is not None:
                    self.assertTrue(
                        session_id.startswith("session-synthetic-"),
                        (path.name, session_id),
                    )
    def test_no_fixture_contains_owner_home_coordinates(self):
        forbidden = ("/Users/", "/home/")
        for path in sorted(FIXTURE_ROOT.glob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, text, (path.name, marker))

    def test_contract_readme_keeps_provider_specific_boundary(self):
        text = (FIXTURE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("entrypoint claimed at first release", text)
        self.assertIn("Any unknown top-level record type", text)
        self.assertIn("They are not a generic conversation schema", text)


if __name__ == "__main__":
    unittest.main()
