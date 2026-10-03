"""Static registry boundaries, native hooks and historical CLI options."""

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_preserve import cli, exporter
from codex_preserve import _provider_registry as registry
from codex_preserve._v3_package import V3PackageSpec, verify_v3_package
from tests.test_public_multprovider_cli import captured


class ProviderRegistryTests(unittest.TestCase):
    def test_only_the_four_released_provider_identities_are_registered(self):
        self.assertEqual(tuple(registry.PROVIDER_REGISTRY),
                         ("codex", "claude", "kimi", "zcode"))
        self.assertEqual(cli.PROVIDERS, tuple(registry.PROVIDER_REGISTRY))
        self.assertEqual(registry.PROVIDER_REGISTRY["kimi"].display_name,
                         "Kimi Code")

    def test_registry_and_entries_are_immutable(self):
        first = registry.ADAPTERS[0]
        with self.assertRaises(TypeError):
            registry.PROVIDER_REGISTRY["other"] = first
        with self.assertRaises(FrozenInstanceError):
            first.key = "other"

    def test_duplicate_and_empty_keys_are_rejected(self):
        first = registry.ADAPTERS[0]
        for entries in ([first, first], [replace(first, key="")]):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    registry._registry(entries)

    def test_codex_parser_keeps_rich_options_groups_defaults_and_help(self):
        old = exporter.build_parser()
        old.prog = "session-preserve export codex"
        old.description = (
            "Preserve one already-persisted local Codex session as a schema-3 package."
        )
        old.set_defaults(output_dir=cli.DEFAULT_OUTPUT_DIR)
        new = registry.PROVIDER_REGISTRY["codex"].build_parser(cli.DEFAULT_OUTPUT_DIR)

        def surface(parser):
            return [(a.option_strings, a.dest, a.required, a.nargs, a.const,
                     a.default, a.type, a.choices) for a in parser._actions]

        self.assertEqual(surface(old), surface(new))
        self.assertEqual(old.format_help(), new.format_help())
        self.assertEqual(old.parse_args([]), new.parse_args([]))

    def test_native_arguments_defaults_and_cross_provider_rejection(self):
        cases = {
            "claude": (["--source", "synthetic.jsonl"], {"source"}),
            "kimi": (["--source", "synthetic-session"], {"source"}),
            "zcode": (["--session-id", "synthetic-id"], {"database", "session_id"}),
        }
        common = {"help", "output_dir", "stdout_receipt", "quiet"}
        for key, (args, native) in cases.items():
            with self.subTest(key=key):
                parser = registry.PROVIDER_REGISTRY[key].build_parser(cli.DEFAULT_OUTPUT_DIR)
                self.assertEqual({a.dest for a in parser._actions}, common | native)
                options = parser.parse_args(args)
                self.assertEqual(options.output_dir, cli.DEFAULT_OUTPUT_DIR)
                self.assertFalse(options.quiet)
                self.assertFalse(options.stdout_receipt)
                with captured():
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(args + ["--list-candidates"])
                self.assertEqual(error.exception.code, 2)
                with captured():
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args([])
                self.assertEqual(error.exception.code, 2)
        parser = registry.PROVIDER_REGISTRY["zcode"].build_parser("out")
        self.assertEqual(parser.parse_args(["--session-id", "synthetic"]).database,
                         "~/.zcode/cli/db/db.sqlite")

    def test_codex_candidates_short_circuit_even_when_empty_and_scrub_privacy(self):
        home = Path("/synthetic/home")
        cases = ([], [{"nested": {"path": str(home / "Downloads" / "synthetic.txt")},
                       "state": str(home / ".codex" / "synthetic")}])
        for rows in cases:
            with self.subTest(rows=rows):
                with mock.patch.object(Path, "home", return_value=home), \
                        mock.patch.object(exporter, "list_candidates", return_value=rows) as listing, \
                        mock.patch.object(exporter, "run_export") as parse, \
                        mock.patch.object(cli, "_write_spec") as publish:
                    with captured() as (out, err):
                        code = cli.main(["export", "codex", "--list-candidates",
                                         "--workspace", "synthetic", "--since-hours", "1"])
                self.assertEqual(code, 0)
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(json.loads(out.getvalue())["candidate_count"], len(rows))
                listing.assert_called_once_with("synthetic", 1)
                parse.assert_not_called()
                publish.assert_not_called()
                self.assertNotIn(str(home), out.getvalue())
                if rows:
                    self.assertIn("$ATTACHMENT", out.getvalue())
                    self.assertIn("$CODEX_STATE", out.getvalue())

    def test_codex_legacy_zip_rejection_precedes_candidates(self):
        with mock.patch.object(exporter, "list_candidates") as listing:
            with captured() as (out, err):
                with self.assertRaises(SystemExit) as error:
                    cli.main(["export", "codex", "--list-candidates",
                              "--build-handoff-zip", "synthetic-package"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("legacy package option", err.getvalue())
        listing.assert_not_called()

    def test_codex_blocked_selection_still_scrubs_private_detail(self):
        home = Path("/synthetic/home")
        blocked = exporter.ExportBlocked("BLOCKED_SELECTION", str(home / "Downloads" / "missing"))
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(exporter, "run_export", side_effect=blocked), \
                mock.patch.object(cli, "_write_spec") as publish:
            with captured() as (out, err):
                code = cli.main(["export", "codex", "--session-id", "synthetic"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(err.getvalue())["export_status"], "BLOCKED_SELECTION")
        self.assertIn("$ATTACHMENT", err.getvalue())
        self.assertNotIn(str(home), err.getvalue())
        publish.assert_not_called()

    def _synthetic_adapter(self, calls, stable=True):
        def arguments(parser):
            parser.add_argument("--native-key", required=True)

        def prepare(options, parser):
            calls.append("prepare")

        def candidates(options):
            calls.append("candidates")
            return None

        def parse(options):
            calls.append(options.native_key)
            return ("native-result", options.native_key)

        def build(source):
            self.assertEqual(source, ("native-result", "synthetic"))
            calls.append("spec")
            return V3PackageSpec(
                "synthetic-new", "synthetic-adapter", "1", "NON_COMPLETE",
                stable, "synthetic-native", "a" * 64, "# Synthetic\n", {},
            )

        return registry.ProviderAdapter(
            "synthetic-new", "Synthetic", "Synthetic provider", "--native-key synthetic",
            arguments, parse, build, prepare_options=prepare, candidate_listing=candidates,
        )

    def _dispatch(self, adapter, output):
        with mock.patch.object(cli, "PROVIDER_REGISTRY", {adapter.key: adapter}):
            with captured() as (out, err):
                code = cli.main(["export", adapter.key.upper(), "--native-key", "synthetic",
                                 "--output-dir", str(output)])
        return code, out.getvalue(), err.getvalue()

    def test_native_result_dispatches_without_a_shared_cli_provider_branch(self):
        calls = []
        adapter = self._synthetic_adapter(calls)
        with tempfile.TemporaryDirectory() as temp:
            code, out, err = self._dispatch(adapter, Path(temp) / "exports")
            self.assertEqual(code, 0, err)
            package = Path(json.loads(out)["package"])
            self.assertEqual(verify_v3_package(package)["verdict"], "PASS")
            self.assertEqual(json.loads(out)["provider"], adapter.key)
        self.assertEqual(calls, ["prepare", "candidates", "synthetic", "spec"])

    def test_optional_hooks_can_be_absent(self):
        calls = []
        adapter = replace(self._synthetic_adapter(calls),
                          prepare_options=None, candidate_listing=None)
        with tempfile.TemporaryDirectory() as temp:
            code, out, err = self._dispatch(adapter, Path(temp) / "exports")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, ["synthetic", "spec"])

    def test_unstable_source_blocks_before_any_package_publication(self):
        calls = []
        adapter = self._synthetic_adapter(calls, stable=False)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "exports"
            code, out, err = self._dispatch(adapter, output)
            self.assertFalse(output.exists())
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["export_status"], "BLOCKED_SOURCE_UNSTABLE")

    def test_provider_blocked_diagnostic_does_not_publish(self):
        adapter = replace(self._synthetic_adapter([]), select_and_parse=mock.Mock(
            side_effect=registry.ProviderExportBlocked("BLOCKED_SELECTION", "fixed safe detail")))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "exports"
            code, out, err = self._dispatch(adapter, output)
            self.assertFalse(output.exists())
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err), {
            "export_status": "BLOCKED_SELECTION", "detail": "fixed safe detail",
        })

    def test_unexpected_native_error_is_not_swallowed(self):
        adapter = replace(self._synthetic_adapter([]), select_and_parse=mock.Mock(
            side_effect=ValueError("synthetic unsupported source")))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "synthetic unsupported source"):
                self._dispatch(adapter, Path(temp) / "exports")


if __name__ == "__main__":
    unittest.main()
