"""7 — the tree passes its own public-hygiene scan.

The scan is also proved non-vacuous here: a planted violation of every rule
family must be detected, and ordinary public vocabulary must not be.

Two conventions in this file are deliberate:

- credential-shaped fixtures are assembled at run time from non-secret
  fragments rather than written out as static token-shaped literals, so this
  file never itself looks like a leaked credential to any other scanner;
- no rule here names a real account, repository, project, helper or machine.
  The scan is generic, and its tests have to stay generic too, or the tests
  would leak exactly what the scan exists to keep out.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

CANDIDATE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CANDIDATE_ROOT / "tools"))

import public_hygiene_scan as scanner  # noqa: E402


def synthetic_github_token():
    """A token-shaped value built at run time, never a source literal."""
    return "gh" + "p_" + ("AB" * 11)


def synthetic_home_path():
    """A home path that is not one of the scan's synthetic fixture names."""
    return "/Users/" + "realperson" + "/Desktop"


def synthetic_retry_reference():
    """A retry-round reference, assembled so this file is not itself one."""
    return "Retry" + " 3"


def synthetic_issue_reference():
    """An unqualified issue reference, likewise assembled at run time."""
    return "#" + "4821"


class CandidateTreeIsPublishable(unittest.TestCase):
    def test_the_whole_tree_scans_clean(self):
        findings = scanner.scan(CANDIDATE_ROOT)
        self.assertEqual(
            [item.render() for item in findings], [],
            "the tree must be safe to publish")

    def test_the_scan_is_deterministic(self):
        first = [item.key() for item in scanner.scan(CANDIDATE_ROOT)]
        second = [item.key() for item in scanner.scan(CANDIDATE_ROOT)]
        self.assertEqual(first, second)
        self.assertEqual(second, sorted(second))

    def test_the_rule_set_stays_generic(self):
        """The rules must be structural shapes, never a denylist of names.

        A hygiene tool shipped in a public repository cannot protect a
        private name by listing it: publishing the list publishes the name.
        So the rule set is pinned here. Adding a rule is fine — it just has
        to be a deliberate edit to this list, which is the moment to ask
        whether the new rule names something that must not be published.
        """
        self.assertEqual(
            [rule[0] for rule in scanner.FORBIDDEN_NAME_RULES],
            ["real_session_payload", "secret_bearing_file"])
        self.assertEqual(
            [rule[0] for rule in scanner.CONTENT_RULES],
            ["internal_retry_reference",
             "credential_pem_private_key",
             "credential_github_token",
             "credential_openai_key",
             "credential_slack_token",
             "credential_aws_access_key_id",
             "credential_google_api_key",
             "credential_jwt",
             "credential_cookie_header",
             "credential_pypi_token",
             "stored_index_credential_reference"])

    def test_rules_naming_a_specific_portfolio_do_not_come_back(self):
        """Named guards against the specific rules this tree used to ship."""
        codes = ({rule[0] for rule in scanner.CONTENT_RULES}
                 | {rule[0] for rule in scanner.FORBIDDEN_NAME_RULES})
        for retired in ("private_repo_coordinate", "private_project_name",
                        "private_mechanism_name", "private_ops_document",
                        "private_ops_directory",
                        "private_mechanism_instruction"):
            self.assertNotIn(
                retired, codes,
                "%s identifies a specific private account, repository, "
                "project or helper and must not ship in a public tree"
                % retired)


