"""Proof tests for the codex-preserve CLI surface and the verify contract.

Every fixture is synthetic. These tests additionally assert the two boundaries
the product claims: no real Codex session directory is read, and no network or
model call is required for export or verify.
"""

import builtins
import contextlib
import errno
import io as _io
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from codex_preserve import __version__, cli, exporter, verify

from tests.test_codex_conversation_export import (
    ExporterTestCase,
    RolloutBuilder,
    minimal_session,
)


@contextlib.contextmanager
def recorded_reads():
    """Record every path opened through pathlib/io during the block."""
    opened = []
    real_open = _io.open

    def spy(file, *args, **kwargs):
        try:
            opened.append(os.fspath(file))
        except TypeError:
            opened.append(repr(file))
        return real_open(file, *args, **kwargs)

    with mock.patch("io.open", spy):
        yield opened


@contextlib.contextmanager
def no_outbound_calls():
    """Fail loudly if anything tries to open a socket or spawn a process."""

    def forbidden_socket(*args, **kwargs):
        raise AssertionError("a socket was opened")

    def forbidden_run(*args, **kwargs):
        raise AssertionError("a subprocess was spawned")

    with mock.patch.object(socket, "socket", forbidden_socket), \
            mock.patch.object(subprocess, "run", forbidden_run), \
            mock.patch.object(subprocess, "Popen", forbidden_run):
        yield


