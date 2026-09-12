"""The three shipped examples are regression evidence, not decoration.

`examples/pass`, `examples/fail` and `examples/unverifiable` are the only
user-facing demonstration of the three-state contract, so their verdicts and
exit codes are asserted here. If an example ever stops producing the code its
own README documents, this fails.

These fixtures are also checked for what they must never contain: a real
Codex session, a real home directory, or anything but synthetic content.
"""

import io
import json
import contextlib
import unittest
from pathlib import Path

from codex_preserve import cli, exporter, verify

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# (directory, expected verdict, expected exit code, expected reason code)
CASES = (
    ("pass", verify.VERDICT_PASS, verify.EXIT_PASS, None),
    ("fail", verify.VERDICT_FAIL, verify.EXIT_FAIL, "member_sha256_mismatch"),
    ("unverifiable", verify.VERDICT_UNVERIFIABLE, verify.EXIT_UNVERIFIABLE,
     "package_schema_unsupported"),
)


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class ShippedExamples(unittest.TestCase):
    def test_every_documented_example_directory_exists(self):
        for name, _verdict, _code, _reason in CASES:
            self.assertTrue((EXAMPLES / name).is_dir(),
                            "missing examples/%s" % name)

    def test_examples_produce_the_documented_exit_codes(self):
        for name, expected_verdict, expected_exit, expected_reason in CASES:
            with self.subTest(example=name):
                with captured() as (out, err):
                    code = cli.main(["verify", str(EXAMPLES / name)])
                self.assertEqual(code, expected_exit)
                text = out.getvalue() + err.getvalue()
                self.assertIn(expected_verdict, text)
                if expected_reason is not None:
                    self.assertIn(expected_reason, text)

    def test_json_receipt_agrees_with_the_exit_code(self):
        for name, expected_verdict, expected_exit, _reason in CASES:
            with self.subTest(example=name):
                with captured() as (out, err):
                    code = cli.main(["verify", str(EXAMPLES / name), "--json"])
                receipt = json.loads(out.getvalue() or err.getvalue())
                self.assertEqual(receipt["verdict"], expected_verdict)
                self.assertEqual(receipt["exit_code"], expected_exit)
                self.assertEqual(code, expected_exit)

    def test_pass_and_unverifiable_share_identical_payload_bytes(self):
        """UNVERIFIABLE must be an unreadable manifest, not a damaged payload.

        If this ever diverges, the example would be demonstrating the wrong
        thing: the third state exists for packages nothing bad is known about.
        """
        manifest = exporter.PACKAGE_MANIFEST_FILENAME
        for member in sorted((EXAMPLES / "pass").rglob("*")):
            if not member.is_file():
                continue
            relative = member.relative_to(EXAMPLES / "pass")
            other = EXAMPLES / "unverifiable" / relative
            self.assertTrue(other.is_file(), "missing %s" % relative)
            if relative.as_posix() == manifest:
                continue
            self.assertEqual(member.read_bytes(), other.read_bytes(),
                             "payload drift in %s" % relative)

    def test_fail_differs_from_pass_only_in_one_attested_payload(self):
        conversation = exporter.CONVERSATION_FILENAME
        differing = []
        for member in sorted((EXAMPLES / "pass").rglob("*")):
            if not member.is_file():
                continue
            relative = member.relative_to(EXAMPLES / "pass")
            other = EXAMPLES / "fail" / relative
            self.assertTrue(other.is_file(), "missing %s" % relative)
            if member.read_bytes() != other.read_bytes():
                differing.append(relative.as_posix())
        self.assertEqual(differing, [conversation])
        # Same length: the alteration is invisible to a directory listing and
        # is caught by the hash, not by the size.
        self.assertEqual(
            (EXAMPLES / "pass" / conversation).stat().st_size,
            (EXAMPLES / "fail" / conversation).stat().st_size)

    def test_examples_carry_no_real_session_or_home_coordinate(self):
        """The fixtures must stay synthetic and self-evidently so."""
        synthetic_session = "01a00000-1111-2222-3333-444444444444"
        for name, _verdict, _code, _reason in CASES:
            seen_synthetic_session = False
            for member in sorted((EXAMPLES / name).rglob("*")):
                if not member.is_file():
                    continue
                with self.subTest(path=str(member)):
                    # A rollout or session index is raw Codex session data and
                    # must never be shipped, whatever it contains.
                    self.assertFalse(member.name.startswith("rollout-"))
                    self.assertNotEqual(member.name, "session_index.jsonl")
                    text = member.read_text("utf-8")
                    # Export normalizes home paths; an absolute one here would
                    # mean a real machine coordinate got baked into a fixture.
                    self.assertNotIn("/Users/", text)
                    if synthetic_session in text:
                        seen_synthetic_session = True
            self.assertTrue(seen_synthetic_session,
                            "examples/%s no longer carries the synthetic "
                            "session id" % name)


if __name__ == "__main__":
    unittest.main()
