"""Schema-3.0 package assembly and integrity contract tests."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from codex_preserve import verify
from codex_preserve._v3_package import (
    ARTIFACT_INDEX_FILENAME,
    CONVERSATION_FILENAME,
    MANIFEST_FILENAME,
    RECEIPT_FILENAME,
    V3Member,
    V3PackageSpec,
    build_v3_materialization,
    verify_v3_package,
    write_v3_package,
)


def spec(extra_members=()):
    return V3PackageSpec(
        provider="synthetic-provider",
        adapter_name="synthetic-adapter",
        adapter_version="1",
        coverage_status="COMPLETE",
        source_stable=True,
        source_identity_scope="selected_persisted_source",
        source_identity_sha256="a" * 64,
        conversation_markdown="# Synthetic conversation\n\nHello.\n",
        provider_receipt={
            "synthetic": True,
            "ui_completeness_attested": False,
        },
        extra_members=tuple(extra_members),
    )


class V3PackageContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_materialization_is_deterministic_and_uses_frozen_names(self):
        first_members, first_manifest = build_v3_materialization(spec())
        second_members, second_manifest = build_v3_materialization(spec())
        self.assertEqual(first_members, second_members)
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(
            [member.path for member in first_members],
            [CONVERSATION_FILENAME, RECEIPT_FILENAME],
        )
        manifest = json.loads(first_manifest)
        self.assertEqual(manifest["package_schema_version"], "3.0")
        self.assertEqual(manifest["product"], "session-preserve")
        self.assertEqual(manifest["provider"], "synthetic-provider")
        self.assertEqual(manifest["transfer_artifact"]["path"],
                         "session-package.zip")

    def test_receipt_keeps_common_and_provider_specific_layers_separate(self):
        members, _ = build_v3_materialization(spec())
        receipt = json.loads(next(
            member.payload for member in members
            if member.path == RECEIPT_FILENAME
        ))
        self.assertEqual(receipt["provider"], "synthetic-provider")
        self.assertEqual(
            receipt["provider_receipt"]["ui_completeness_attested"], False)
        self.assertTrue(receipt["integrity_scope"]["manifest_relative"])
        self.assertFalse(receipt["integrity_scope"]["authenticity_attested"])

    def test_write_and_verify_minimal_package(self):
        package = self.root / "package"
        write_v3_package(package, spec())
        self.assertTrue((package / CONVERSATION_FILENAME).is_file())
        self.assertTrue((package / RECEIPT_FILENAME).is_file())
        self.assertTrue((package / MANIFEST_FILENAME).is_file())
        self.assertFalse((package / "attachments").exists())
        self.assertFalse((package / "artifacts").exists())
        result = verify_v3_package(package)
        self.assertEqual((result["verdict"], result["exit_code"]), ("PASS", 0))
        self.assertEqual((result["members_verified"],
                          result["members_attested"]), (2, 2))

    def test_optional_attachment_and_artifact_members_are_attested(self):
        extras = (
            V3Member("attachments/input.txt", "attachment", b"input\n"),
            V3Member("artifacts/result.txt", "artifact", b"result\n"),
            V3Member(ARTIFACT_INDEX_FILENAME, "artifact_index",
                     b"# Artifacts\n"),
        )
        package = self.root / "package"
        write_v3_package(package, spec(extras))
        result = verify_v3_package(package)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["members_attested"], 5)

    def test_public_verifier_dispatches_schema_3_without_changing_old_schema_code(self):
        package = self.root / "package"
        write_v3_package(package, spec())
        receipt = verify.verify_package(package)
        self.assertEqual((receipt["verdict"], receipt["exit_code"]), ("PASS", 0))
        self.assertEqual(receipt["package_schema_version"], "3.0")
        self.assertEqual(receipt["tool"], "session-preserve")
        human = verify.render_human(receipt)
        self.assertIn("session-preserve verify: PASS", human)
        self.assertIn("schema:  3.0 (expected 3.0)", human)

    def test_member_tamper_is_fail(self):
        package = self.root / "package"
        write_v3_package(package, spec())
        (package / CONVERSATION_FILENAME).write_text(
            "tampered\n", encoding="utf-8")
        result = verify_v3_package(package)
        self.assertEqual((result["verdict"], result["exit_code"]), ("FAIL", 1))
        self.assertIn("member_size_mismatch",
                      {row["code"] for row in result["reasons"]})

    def test_unattested_extra_file_is_fail(self):
        package = self.root / "package"
        write_v3_package(package, spec())
        (package / "extra.txt").write_text("extra", encoding="utf-8")
        result = verify_v3_package(package)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("unattested_member_present",
                      {row["code"] for row in result["reasons"]})

    def test_symlink_anywhere_is_fail(self):
        package = self.root / "package"
        write_v3_package(package, spec())
        (package / "link.txt").symlink_to(package / CONVERSATION_FILENAME)
        result = verify_v3_package(package)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("symlink_in_package",
                      {row["code"] for row in result["reasons"]})

    def test_invalid_extra_member_boundaries_fail_before_write(self):
        cases = (
            V3Member("../escape", "artifact", b"x"),
            V3Member("free-floating.txt", "artifact", b"x"),
            V3Member("artifacts/a.txt", "attachment", b"x"),
            V3Member(MANIFEST_FILENAME, "artifact", b"x"),
        )
        for member in cases:
            with self.subTest(member=member.path):
                with self.assertRaises(ValueError):
                    build_v3_materialization(spec((member,)))

    def test_unstable_source_cannot_attest_identity(self):
        bad = replace(spec(), source_stable=False)
        with self.assertRaises(ValueError):
            build_v3_materialization(bad)
        okay = replace(
            spec(),
            source_stable=False,
            source_identity_sha256=None,
            coverage_status="NON_COMPLETE",
        )
        members, manifest = build_v3_materialization(okay)
        self.assertTrue(members)
        self.assertIsNone(
            json.loads(manifest)["source_snapshot"]["identity_sha256"])

    def test_existing_target_is_never_overwritten(self):
        package = self.root / "package"
        package.mkdir()
        marker = package / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            write_v3_package(package, spec())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_manifest_member_rows_are_relative_and_hash_bound(self):
        members, manifest_bytes = build_v3_materialization(spec())
        manifest = json.loads(manifest_bytes)
        rows = {row["path"]: row for row in manifest["members"]}
        for member in members:
            self.assertIn(member.path, rows)
            self.assertFalse(member.path.startswith("/"))
            self.assertEqual(rows[member.path]["bytes"], len(member.payload))

    def test_manifest_does_not_create_provider_neutral_conversation_ontology(self):
        _, manifest_bytes = build_v3_materialization(spec())
        text = manifest_bytes.decode("utf-8")
        for forbidden in ("GenericConversation", "GenericTurn", "GenericTool"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