@contextlib.contextmanager
def captured():
    out, err = _io.StringIO(), _io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class CliSurface(unittest.TestCase):
    """2 — the installed console entry point works and names itself."""

    def test_the_package_and_entry_point_import(self):
        # 1 — candidate package/import path works.
        self.assertTrue(__version__)
        self.assertTrue(callable(cli.main))
        self.assertEqual(exporter.PACKAGE_SCHEMA_VERSION, "2.2")

    def test_help_exits_zero_and_disambiguates_from_codex_archive(self):
        with captured() as (out, _err):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("codex-preserve", text)
        self.assertIn("verify", text)
        self.assertIn("not `codex archive`", text)
        self.assertIn("not affiliated", text)

    def test_no_argument_invocation_prints_the_same_usage(self):
        with captured() as (out, _err):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("codex-preserve", out.getvalue())

    def test_version_reports_package_and_schema(self):
        with captured() as (out, _err):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertIn(__version__, out.getvalue())
        self.assertIn(exporter.PACKAGE_SCHEMA_VERSION, out.getvalue())

    def test_archive_is_refused_rather_than_aliased(self):
        for verb in ("archive", "unarchive"):
            with captured() as (out, err):
                code = cli.main([verb, "whatever"])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("no `%s` command" % verb, err.getvalue())

    def test_export_help_reaches_the_exporter_parser(self):
        with captured() as (out, _err):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["export", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--rollout", out.getvalue())
        self.assertIn("codex-preserve export", out.getvalue())


class VerifyContract(ExporterTestCase):
    def _package(self, builder=None):
        """3 — one synthetic export, returning its package directory."""
        rollout = (builder or minimal_session()).write(self.sessions)
        markdown, receipt = self.export(rollout)
        self.assertTrue(markdown)
        self.assertEqual(receipt["export_status"], exporter.STATUS_COMPLETE)
        packages = self.package_dirs()
        self.assertEqual(len(packages), 1)
        return packages[0]

    def _verify(self, argument, json_mode=False):
        argv = ["verify", str(argument)] + (["--json"] if json_mode else [])
        with captured() as (out, err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    # -- 3 -----------------------------------------------------------------

    def test_export_produces_the_human_file_receipt_and_manifest(self):
        package = self._package()
        for member in (exporter.CONVERSATION_FILENAME,
                       exporter.RECEIPT_FILENAME,
                       exporter.PACKAGE_MANIFEST_FILENAME):
            self.assertTrue((package / member).is_file(), member)
        manifest = json.loads(
            (package / exporter.PACKAGE_MANIFEST_FILENAME).read_text("utf-8"))
        self.assertEqual(manifest["package_schema_version"], "2.2")
        attested = {row["path"] for row in manifest["conversation_files"]}
        self.assertIn(exporter.CONVERSATION_FILENAME, attested)
        self.assertIn(exporter.RECEIPT_FILENAME, attested)

    # -- 4 -----------------------------------------------------------------

    def test_intact_package_verifies_and_exits_zero(self):
        package = self._package()
        code, out, err = self._verify(package)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("PASS", out)
        self.assertIn("verified", out)

    def test_the_json_receipt_states_what_it_does_and_does_not_attest(self):
        package = self._package()
        code, out, _err = self._verify(package, json_mode=True)
        self.assertEqual(code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["reasons"], [])
        self.assertGreaterEqual(receipt["members_attested"], 3)
        self.assertEqual(receipt["members_verified"],
                         receipt["members_attested"])
        self.assertIn("not a cryptographic signature",
                      receipt["attestation_semantics"])
        self.assertNotIn(str(Path.home()), out)

    # -- 5 -----------------------------------------------------------------

    def test_a_removed_attested_member_fails_nonzero(self):
        package = self._package()
        (package / exporter.CONVERSATION_FILENAME).unlink()
        code, out, err = self._verify(package)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("FAIL", err)
        self.assertIn("member_missing", err)

    def test_an_altered_attested_member_fails_nonzero(self):
        package = self._package()
        target = package / exporter.CONVERSATION_FILENAME
        payload = target.read_bytes()
        target.write_bytes(payload.replace(b"Audit", b"AUDIT", 1)
                           if b"Audit" in payload else payload + b"x")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", err)
        self.assertTrue("member_sha256_mismatch" in err
                        or "member_size_mismatch" in err)

    def test_an_altered_receipt_member_fails_nonzero(self):
        package = self._package()
        target = package / exporter.RECEIPT_FILENAME
        target.write_text(target.read_text("utf-8") + "\n", encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("member_size_mismatch", err)

    def test_a_tampered_derived_transfer_zip_fails_nonzero(self):
        package = self._package()
        with captured():
            self.assertEqual(
                exporter.main(["--build-handoff-zip", str(package)]), 0)
        code, _out, _err = self._verify(package)
        self.assertEqual(code, 0)

        zip_path = package / exporter.HANDOFF_ZIP_FILENAME
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path, "r") as archive:
            self.assertIn(exporter.PACKAGE_MANIFEST_FILENAME,
                          archive.namelist())
        zip_path.write_bytes(zip_path.read_bytes() + b"\x00")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("handoff_zip_mismatch", err)

    # -- 6 -----------------------------------------------------------------

    def test_a_directory_without_a_manifest_fails_closed(self):
        empty = self.root / "not-a-package"
        empty.mkdir()
        code, out, err = self._verify(empty)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("manifest_missing", err)

    def test_an_unparsable_manifest_fails_closed(self):
        package = self._package()
        (package / exporter.PACKAGE_MANIFEST_FILENAME).write_text(
            "{not json", encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("manifest_unreadable", err)

    def test_a_manifest_that_is_not_an_object_fails_closed(self):
        package = self._package()
        (package / exporter.PACKAGE_MANIFEST_FILENAME).write_text(
            "[1, 2, 3]", encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("manifest_not_an_object", err)

    def test_a_manifest_that_attests_nothing_fails_closed(self):
        package = self._package()
        (package / exporter.PACKAGE_MANIFEST_FILENAME).write_text(
            json.dumps({"package_schema_version": "2.2"}), encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("manifest_attests_nothing", err)

    def test_an_unsafe_member_path_fails_closed_rather_than_escaping(self):
        package = self._package()
        manifest_path = package / exporter.PACKAGE_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["conversation_files"][0]["path"] = "../escaped.md"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("unsafe_member_path", err)

    # -- FR-01: malformed manifest shapes are UNVERIFIABLE, never a traceback

    def _rewrite_manifest(self, package, mutate):
        path = package / exporter.PACKAGE_MANIFEST_FILENAME
        manifest = json.loads(path.read_text("utf-8"))
        mutate(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return package

    def test_a_non_object_row_in_each_collection_fails_closed(self):
        """The reused primitive calls row.get() on raw elements.

        It does not catch AttributeError, so a non-object row would escape as
        a traceback and exit 1. Nothing about the payload has been shown to be
        wrong, so the right answer is UNVERIFIABLE.
        """
        for collection in verify.ROW_COLLECTIONS:
            package = self._package()
            self._rewrite_manifest(
                package,
                lambda m, c=collection: m.__setitem__(
                    c, list(m.get(c) or []) + ["oops"]))
            code, out, err = self._verify(package)
            self.assertEqual(code, 2, collection)
            self.assertEqual(out, "", collection)
            self.assertIn("UNVERIFIABLE", err, collection)
            self.assertIn("manifest_row_not_an_object", err, collection)
            self.assertNotIn("Traceback", err, collection)
            shutil.rmtree(self.output)

    def test_a_malformed_row_still_emits_valid_json(self):
        package = self._package()
        self._rewrite_manifest(
            package, lambda m: m.__setitem__("artifacts", [42]))
        code, out, err = self._verify(package, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        receipt = json.loads(err)
        self.assertEqual(receipt["verdict"], "UNVERIFIABLE")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(
            [row["code"] for row in receipt["reasons"]],
            ["manifest_row_not_an_object"])

    def test_a_collection_that_is_not_an_array_fails_closed(self):
        """as_list() silently drops a non-array collection.

        Skipping members the manifest meant to attest must not read as PASS.
        """
        package = self._package()
        self._rewrite_manifest(
            package, lambda m: m.__setitem__("attachments", {"a": 1}))
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("manifest_collection_not_an_array", err)

    def test_every_malformed_row_is_reported_not_just_the_first(self):
        package = self._package()
        self._rewrite_manifest(package, lambda m: (
            m.__setitem__("attachments", ["a", "b"]),
            m.__setitem__("artifacts", [None]),
        ))
        code, _out, err = self._verify(package, json_mode=True)
        self.assertEqual(code, 2)
        codes = [row["code"] for row in json.loads(err)["reasons"]]
        self.assertEqual(codes, ["manifest_row_not_an_object"] * 3)

    def test_a_malformed_row_never_reaches_the_reused_primitive(self):
        """The primitive stays untouched; it is simply not handed bad input."""
        package = self._package()
        self._rewrite_manifest(
            package, lambda m: m.__setitem__("artifacts", ["oops"]))
        with mock.patch.object(
                exporter, "package_files_current",
                side_effect=AssertionError("primitive must not be called")):
            receipt = verify.verify_package(package)
        self.assertEqual(receipt["verdict"], verify.VERDICT_UNVERIFIABLE)

    def test_a_well_formed_manifest_still_defers_to_the_primitive(self):
        """Verdict authority for valid input is unchanged."""
        package = self._package()
        with mock.patch.object(exporter, "package_files_current",
                               return_value=False) as primitive:
            receipt = verify.verify_package(package)
        self.assertTrue(primitive.called)
        self.assertEqual(receipt["verdict"], verify.VERDICT_FAIL)
        self.assertEqual(receipt["exit_code"], 1)

    def test_an_unexpected_primitive_error_fails_closed(self):
        package = self._package()
        with mock.patch.object(exporter, "package_files_current",
                               side_effect=RuntimeError("boom")):
            receipt = verify.verify_package(package)
        self.assertEqual(receipt["verdict"], verify.VERDICT_UNVERIFIABLE)
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual([row["code"] for row in receipt["reasons"]],
                         ["integrity_primitive_error"])

    # -- R2-01: an unqueryable member is UNVERIFIABLE, never a traceback -----

    def test_a_member_type_query_permission_error_fails_closed(self):
        """Path.is_file() re-raises OSError other than ENOENT/ENOTDIR-style.

        An unqueryable member is proven neither missing nor altered, so the
        verdict must be UNVERIFIABLE / exit 2, never a traceback and never
        FAIL.
        """
        package = self._package()
        with mock.patch.object(Path, "is_file",
                               side_effect=PermissionError(13, "denied")):
            code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("member_state_unverifiable", err)
        self.assertNotIn("Traceback", err)

    def test_a_member_type_query_overlong_path_error_fails_closed(self):
        """ENAMETOOLONG-style failures take the same fail-closed path."""
        package = self._package()
        with mock.patch.object(
                Path, "is_file",
                side_effect=OSError(errno.ENAMETOOLONG, "name too long")):
            code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("member_state_unverifiable", err)
        self.assertNotIn("Traceback", err)

    def test_a_member_type_query_error_still_emits_valid_json(self):
        package = self._package()
        with mock.patch.object(Path, "is_file",
                               side_effect=PermissionError(13, "denied")):
            code, out, err = self._verify(package, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        receipt = json.loads(err)
        self.assertEqual(receipt["verdict"], "UNVERIFIABLE")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIn("member_state_unverifiable",
                      [row["code"] for row in receipt["reasons"]])

    def test_an_unrelated_programming_error_is_not_swallowed(self):
        """The guard is a precise OSError guard, not a blanket suppressor."""
        package = self._package()
        with mock.patch.object(Path, "is_file",
                               side_effect=TypeError("a real bug")):
            with self.assertRaises(TypeError):
                verify.verify_package(package)

    # -- K3R-01: an unreadable member is unknown, not proven bad -------------

    class _BrokenStat:
        """is_file() sees a regular file; reading st_size raises OSError.

        ``Path.is_file`` resolves through ``Path.stat`` internally, so a mock
        that raises in ``stat`` itself would exercise the ``is_file`` guard
        instead of the size-read guard under test.
        """

        st_mode = 0o100644  # S_IFREG | 0644

        @property
        def st_size(self):
            raise PermissionError(13, "denied")

    def test_a_member_stat_error_is_unverifiable(self):
        """A stat() OSError means the member's state cannot be determined:
        UNVERIFIABLE / exit 2, never FAIL and never a traceback."""
        package = self._package()
        real_stat = Path.stat
        broken = self._BrokenStat()

        def flaky_stat(self, *args, **kwargs):
            if self.name == exporter.CONVERSATION_FILENAME:
                return broken
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", flaky_stat):
            code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("member_unreadable", err)
        self.assertNotIn("Traceback", err)

    def test_a_member_hash_permission_error_is_unverifiable(self):
        """An open/hash PermissionError is the same cannot-determine class."""
        package = self._package()
        with mock.patch.object(exporter, "sha256_file",
                               side_effect=PermissionError(13, "denied")):
            code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("member_unreadable", err)
        self.assertNotIn("Traceback", err)

    def test_a_member_hash_error_still_emits_valid_json(self):
        package = self._package()
        with mock.patch.object(exporter, "sha256_file",
                               side_effect=PermissionError(13, "denied")):
            code, out, err = self._verify(package, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        receipt = json.loads(err)
        self.assertEqual(receipt["verdict"], "UNVERIFIABLE")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIn("member_unreadable",
                      [row["code"] for row in receipt["reasons"]])

    # -- K3R-02: determinate evidence outranks a simultaneous unknown --------

    def _is_file_denying(self, denied_name):
        real_is_file = Path.is_file

        def flaky(self, *args, **kwargs):
            if self.name == denied_name:
                raise PermissionError(13, "denied")
            return real_is_file(self, *args, **kwargs)

        return flaky

    def test_a_tampered_plus_unqueryable_member_stays_fail(self):
        """One proven-altered member makes the package FAIL even while
        another member cannot be queried at all."""
        package = self._package()
        target = package / exporter.CONVERSATION_FILENAME
        target.write_bytes(target.read_bytes() + b"x")
        with mock.patch.object(
                Path, "is_file",
                self._is_file_denying(exporter.RECEIPT_FILENAME)):
            code, out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_size_mismatch", err)
        self.assertIn("member_state_unverifiable", err)

    def test_a_missing_plus_unqueryable_member_stays_fail(self):
        """One proven-missing member makes the package FAIL even while
        another member cannot be queried at all."""
        package = self._package()
        (package / exporter.CONVERSATION_FILENAME).unlink()
        with mock.patch.object(
                Path, "is_file",
                self._is_file_denying(exporter.RECEIPT_FILENAME)):
            code, out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_missing", err)
        self.assertIn("member_state_unverifiable", err)

    def test_determinate_failure_plus_primitive_error_stays_fail(self):
        """A primitive error is unknown evidence; it must not downgrade a
        determinate member failure to UNVERIFIABLE."""
        package = self._package()
        target = package / exporter.CONVERSATION_FILENAME
        target.write_bytes(target.read_bytes() + b"x")
        with mock.patch.object(exporter, "package_files_current",
                               side_effect=RuntimeError("boom")):
            code, out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_size_mismatch", err)
        self.assertIn("integrity_primitive_error", err)

    # Controls: intact -> PASS (test_intact_package_verifies_and_exits_zero);
    # unknown-only -> UNVERIFIABLE (the R2-01 / K3R-01 tests above).

    # -- CP-SYMLINK-01: symlinked members are never followed ----------------

    def _symlinked_member(self, package, target):
        """Replace the conversation member with a symlink to ``target``."""
        member = package / exporter.CONVERSATION_FILENAME
        member.unlink()
        member.symlink_to(target)
        return member

    def test_a_leaf_symlink_to_an_outside_target_is_rejected(self):
        """The coordinator's reproduction: a symlinked member whose target
        bytes match the attestation must not PASS."""
        package = self._package()
        member = package / exporter.CONVERSATION_FILENAME
        outside = self.root / "outside.txt"
        outside.write_bytes(member.read_bytes())
        self._symlinked_member(package, outside)
        code, out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_symlink_not_allowed", err)

    def test_a_parent_directory_symlink_is_rejected(self):
        """A hand-built package whose member's parent directory is a symlink
        to an outside directory: FAIL, target never read."""
        package = self.root / "pkg-parent-link"
        outside = self.root / "outside-dir"
        outside.mkdir()
        payload = b"synthetic member bytes\n"
        (outside / "member.txt").write_bytes(payload)
        package.mkdir()
        (package / "sub").symlink_to(outside)
        manifest = {
            "package_schema_version": exporter.PACKAGE_SCHEMA_VERSION,
            "conversation_files": [{
                "path": "sub/member.txt",
                "bytes": len(payload),
                "sha256": exporter.sha256_file(outside / "member.txt"),
            }],
        }
        (package / exporter.PACKAGE_MANIFEST_FILENAME).write_text(
            json.dumps(manifest), encoding="utf-8")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("member_symlink_not_allowed", err)

    def test_a_symlink_into_the_package_is_still_rejected(self):
        """Containment is not a target check: even an in-package alias is
        rejected, because a canonical member is a real regular file."""
        package = self._package()
        member = package / exporter.CONVERSATION_FILENAME
        alias = package / "alias-copy"
        alias.write_bytes(member.read_bytes())
        self._symlinked_member(package, alias.name)
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("member_symlink_not_allowed", err)

    def test_a_broken_symlink_member_is_rejected(self):
        package = self._package()
        self._symlinked_member(package, "no-such-target")
        code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("member_symlink_not_allowed", err)
        self.assertNotIn("member_missing", err)

    def test_a_symlink_check_oserror_is_unverifiable(self):
        """If the containment check itself cannot be completed, nothing is
        proven: UNVERIFIABLE / exit 2, no traceback."""
        package = self._package()
        with mock.patch.object(Path, "is_symlink",
                               side_effect=PermissionError(13, "denied")):
            code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("member_containment_unverifiable", err)
        self.assertNotIn("Traceback", err)

    def test_a_symlink_finding_outranks_a_simultaneous_unknown(self):
        """Determinate symlink invalidity keeps FAIL even when another
        member's read state is unknown."""
        package = self._package()
        self._symlinked_member(package, "no-such-target")
        with mock.patch.object(exporter, "sha256_file",
                               side_effect=PermissionError(13, "denied")):
            code, _out, err = self._verify(package)
        self.assertEqual(code, 1)
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_symlink_not_allowed", err)

    def test_the_primitive_is_never_called_for_a_symlinked_member(self):
        """Fail closed before the reused primitive could follow the link."""
        package = self._package()
        member = package / exporter.CONVERSATION_FILENAME
        outside = self.root / "outside.txt"
        outside.write_bytes(member.read_bytes())
        self._symlinked_member(package, outside)
        with mock.patch.object(
                exporter, "package_files_current",
                side_effect=AssertionError("primitive must not be called")):
            receipt = verify.verify_package(package)
        self.assertEqual(receipt["verdict"], verify.VERDICT_FAIL)
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIn("member_symlink_not_allowed",
                      [row["code"] for row in receipt["reasons"]])

    def test_a_symlink_receipt_json_is_valid_and_leaks_no_target(self):
        package = self._package()
        member = package / exporter.CONVERSATION_FILENAME
        outside = self.root / "outside.txt"
        outside.write_bytes(member.read_bytes())
        self._symlinked_member(package, outside)
        code, out, err = self._verify(package, json_mode=True)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        receipt = json.loads(err)
        self.assertEqual(receipt["verdict"], "FAIL")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertNotIn(str(outside), err)

    # -- FR-10: an unreadable attested-current transfer ZIP is unknown ------

    def _package_with_current_zip(self):
        """A synthetic package whose current derived transfer ZIP is built."""
        package = self._package()
        with captured():
            self.assertEqual(
                exporter.main(["--build-handoff-zip", str(package)]), 0)
        self.assertTrue(
            (package / exporter.HANDOFF_ZIP_FILENAME).is_file())
        return package

    def _chmod_zero_blocks_reads(self, zip_path):
        """chmod the ZIP 000; return whether that really blocks real I/O."""
        zip_path.chmod(0)
        try:
            with open(zip_path, "rb"):
                return False
        except OSError:
            return True

    def test_an_unreadable_attested_current_zip_is_unverifiable(self):
        """A permission-blocked ZIP is cannot-determine, not proven bad:
        UNVERIFIABLE / exit 2, never handoff_zip_mismatch FAIL."""
        package = self._package_with_current_zip()
        zip_path = package / exporter.HANDOFF_ZIP_FILENAME
        try:
            if not self._chmod_zero_blocks_reads(zip_path):
                self.skipTest("chmod 0 does not block reads for this user")
            code, out, err = self._verify(package)
        finally:
            zip_path.chmod(0o644)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: UNVERIFIABLE", err)
        self.assertIn("member_unreadable", err)
        self.assertNotIn("handoff_zip_mismatch", err)
        self.assertNotIn("Traceback", err)

    def test_an_unreadable_zip_json_receipt_is_valid_and_unverifiable(self):
        package = self._package_with_current_zip()
        zip_path = package / exporter.HANDOFF_ZIP_FILENAME
        try:
            if not self._chmod_zero_blocks_reads(zip_path):
                self.skipTest("chmod 0 does not block reads for this user")
            code, out, err = self._verify(package, json_mode=True)
        finally:
            zip_path.chmod(0o644)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        receipt = json.loads(err)
        self.assertEqual(receipt["verdict"], "UNVERIFIABLE")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIn("member_unreadable",
                      [row["code"] for row in receipt["reasons"]])
        self.assertEqual(receipt["handoff_zip"]["verdict"], "UNVERIFIABLE")

    def test_an_unreadable_zip_plus_tampered_member_stays_fail(self):
        """A determinate tampered member outranks the ZIP's unknown state."""
        package = self._package_with_current_zip()
        target = package / exporter.CONVERSATION_FILENAME
        target.write_bytes(target.read_bytes() + b"x")
        zip_path = package / exporter.HANDOFF_ZIP_FILENAME
        try:
            if not self._chmod_zero_blocks_reads(zip_path):
                self.skipTest("chmod 0 does not block reads for this user")
            code, out, err = self._verify(package)
        finally:
            zip_path.chmod(0o644)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("codex-preserve verify: FAIL", err)
        self.assertIn("member_size_mismatch", err)
        self.assertIn("member_unreadable", err)

    def test_a_readable_valid_current_zip_still_passes(self):
        package = self._package_with_current_zip()
        code, out, err = self._verify(package)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("PASS", out)

    def test_the_zip_primitive_is_never_called_for_an_unreadable_zip(self):
        """Fail closed before the primitive's OSError surface could collapse
        the unknown into a determinate False."""
        package = self._package_with_current_zip()
        real_open = builtins.open

        def denying(file, *args, **kwargs):
            if Path(file).name == exporter.HANDOFF_ZIP_FILENAME:
                raise PermissionError(13, "denied")
            return real_open(file, *args, **kwargs)

        with mock.patch.object(exporter, "handoff_zip_current",
                               side_effect=AssertionError(
                                   "primitive must not be called")), \
                mock.patch.object(builtins, "open", denying):
            receipt = verify.verify_package(package)
        self.assertEqual(receipt["verdict"], verify.VERDICT_UNVERIFIABLE)
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIn("member_unreadable",
                      [row["code"] for row in receipt["reasons"]])

    # -- FR-04: an uninterpretable schema must not PASS ---------------------

    def test_the_supported_schema_set_comes_from_the_reused_core(self):
        self.assertEqual(exporter.RECOGNIZED_PACKAGE_SCHEMA_VERSIONS,
                         ("2.1", "2.2"))
        self.assertIn(exporter.PACKAGE_SCHEMA_VERSION,
                      exporter.RECOGNIZED_PACKAGE_SCHEMA_VERSIONS)

    def test_an_unknown_schema_fails_closed(self):
        package = self._package()
        self._rewrite_manifest(
            package,
            lambda m: m.__setitem__("package_schema_version", "99.0"))
        code, out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("UNVERIFIABLE", err)
        self.assertIn("package_schema_unsupported", err)

    def test_a_missing_schema_fails_closed(self):
        package = self._package()
        self._rewrite_manifest(
            package, lambda m: m.pop("package_schema_version", None))
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("package_schema_missing", err)

    def test_a_non_string_schema_fails_closed(self):
        package = self._package()
        self._rewrite_manifest(
            package, lambda m: m.__setitem__("package_schema_version", 2.2))
        code, _out, err = self._verify(package)
        self.assertEqual(code, 2)
        self.assertIn("package_schema_unsupported", err)

    def test_the_historical_schema_the_core_understands_is_accepted(self):
        """2.1 is recognized by the reused legacy path, so verify accepts it.

        Only the schema gate is under test here: the members are untouched, so
        the reused primitive still decides the verdict.
        """
        package = self._package()
        self._rewrite_manifest(
            package,
            lambda m: m.__setitem__("package_schema_version", "2.1"))
        receipt = verify.verify_package(package)
        self.assertNotIn(
            "package_schema_unsupported",
            [row["code"] for row in receipt["reasons"]])

    def test_a_missing_package_path_fails_closed(self):
        code, out, err = self._verify(self.root / "nothing-here")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("package_path_unavailable", err)

    def test_a_file_instead_of_a_package_directory_fails_closed(self):
        target = self.root / "a-file"
        target.write_text("x", encoding="utf-8")
        code, _out, err = self._verify(target)
        self.assertEqual(code, 2)
        self.assertIn("package_path_not_a_directory", err)

    # -- 8 and 9 -----------------------------------------------------------

    def test_no_real_codex_session_directory_is_read(self):
        real_home = Path.home()
        rollout = minimal_session().write(self.sessions)
        with recorded_reads() as opened:
            with captured():
                self.assertEqual(exporter.main([
                    "--rollout", str(rollout),
                    "--output-dir", str(self.output),
                    "--no-git-probe", "--quiet"]), 0)
        self.assertTrue(opened)
        for entry in opened:
            resolved = str(Path(entry))
            self.assertNotIn("/.codex/", resolved)
            self.assertFalse(
                resolved.startswith(str(real_home / ".codex")), resolved)
            self.assertFalse(
                resolved.startswith(str(real_home / "Desktop")), resolved)

    def test_the_declared_discovery_roots_are_never_the_real_ones(self):
        self.assertEqual(exporter.DISCOVERY_ROOTS,
                         ("~/.codex/sessions", "~/.codex/archived_sessions"))
        roots = [str(root) for root in exporter.discovery_roots()]
        self.assertEqual(roots, [str(self.sessions)])

    def test_export_and_verify_need_no_network_or_subprocess(self):
        rollout = minimal_session().write(self.sessions)
        with no_outbound_calls():
            with captured():
                self.assertEqual(exporter.main([
                    "--rollout", str(rollout),
                    "--output-dir", str(self.output),
                    "--no-git-probe", "--quiet"]), 0)
            package = self.package_dirs()[0]
            code, _out, _err = self._verify(package)
        self.assertEqual(code, 0)

    def test_the_export_core_imports_no_networking_module(self):
        source = Path(exporter.__file__).read_text("utf-8")
        for module in ("urllib.request", "http.client", "requests",
                       "socket", "ssl", "asyncio", "openai", "anthropic"):
            self.assertNotIn("import %s" % module, source, module)

    def test_the_rollout_is_never_mutated_by_verify(self):
        rollout = minimal_session().write(self.sessions)
        before = exporter.sha256_file(rollout)
        with captured():
            self.assertEqual(exporter.main([
                "--rollout", str(rollout), "--output-dir", str(self.output),
                "--no-git-probe", "--quiet"]), 0)
        self._verify(self.package_dirs()[0])
        self.assertEqual(exporter.sha256_file(rollout), before)


class VerifyRendering(ExporterTestCase):
    def test_the_human_reason_never_leaks_the_real_home_path(self):
        receipt = verify.verify_package(Path.home() / ".codex" / "no-such-pkg")
        self.assertEqual(receipt["verdict"], verify.VERDICT_UNVERIFIABLE)
        rendered = verify.render_human(receipt)
        self.assertNotIn(str(Path.home()), rendered)
        self.assertIn("package_path_unavailable", rendered)


if __name__ == "__main__":
    unittest.main()