class ScanDetectsPlantedViolations(unittest.TestCase):
    """Every rule family must catch something, or it is decoration."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hygiene-scan-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _codes(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")
        return sorted({item.code for item in scanner.scan(self.root)})

    def test_a_real_home_path_is_detected(self):
        body = 'P = "%s"\n' % synthetic_home_path()
        self.assertIn("private_home_path", self._codes("a.py", body))

    def test_an_internal_retry_reference_is_detected(self):
        body = "carried over from %s\n" % synthetic_retry_reference()
        self.assertIn("internal_retry_reference", self._codes("a.md", body))

    def test_an_unqualified_issue_reference_is_detected(self):
        body = "as agreed in %s\n" % synthetic_issue_reference()
        self.assertIn("internal_issue_reference", self._codes("a.md", body))

    def test_a_credential_literal_is_detected(self):
        self.assertIn("credential_github_token",
                      self._codes("a.py", 'T = "%s"\n'
                                  % synthetic_github_token()))

    def test_a_pem_private_key_block_is_detected(self):
        body = "-----BEGIN " + "RSA PRIVATE KEY" + "-----\n"
        self.assertIn("credential_pem_private_key",
                      self._codes("a.txt", body))

    def test_a_pypi_token_literal_is_detected(self):
        body = "password: pypi-" + "AgE" + "IcHlwaS5vcmcCJDAwMDAwMDAw\n"
        self.assertIn("credential_pypi_token", self._codes("w.yml", body))

    def test_a_stored_index_credential_reference_is_detected(self):
        # Release auth is Trusted Publishing over OIDC. A workflow reaching
        # for a stored password is a regression, not a style choice.
        body = "  password: ${{ secrets." + "PYPI_API_TOKEN }}\n"
        self.assertIn("stored_index_credential_reference",
                      self._codes("w.yml", body))

    def test_a_twine_password_environment_variable_is_detected(self):
        body = "  TWINE_" + "PASSWORD: hunter2\n"
        self.assertIn("stored_index_credential_reference",
                      self._codes("w.yml", body))

    def test_a_marker_cannot_excuse_a_stored_index_credential(self):
        body = ("  password: ${{ secrets." + "PYPI_API_TOKEN }}  # "
                + scanner.MARKER + "\n")
        self.assertIn("stored_index_credential_reference",
                      self._codes("w.yml", body))

    def test_a_committed_rollout_file_is_detected(self):
        name = "rollout-" + "2026-01-01T00-00-00-x.jsonl"
        (self.root / name).write_text("{}\n", encoding="utf-8")
        codes = sorted({item.code for item in scanner.scan(self.root)})
        self.assertIn("real_session_payload", codes)

    def test_a_committed_session_index_is_detected(self):
        (self.root / ("session_index" + ".jsonl")).write_text(
            "{}\n", encoding="utf-8")
        codes = sorted({item.code for item in scanner.scan(self.root)})
        self.assertIn("real_session_payload", codes)

    def test_a_committed_key_file_is_detected(self):
        (self.root / ("server." + "pem")).write_text("x\n", encoding="utf-8")
        codes = sorted({item.code for item in scanner.scan(self.root)})
        self.assertIn("secret_bearing_file", codes)

    def test_a_symlink_is_detected(self):
        (self.root / "real.md").write_text("x\n", encoding="utf-8")
        (self.root / "link.md").symlink_to(self.root / "real.md")
        codes = sorted({item.code for item in scanner.scan(self.root)})
        self.assertIn("symlink_in_tree", codes)

    def test_a_same_line_marker_does_not_hide_a_real_home_path(self):
        """The marker on the offending line must not switch the whole line
        off; it exempts credential rules only."""
        body = ('X = "%s"  # %s\n' % (synthetic_home_path(), scanner.MARKER))
        self.assertIn("private_home_path", self._codes("a.py", body))

    def test_a_same_line_marker_does_not_hide_a_second_rule(self):
        """A different non-credential finding on the marker's own line is
        still reported."""
        body = 'N = "%s"  # %s\n' % (synthetic_retry_reference(),
                                      scanner.MARKER)
        self.assertIn("internal_retry_reference", self._codes("a.py", body))


class ScanStaysNarrowEnoughToPublish(unittest.TestCase):
    """The scan must not make ordinary public vocabulary unpublishable."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hygiene-scan-ok-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _clean(self, body):
        (self.root / "a.md").write_text(body, encoding="utf-8")
        return [item.render() for item in scanner.scan(self.root)]

    def test_ordinary_public_words_pass(self):
        self.assertEqual(self._clean(
            "Open an issue if a retry is needed. Codex archive is different.\n"
            "The owner of the repository governs releases.\n"), [])

    def test_a_synthetic_fixture_home_passes(self):
        self.assertEqual(
            self._clean('WORKSPACE = "/Users/testowner/Documents/demo"\n'), [])

    def test_a_repository_qualified_upstream_issue_passes(self):
        self.assertEqual(
            self._clean("upstream openai/codex#24289 asks the same\n"), [])

    def test_a_marked_synthetic_credential_fixture_passes(self):
        self.assertEqual(self._clean(
            "# %s\nTOKEN = \"%s\"\n"
            % (scanner.MARKER, synthetic_github_token())), [])

    def test_a_same_line_marker_still_exempts_a_synthetic_credential(self):
        """The marker on the offending line itself exempts credential rules."""
        self.assertEqual(self._clean(
            'TOKEN = "%s"  # %s\n'
            % (synthetic_github_token(), scanner.MARKER)), [])

    def test_the_marker_can_never_excuse_a_non_credential_finding(self):
        body = ("# %s\nPATH = \"%s\"\n"
                % (scanner.MARKER, synthetic_home_path()))
        (self.root / "a.md").write_text(body, encoding="utf-8")
        codes = sorted({item.code for item in scanner.scan(self.root)})
        self.assertIn("private_home_path", codes)


