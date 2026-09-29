"""Contract tests for hand-authored Kimi Code and ZCode vNext fixtures."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).parent / "fixtures"
KIMI = ROOT / "kimi"
ZCODE = ROOT / "zcode"


class KimiContractFixtures(unittest.TestCase):
    def test_fixture_inventory_is_explicit(self):
        actual = sorted(
            path.name for path in KIMI.iterdir()
            if path.is_dir()
        )
        self.assertEqual(
            actual,
            ["compaction_cli", "linear_cli", "privacy_tool_cli",
             "unknown_record_cli"],
        )

    def test_every_session_has_state_and_main_wire(self):
        for session in sorted(path for path in KIMI.iterdir() if path.is_dir()):
            with self.subTest(session=session.name):
                state = session / "state.json"
                wire = session / "agents" / "main" / "wire.jsonl"
                self.assertTrue(state.is_file())
                self.assertTrue(wire.is_file())
                self.assertIsInstance(json.loads(state.read_text(encoding="utf-8")),
                                      dict)
                for line in wire.read_text(encoding="utf-8").splitlines():
                    self.assertIsInstance(json.loads(line), dict)

    def test_linear_prompt_mirror_is_exact_but_not_unique_source(self):
        wire = KIMI / "linear_cli" / "agents" / "main" / "wire.jsonl"
        rows = [json.loads(line)
                for line in wire.read_text(encoding="utf-8").splitlines()]
        canonical = next(
            row for row in rows
            if row.get("type") == "context.append_message"
        )
        mirror = next(row for row in rows if row.get("type") == "turn.prompt")
        self.assertEqual(canonical["message"]["origin"]["kind"], "user")
        self.assertEqual(
            canonical["message"]["content"][0]["text"],
            mirror["input"][0]["text"],
        )

    def test_contract_readme_keeps_provider_specific_boundary(self):
        text = (KIMI / "README.md").read_text(encoding="utf-8")
        self.assertIn("agents/main/wire.jsonl", text)
        self.assertIn("event.part.type=text", text)
        self.assertIn("They are not a generic provider schema", text)


class ZCodeContractFixtures(unittest.TestCase):
    def _database(self, fixture_name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "synthetic.sqlite"
        connection = sqlite3.connect(path)
        script = (ZCODE / fixture_name).read_text(encoding="utf-8")
        connection.executescript(script)
        return connection

    def test_fixture_inventory_is_explicit(self):
        actual = sorted(path.name for path in ZCODE.glob("*.sql"))
        self.assertEqual(
            actual,
            ["linear_visible.sql", "privacy_hidden_tool.sql", "unknown_part.sql"],
        )

    def test_linear_fixture_has_visible_user_and_assistant_text(self):
        connection = self._database("linear_visible.sql")
        rows = connection.execute(
            """
            SELECT m.sequence, m.data, p.sequence, p.data
            FROM message AS m
            JOIN part AS p ON p.message_id = m.id
            ORDER BY m.sequence, p.sequence
            """
        ).fetchall()
        self.assertEqual(len(rows), 2)
        roles = [json.loads(row[1])["role"] for row in rows]
        texts = [json.loads(row[3])["text"] for row in rows]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(
            texts,
            ["synthetic ZCode user prompt", "synthetic ZCode assistant answer"],
        )

    def test_privacy_fixture_contains_hidden_reasoning_and_tool_canaries(self):
        connection = self._database("privacy_hidden_tool.sql")
        message_rows = [
            json.loads(row[0])
            for row in connection.execute("SELECT data FROM message")
        ]
        part_rows = [
            json.loads(row[0])
            for row in connection.execute("SELECT data FROM part")
        ]
        hidden = [
            row for row in message_rows
            if row.get("semantics", {}).get("transcriptVisibility") == "hidden"
        ]
        self.assertEqual(len(hidden), 1)
        self.assertTrue(hidden[0].get("synthetic"))
        self.assertIn("reasoning", [row.get("type") for row in part_rows])
        self.assertIn("tool", [row.get("type") for row in part_rows])

    def test_unknown_part_fixture_is_structurally_valid_sql(self):
        connection = self._database("unknown_part.sql")
        types = [
            json.loads(row[0]).get("type")
            for row in connection.execute("SELECT data FROM part ORDER BY sequence")
        ]
        self.assertEqual(types, ["text", "future-part"])

    def test_contract_readme_pins_sqlite_as_primary_store(self):
        text = (ZCODE / "README.md").read_text(encoding="utf-8")
        self.assertIn("$ZCODE_HOME/cli/db/db.sqlite", text)
        self.assertIn("part.type=text", text)
        self.assertIn("They are not a generic provider schema", text)


class CrossProviderFixtureHygiene(unittest.TestCase):
    def test_no_real_home_paths_or_raw_credentials(self):
        for root in (KIMI, ZCODE):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                mac_home = "/Users/" + "realperson/"
                linux_home = "/home/" + "realperson/"
                self.assertNotIn(mac_home, text)
                self.assertNotIn(linux_home, text)
                self.assertNotIn("Bearer ", text)


if __name__ == "__main__":
    unittest.main()
