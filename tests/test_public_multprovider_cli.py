"""Public Session Preserve CLI smoke tests across all four v0.2 providers."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_preserve import cli, exporter
from tests.test_codex_conversation_export import minimal_session


FIXTURES = Path(__file__).parent / "fixtures"


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class PublicMultiProviderCli(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "exports"

    def _single_package(self):
        packages = [p for p in self.output.iterdir() if p.is_dir()]
        self.assertEqual(len(packages), 1, packages)
        package = packages[0]
        self.assertTrue((package / "conversation.md").is_file())
        self.assertTrue((package / "export.receipt.json").is_file())
        self.assertTrue((package / "package.manifest.json").is_file())
        self.assertFalse((package / "对话记录.md").exists())
        self.assertFalse((package / "ConversationExport.receipt.json").exists())
        return package

    def _verify_public_cli(self, package):
        with captured() as (out, err):
            code = cli.main(["verify", str(package), "--json"])
        self.assertEqual(code, 0, err.getvalue())
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["package_schema_version"], "3.0")
        self.assertEqual(receipt["tool"], "session-preserve")

    def _assert_provider_package(self, expected_provider):
        package = self._single_package()
        manifest = json.loads(
            (package / "package.manifest.json").read_text(encoding="utf-8")
        )
        receipt = json.loads(
            (package / "export.receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["provider"], expected_provider)
        self.assertEqual(receipt["provider"], expected_provider)
        self.assertEqual(manifest["package_schema_version"], "3.0")
        self.assertTrue(package.name.startswith(expected_provider + "__"))
        self._verify_public_cli(package)
        return package

    def test_codex_public_export_writes_schema3(self):
        source = minimal_session().write(self.root)
        with mock.patch.object(exporter, "resolve_session_thread_names",
                               return_value={}):
            with captured() as (out, err):
                code = cli.main([
                    "export", "codex",
                    "--rollout", str(source),
                    "--output-dir", str(self.output),
                    "--no-git-probe",
                ])
        self.assertEqual(code, 0, err.getvalue())
        surface = json.loads(out.getvalue())
        self.assertEqual(surface["provider"], "codex")
        self.assertEqual(surface["package_schema_version"], "3.0")
        package = self._assert_provider_package("codex")
        self.assertIn(
            "Audit finished; one dirty file.",
            (package / "conversation.md").read_text(encoding="utf-8"),
        )

    def test_claude_public_export_writes_schema3(self):
        source = FIXTURES / "claude" / "linear_cli.jsonl"
        with captured() as (out, err):
            code = cli.main([
                "export", "claude",
                "--source", str(source),
                "--output-dir", str(self.output),
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["provider"], "claude")
        package = self._assert_provider_package("claude")
        text = (package / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("synthetic user text", text)
        self.assertIn("synthetic assistant text", text)

    def test_kimi_public_export_writes_schema3(self):
        source = FIXTURES / "kimi" / "linear_cli"
        with captured() as (out, err):
            code = cli.main([
                "export", "kimi",
                "--source", str(source),
                "--output-dir", str(self.output),
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["provider"], "kimi")
        package = self._assert_provider_package("kimi")
        text = (package / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("synthetic hello", text)
        self.assertIn("synthetic Kimi answer", text)

    def test_zcode_public_export_writes_schema3(self):
        database = self.root / "zcode.sqlite"
        connection = sqlite3.connect(database)
        connection.executescript(
            (FIXTURES / "zcode" / "linear_visible.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.close()

        with captured() as (out, err):
            code = cli.main([
                "export", "zcode",
                "--database", str(database),
                "--session-id", "session-synthetic-zcode-linear",
                "--output-dir", str(self.output),
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["provider"], "zcode")
        package = self._assert_provider_package("zcode")
        text = (package / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("synthetic ZCode user prompt", text)
        self.assertIn("synthetic ZCode assistant answer", text)

    def test_pack_builds_derived_session_zip_without_breaking_package(self):
        source = FIXTURES / "claude" / "linear_cli.jsonl"
        with captured() as (_out, err):
            code = cli.main([
                "export", "claude",
                "--source", str(source),
                "--output-dir", str(self.output),
                "--quiet",
            ])
        self.assertEqual(code, 0, err.getvalue())
        package = self._single_package()
        with captured() as (out, err):
            code = cli.main(["pack", str(package)])
        self.assertEqual(code, 0, err.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "PACKED")
        self.assertFalse(result["canonical_package_dependency"])
        self.assertTrue((package / "session-package.zip").is_file())
        self._verify_public_cli(package)

    def test_unknown_provider_is_rejected_without_output(self):
        with captured() as (out, err):
            code = cli.main(["export", "other"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("unknown provider", err.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