class RuleTableExemptionIsScopedToTheScanner(unittest.TestCase):
    """FR-05 — a scanned file must not be able to switch the gate off.

    The markers are assembled at run time: writing them into this file would
    itself be the misuse the tests below are proving is caught.
    """

    BEGIN = "hygiene-rule-" + "table: begin"
    END = "hygiene-rule-" + "table: end"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hygiene-region-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self, relative, secret_line):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# %s\n%s# %s\n" % (self.BEGIN, secret_line, self.END),
                        encoding="utf-8")
        return path

    def _findings(self):
        return scanner.scan(self.root)

    def test_a_planted_marker_does_not_disable_content_rules(self):
        self._write("evil.py", 'T = "%s"\n' % synthetic_github_token())
        codes = sorted({item.code for item in self._findings()})
        self.assertIn("credential_github_token", codes)
        self.assertIn("hygiene_region_marker_misuse", codes)

    def test_a_planted_marker_does_not_hide_a_real_home_path(self):
        self._write("docs/notes.md", 'P = "%s"\n' % synthetic_home_path())
        codes = sorted({item.code for item in self._findings()})
        self.assertIn("private_home_path", codes)

    def test_the_marker_is_misuse_even_at_the_scanner_basename(self):
        """Only the exact tools/ path is exempt, not any similar name."""
        self._write("public_hygiene_scan.py",
                    'T = "%s"\n' % synthetic_github_token())
        codes = sorted({item.code for item in self._findings()})
        self.assertIn("hygiene_region_marker_misuse", codes)
        self.assertIn("credential_github_token", codes)

    def test_a_marker_alone_is_still_reported(self):
        self._write("clean.md", "nothing to see here\n")
        codes = [item.code for item in self._findings()]
        self.assertEqual(codes, ["hygiene_region_marker_misuse"] * 2)

    def test_only_the_scanner_itself_carries_an_exempt_region(self):
        scanner._REGION_LINES.clear()
        scanner.scan(CANDIDATE_ROOT)
        exempt = {path: count for path, count
                  in scanner._REGION_LINES.items() if count}
        self.assertEqual(list(exempt), [scanner.SCANNER_SELF_PATH])
        self.assertGreater(exempt[scanner.SCANNER_SELF_PATH], 0)


if __name__ == "__main__":
    unittest.main()
