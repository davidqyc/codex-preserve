"""Synthetic end-to-end schema-3 package tests for non-Codex providers."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_preserve import exporter
from codex_preserve._claude_source import parse_claude_session
from codex_preserve._claude_v3 import claude_v3_spec
from codex_preserve._codex_v3 import codex_v3_spec
from codex_preserve._kimi_source import parse_kimi_session
from codex_preserve._kimi_v3 import kimi_v3_spec
from codex_preserve._v3_package import (
    CONVERSATION_FILENAME,
    RECEIPT_FILENAME,
    verify_v3_package,
    write_v3_package,
)
from codex_preserve._zcode_source import parse_zcode_session
from codex_preserve._zcode_v3 import zcode_v3_spec
from tests.test_codex_conversation_export import minimal_session


FIXTURES = Path(__file__).parent / "fixtures"


class V3ProviderAdapters(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _zcode_db(self, fixture):
        path = self.root / (fixture + ".sqlite")
        connection = sqlite3.connect(path)
        connection.executescript(
            (FIXTURES / "zcode" / fixture).read_text(encoding="utf-8"))
        connection.close()
        return path

    def _write_verify(self, name, spec):
        package = self.root / name
        write_v3_package(package, spec)
        result = verify_v3_package(package)
        self.assertEqual((result["verdict"], result["exit_code"]), ("PASS", 0))
        receipt = json.loads(
            (package / RECEIPT_FILENAME).read_text(encoding="utf-8"))
        conversation = (package / CONVERSATION_FILENAME).read_text(
            encoding="utf-8")
        return package, receipt, conversation

    def test_codex_v3_reuses_proven_parser_without_old_physical_names(self):
        source = minimal_session().write(self.root)
        options = exporter.build_parser().parse_args([
            "--rollout", str(source),
            "--output-dir", str(self.root / "unused"),
            "--no-git-probe",
            "--quiet",
        ])
        options.generated_at = "2026-09-30T00:00:00Z"
        context = exporter.run_export(options)
        package, receipt, conversation = self._write_verify(
            "codex-package", codex_v3_spec(context))
        self.assertEqual(receipt["provider"], "codex")
        self.assertIn("# Conversation", conversation)
        self.assertIn("Audit finished; one dirty file.", conversation)
        self.assertFalse((package / "对话记录.md").exists())
        self.assertFalse((package / "ConversationExport.receipt.json").exists())
        self.assertEqual(
            receipt["provider_receipt"]["provider_export_status"], "COMPLETE")
        self.assertFalse(
            receipt["provider_receipt"]["ui_completeness_attested"])

    def test_claude_v3_preserves_loss_averse_readable_graph(self):
        source = parse_claude_session(
            FIXTURES / "claude" / "parallel_stale_leaf_cli.jsonl")
        _, receipt, conversation = self._write_verify(
            "claude-package", claude_v3_spec(source))
        self.assertEqual(receipt["provider"], "claude")
        self.assertIn("synthetic complete follow-up answer", conversation)
        self.assertIn("synthetic updated complete answer", conversation)
        self.assertNotIn("SYNTHETIC_PARALLEL_TOOL_INPUT_CANARY", conversation)
        provider = receipt["provider_receipt"]
        self.assertFalse(
            provider["coverage"]["ui_completeness_attested"])
        self.assertFalse(provider["active_head_selected"])

    def test_kimi_v3_does_not_duplicate_turn_prompt_or_leak_tool_payload(self):
        source = parse_kimi_session(FIXTURES / "kimi" / "privacy_tool_cli")
        _, receipt, conversation = self._write_verify(
            "kimi-package", kimi_v3_spec(source))
        self.assertEqual(receipt["provider"], "kimi")
        self.assertEqual(conversation.count("synthetic tool request"), 1)
        self.assertIn("synthetic safe answer", conversation)
        for canary in (
            "SYNTHETIC_KIMI_THINKING_CANARY",
            "SYNTHETIC_KIMI_TOOL_ARGS_CANARY",
            "SYNTHETIC_KIMI_TOOL_RESULT_CANARY",
        ):
            self.assertNotIn(canary, conversation)
            self.assertNotIn(canary, json.dumps(receipt, sort_keys=True))
        self.assertFalse(
            receipt["provider_receipt"]["ui_completeness_attested"])

    def test_zcode_v3_filters_hidden_and_reasoning_tool_bodies(self):
        path = self._zcode_db("privacy_hidden_tool.sql")
        source = parse_zcode_session(
            path, "session-synthetic-zcode-privacy")
        _, receipt, conversation = self._write_verify(
            "zcode-package", zcode_v3_spec(source))
        self.assertEqual(receipt["provider"], "zcode")
        self.assertIn("synthetic safe user text", conversation)
        self.assertIn("synthetic safe assistant text", conversation)
        for canary in (
            "SYNTHETIC_ZCODE_HIDDEN_TEXT_CANARY",
            "SYNTHETIC_ZCODE_REASONING_CANARY",
            "SYNTHETIC_ZCODE_TOOL_INPUT_CANARY",
            "SYNTHETIC_ZCODE_TOOL_OUTPUT_CANARY",
        ):
            self.assertNotIn(canary, conversation)
            self.assertNotIn(canary, json.dumps(receipt, sort_keys=True))
        provider = receipt["provider_receipt"]
        self.assertEqual(provider["hidden_message_count"], 1)
        self.assertEqual(provider["reasoning_part_count"], 1)

    def test_noncomplete_sources_still_materialize_auditable_packages(self):
        claude = parse_claude_session(
            FIXTURES / "claude" / "unknown_block_cli.jsonl")
        package, receipt, _ = self._write_verify(
            "claude-noncomplete", claude_v3_spec(claude))
        self.assertEqual(receipt["coverage_status"], "NON_COMPLETE")
        manifest = json.loads(
            (package / "package.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["coverage_status"], "NON_COMPLETE")

        kimi = parse_kimi_session(FIXTURES / "kimi" / "unknown_record_cli")
        _, receipt, _ = self._write_verify(
            "kimi-noncomplete", kimi_v3_spec(kimi))
        self.assertEqual(receipt["coverage_status"], "NON_COMPLETE")

        zdb = self._zcode_db("unknown_part.sql")
        zcode = parse_zcode_session(
            zdb, "session-synthetic-zcode-unknown")
        _, receipt, _ = self._write_verify(
            "zcode-noncomplete", zcode_v3_spec(zcode))
        self.assertEqual(receipt["coverage_status"], "NON_COMPLETE")

    def test_provider_package_bytes_are_deterministic(self):
        source = parse_kimi_session(FIXTURES / "kimi" / "linear_cli")
        first = kimi_v3_spec(source)
        second = kimi_v3_spec(source)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
