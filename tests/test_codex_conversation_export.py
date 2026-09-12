"""Deterministic tests for the codex-preserve export core.

Every fixture here is synthetic and local. No network, no model call, and no
real Codex session directory is read or written.
"""

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from codex_preserve import exporter


SESSION_A = "01a00000-1111-2222-3333-444444444444"
SESSION_B = "01b00000-1111-2222-3333-555555555555"
TURN_1 = "01a00000-aaaa-bbbb-cccc-000000000001"
TURN_2 = "01a00000-aaaa-bbbb-cccc-000000000002"
WORKSPACE = "/Users/testowner/Documents/GitHub/demo-project"


def artifact_path(package, filename):
    """Resolve a unique fixture payload through the canonical manifest."""
    manifest = json.loads((package / exporter.PACKAGE_MANIFEST_FILENAME).read_text())
    matches = [row["member_path"] for row in manifest["artifacts"]
               if row["original_filename"] == filename and row.get("member_path")]
    if len(matches) != 1:
        raise AssertionError("fixture expected one materialized artifact: %s" % filename)
    return package / matches[0]


# --------------------------------------------------------------------------
# Synthetic rollout construction
# --------------------------------------------------------------------------


@contextlib.contextmanager
def fake_home(home):
    """Pin Path.home() so path normalization is testable without a real home."""
    original = Path.home
    Path.home = staticmethod(lambda: Path(home))
    try:
        yield
    finally:
        Path.home = original


def record(kind, payload, ordinal, timestamp):
    return {"timestamp": timestamp, "ordinal": ordinal, "type": kind,
            "payload": payload}


class RolloutBuilder:
    def __init__(self, session_id=SESSION_A, workspace=WORKSPACE,
                 repository_url="git@github-demo:testowner/demo-project.git"):
        self.records = []
        self.ordinal = 0
        self.minute = 0
        self.session_id = session_id
        self.workspace = workspace
        self.session_meta(repository_url)

    def _stamp(self):
        self.minute += 1
        return "2026-08-25T10:%02d:00.000Z" % (self.minute % 60)

    def add(self, kind, payload):
        self.records.append(record(kind, payload, self.ordinal, self._stamp()))
        self.ordinal += 1
        return self.records[-1]

    def session_meta(self, repository_url):
        self.add("session_meta", {
            "session_id": self.session_id,
            "id": self.session_id,
            "timestamp": "2026-08-25T10:00:00.000Z",
            "cwd": self.workspace,
            "originator": "Codex Desktop",
            "cli_version": "0.149.0-alpha.4.1",
            "source": "vscode",
            "thread_source": "user",
            "model_provider": "openai",
            "base_instructions": {"text": "BASE " * 100,
                                  "provenance": {"type": "model",
                                                 "model": "gpt-5.6-sol"}},
            "dynamic_tools": [{"type": "tool_group", "name": "codex_app",
                               "tools": [{"name": "a"}, {"name": "b"}]}],
            "history_mode": "paginated",
            "git": {"commit_hash": "a" * 40, "branch": "codex/wip/demo",
                    "repository_url": repository_url},
        })

    def turn_context(self, turn_id=TURN_1, model="gpt-5.6-sol", effort="xhigh"):
        self.add("turn_context", {
            "turn_id": turn_id, "cwd": self.workspace,
            "workspace_roots": [self.workspace],
            "approval_policy": "never",
            "sandbox_policy": {"type": "danger-full-access"},
            "model": model, "effort": effort, "summary": "auto",
        })

    def task_started(self, turn_id=TURN_1):
        self.add("event_msg", {"type": "task_started", "turn_id": turn_id,
                               "started_at": 1787607214,
                               "model_context_window": 258400})

    def task_complete(self, turn_id=TURN_1, last_message="done"):
        self.add("event_msg", {"type": "task_complete", "turn_id": turn_id,
                               "started_at": 1787607214,
                               "completed_at": 1787608703,
                               "duration_ms": 1489474,
                               "last_agent_message": last_message})

    def owner_message(self, text, turn_id=TURN_1, mirror=True):
        """A real Owner message: response_item plus the typed UserMessage item."""
        self.add("response_item", {
            "type": "message", "id": "msg_%d" % self.ordinal, "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        if mirror:
            self.add("event_msg", {
                "type": "item_completed", "thread_id": self.session_id,
                "turn_id": turn_id,
                "item": {"type": "UserMessage", "id": "um_%d" % self.ordinal,
                         "content": [{"type": "text", "text": text}]},
                "started_at_ms": 1, "completed_at_ms": 2,
            })

    def injected_user_context(self, text, turn_id=TURN_1):
        """Persisted user-role text the Owner never typed."""
        self.owner_message(text, turn_id=turn_id, mirror=False)

    def developer_message(self, text, turn_id=TURN_1):
        self.add("response_item", {
            "type": "message", "id": "dev_%d" % self.ordinal, "role": "developer",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })

    def assistant(self, text, phase="commentary", turn_id=TURN_1, mirror=True):
        message_id = "am_%d" % self.ordinal
        self.add("response_item", {
            "type": "message", "id": message_id, "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        if mirror:
            self.add("event_msg", {
                "type": "item_completed", "thread_id": self.session_id,
                "turn_id": turn_id,
                "item": {"type": "AgentMessage", "id": message_id,
                         "content": [{"type": "text", "text": text}],
                         "phase": phase},
                "started_at_ms": 1, "completed_at_ms": 2,
            })

    def reasoning(self, summary=None, opaque=True, turn_id=TURN_1, mirror=True):
        reasoning_id = "rs_%d" % self.ordinal
        payload = {
            "type": "reasoning", "id": reasoning_id,
            "summary": [{"type": "summary_text", "text": item}
                        for item in (summary or [])],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        }
        if opaque:
            payload["encrypted_content"] = "OPAQUE" * 20
        self.add("response_item", payload)
        if mirror:
            self.add("event_msg", {
                "type": "item_completed", "thread_id": self.session_id,
                "turn_id": turn_id,
                "item": {"type": "Reasoning", "id": reasoning_id,
                         "summary_text": list(summary or []), "raw_content": []},
                "started_at_ms": 1, "completed_at_ms": 2,
            })

    def command(self, cmd, output="ok\n", exit_code=0, turn_id=TURN_1,
                wall=0.1, with_execution=True):
        call_id = "call_%d" % self.ordinal
        program = ('const r = await tools.exec_command(%s);\ntext(r.output);\n'
                   % json.dumps({"cmd": cmd, "workdir": self.workspace,
                                 "yield_time_ms": 10000}))
        self.add("response_item", {
            "type": "custom_tool_call", "id": "ctc_%d" % self.ordinal,
            "status": "completed", "call_id": call_id, "name": "exec",
            "input": program,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        if with_execution:
            self.add("event_msg", {
                "type": "item_completed", "thread_id": self.session_id,
                "turn_id": turn_id,
                "item": {"type": "CommandExecution", "id": "exec-%d" % self.ordinal,
                         "process_id": "1", "command": ["/bin/zsh", "-lc", cmd],
                         "cwd": "file://%s" % self.workspace,
                         "status": "completed" if exit_code == 0 else "failed",
                         "stdout": output, "stderr": "",
                         "aggregated_output": output, "exit_code": exit_code,
                         "duration": {"secs": 0, "nanos": 1}},
                "started_at_ms": 1, "completed_at_ms": 2,
            })
        self.add("response_item", {
            "type": "custom_tool_call_output", "id": "ctco_%d" % self.ordinal,
            "call_id": call_id,
            "output": [
                {"type": "input_text",
                 "text": "Script completed\nWall time %.1f seconds\nOutput:\n" % wall},
                {"type": "input_text", "text": output},
            ],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })

    def tool_invocation(self, program, turn_id=TURN_1):
        """Persist a raw invocation program for dynamic/template fixtures."""
        call_id = "call_%d" % self.ordinal
        self.add("response_item", {
            "type": "custom_tool_call", "id": "ctc_%d" % self.ordinal,
            "status": "completed", "call_id": call_id, "name": "exec",
            "input": program,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        return call_id

    def command_execution(self, cmd, exit_code=0, turn_id=TURN_1,
                          stderr="", cwd=None):
        """Persist durable execution evidence independently of an invocation."""
        self.add("event_msg", {
            "type": "item_completed", "thread_id": self.session_id,
            "turn_id": turn_id,
            "item": {"type": "CommandExecution", "id": "exec-%d" % self.ordinal,
                     "process_id": "1", "command": ["/bin/zsh", "-lc", cmd],
                     "cwd": "file://%s" % (cwd or self.workspace),
                     "status": "completed" if exit_code == 0 else "failed",
                     "stdout": "", "stderr": stderr,
                     "aggregated_output": stderr, "exit_code": exit_code,
                     "duration": {"secs": 2, "nanos": 0}},
            "started_at_ms": 1, "completed_at_ms": 2,
        })

    def tool_output(self, call_id, output="ok\n", turn_id=TURN_1, wall=0.1):
        self.add("response_item", {
            "type": "custom_tool_call_output", "id": "ctco_%d" % self.ordinal,
            "call_id": call_id,
            "output": [
                {"type": "input_text",
                 "text": "Script completed\nWall time %.1f seconds\nOutput:\n"
                         % wall},
                {"type": "input_text", "text": output},
            ],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })

    def stdin(self, chars, turn_id=TURN_1):
        """A `write_stdin` call carrying a decodable `chars` value."""
        self.stdin_raw(json.dumps({"session_id": "s1", "chars": chars,
                                   "yield_time_ms": 5000}), turn_id)

    def stdin_raw(self, literal, turn_id=TURN_1):
        """A `write_stdin` call whose argument object is written verbatim."""
        call_id = "call_%d" % self.ordinal
        self.add("response_item", {
            "type": "custom_tool_call", "id": "ctc_%d" % self.ordinal,
            "status": "completed", "call_id": call_id, "name": "exec",
            "input": "const r = await tools.write_stdin(%s);\ntext(r.output);\n"
                     % literal,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        self.add("response_item", {
            "type": "custom_tool_call_output", "id": "ctco_%d" % self.ordinal,
            "call_id": call_id,
            "output": [{"type": "input_text", "text": "ok\n"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })

    def wait(self, turn_id=TURN_1):
        """A `wait` tool call: structurally known poll noise, not a shell command."""
        call_id = "call_%d" % self.ordinal
        self.add("response_item", {
            "type": "custom_tool_call", "id": "ctc_%d" % self.ordinal,
            "status": "completed", "call_id": call_id, "name": "wait",
            "input": json.dumps({"session_id": "s1", "yield_time_ms": 5000}),
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })
        self.add("response_item", {
            "type": "custom_tool_call_output", "id": "ctco_%d" % self.ordinal,
            "call_id": call_id,
            "output": [{"type": "input_text", "text": "still running\n"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        })

    def token_count(self, with_rate_limits=True):
        payload = {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 100, "output_tokens": 20,
                                      "reasoning_output_tokens": 5,
                                      "cached_input_tokens": 10,
                                      "total_tokens": 120},
                "model_context_window": 258400,
            },
        }
        if with_rate_limits:
            payload["rate_limits"] = {
                "limit_id": "codex_demo", "limit_name": "GPT-5.3-Codex-Spark",
                "primary": {"used_percent": 12.5, "window_minutes": 300,
                            "resets_at": 1787620475},
                "credits": {"has_credits": True, "unlimited": False,
                            "balance": "9"},
                "plan_type": "pro",
            }
            payload["account_id"] = "acct_should_never_appear"
            payload["account_email"] = "owner@example.com"
        self.add("event_msg", payload)

    def write(self, directory, filename=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = filename or ("rollout-2026-08-25T10-00-00-%s.jsonl" % self.session_id)
        path = directory / name
        with path.open("w", encoding="utf-8") as handle:
            for item in self.records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return path


def minimal_session(builder=None):
    builder = builder or RolloutBuilder()
    builder.turn_context()
    builder.task_started()
    builder.owner_message("please audit the repository")
    builder.reasoning(summary=["**Planning the audit**"])
    builder.assistant("Reading the repository state now.")
    builder.command("git status --short", output=" M docs/notes.md\n")
    builder.reasoning(summary=["**Closing out**"])
    builder.assistant("Audit finished; one dirty file.", phase="final_answer")
    builder.token_count()
    builder.task_complete()
    return builder


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class ExporterTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex-export-test-"))
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.output = self.root / "out"
        self.session_index = self.root / "session_index.jsonl"
        self._previous_session_index = exporter.DEFAULT_SESSION_INDEX
        exporter.DEFAULT_SESSION_INDEX = str(self.session_index)
        self._previous_roots = os.environ.get(exporter.DISCOVERY_ROOTS_ENV)
        os.environ[exporter.DISCOVERY_ROOTS_ENV] = str(self.sessions)
        self.addCleanup(self._restore)

    def _restore(self):
        exporter.DEFAULT_SESSION_INDEX = self._previous_session_index
        if self._previous_roots is None:
            os.environ.pop(exporter.DISCOVERY_ROOTS_ENV, None)
        else:
            os.environ[exporter.DISCOVERY_ROOTS_ENV] = self._previous_roots
        shutil.rmtree(self.root, ignore_errors=True)

    def export(self, rollout_path, *extra, expect_status=0):
        argv = ["--rollout", str(rollout_path), "--output-dir", str(self.output),
                "--no-git-probe", "--quiet"] + list(extra)
        code = exporter.main(argv)
        self.assertEqual(code, expect_status, "unexpected exit code")
        if code != 0:
            return None, None
        markdown = sorted(self.output.rglob(exporter.CONVERSATION_FILENAME))[-1]
        receipt = sorted(self.output.rglob(exporter.RECEIPT_FILENAME))[-1]
        return markdown.read_text(encoding="utf-8"), json.loads(
            receipt.read_text(encoding="utf-8")
        )

    def package_dirs(self):
        return sorted({
            path.parent for path in self.output.rglob(exporter.RECEIPT_FILENAME)
        })

    def package_manifest(self):
        packages = self.package_dirs()
        self.assertTrue(packages)
        return json.loads(
            (packages[-1] / exporter.PACKAGE_MANIFEST_FILENAME).read_text("utf-8")
        )


# --------------------------------------------------------------------------
# T1 — synthetic minimal complete session
# --------------------------------------------------------------------------


class T1MinimalCompleteSession(ExporterTestCase):
    def test_complete_session_exports_every_visible_layer(self):
        path = minimal_session().write(self.sessions)
        markdown, receipt = self.export(path)
        self.assertEqual(receipt["export_status"], exporter.STATUS_COMPLETE)
        self.assertIn("please audit the repository", markdown)
        self.assertIn("Reading the repository state now.", markdown)
        self.assertIn("Audit finished; one dirty file.", markdown)
        self.assertIn("**Planning the audit**", markdown)
        self.assertIn("git status --short", markdown)
        self.assertEqual(receipt["counts"]["owner_messages_exported"], 1)
        self.assertEqual(receipt["counts"]["assistant_progress_exported"], 1)
        self.assertEqual(receipt["counts"]["assistant_final_exported"], 1)
        self.assertEqual(receipt["counts"]["exit_status_resolved"], "1/1")
        self.assertTrue(receipt["partial_state"]["all_turns_completed"])


# --------------------------------------------------------------------------
# T2 — duplicate mirrors
# --------------------------------------------------------------------------


class T2DuplicateRepresentations(ExporterTestCase):
    def test_exact_mirrors_dedupe_and_different_text_never_merges(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("exactly the same request")
        builder.assistant("mirrored progress")
        builder.reasoning(summary=["**Mirrored reasoning**"])
        builder.assistant("a different progress line")
        builder.task_complete()
        path = builder.write(self.sessions)
        markdown, receipt = self.export(path)

        breakdown = receipt["duplicate_breakdown"]
        self.assertEqual(breakdown["user_event_exact_text_mirror"], 1)
        self.assertEqual(breakdown["assistant_event_response_id_match"], 2)
        self.assertEqual(breakdown["reasoning_event_response_id_match"], 1)
        self.assertEqual(receipt["counts"]["owner_messages_exported"], 1)
        self.assertEqual(receipt["counts"]["assistant_progress_exported"], 2)
        # Two different assistant texts must both survive.
        self.assertIn("mirrored progress", markdown)
        self.assertIn("a different progress line", markdown)
        self.assertEqual(markdown.count("exactly the same request"), 1)

    def test_injected_user_context_is_not_presented_as_owner_text(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.injected_user_context(
            "<recommended_plugins>\nAirtable\n</recommended_plugins>"
        )
        builder.owner_message("the only thing I actually typed")
        builder.assistant("ok", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["counts"]["owner_messages_exported"], 1)
        self.assertEqual(receipt["counts"]["persisted_user_context_omitted"], 1)
        self.assertNotIn("Airtable", markdown)
        self.assertIn("recommended_plugins", markdown)  # inventory line only
        self.assertIn("the only thing I actually typed", markdown)


# --------------------------------------------------------------------------
# T3 — opaque reasoning
# --------------------------------------------------------------------------


class T3OpaqueReasoning(ExporterTestCase):
    def test_opaque_reasoning_is_counted_only(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("do the thing")
        for _ in range(5):
            builder.reasoning(summary=[], opaque=True)
        builder.reasoning(summary=["**One visible summary**"])
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        self.assertEqual(receipt["counts"]["opaque_reasoning_count"], 5)
        self.assertEqual(receipt["counts"]["visible_reasoning_records_source"], 1)
        self.assertNotIn("OPAQUE", markdown)
        self.assertNotIn("encrypted_content", markdown)
        boundary = receipt["reasoning_boundary"]
        self.assertFalse(boundary["hidden_chain_of_thought_exported"])
        self.assertFalse(boundary["hidden_chain_of_thought_promised"])
        self.assertFalse(boundary["opaque_reasoning_exported"])
        self.assertFalse(boundary["missing_reasoning_reconstructed"])
        self.assertTrue(boundary["visible_reasoning_summary_if_exposed"])


# --------------------------------------------------------------------------
# T4 — partial / interrupted turn
# --------------------------------------------------------------------------


class T4PartialInterruptedTurn(ExporterTestCase):
    def test_partial_turn_keeps_persisted_content_and_invents_no_final(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("start a long job")
        builder.reasoning(summary=["**Starting the long job**"])
        builder.assistant("Phase one is running.")
        builder.command("make build", output="building\n")
        builder.assistant("Phase two is running.")
        path = builder.write(self.sessions)  # no task_complete
        markdown, receipt = self.export(path)

        self.assertEqual(receipt["export_status"], exporter.STATUS_PARTIAL)
        self.assertIn("Phase one is running.", markdown)
        self.assertIn("Phase two is running.", markdown)
        self.assertIn("completed=false", markdown)
        self.assertFalse(receipt["partial_state"]["all_turns_completed"])
        self.assertEqual(receipt["partial_state"]["partial_turn_count"], 1)
        self.assertFalse(receipt["partial_state"]["final_answer_present"])
        self.assertFalse(receipt["partial_state"]["final_answer_invented"])
        self.assertEqual(receipt["counts"]["assistant_final_exported"], 0)


# --------------------------------------------------------------------------
# T5 — unknown schema
# --------------------------------------------------------------------------


class T5UnknownSchema(ExporterTestCase):
    def test_unknown_types_degrade_the_export_without_hiding_them(self):
        builder = minimal_session()
        builder.add("holographic_state", {"anything": 1})
        builder.add("response_item", {"type": "telepathy", "content": "?"})
        builder.add("event_msg", {"type": "quantum_event", "value": 2})
        builder.add("event_msg", {
            "type": "item_completed", "turn_id": TURN_1,
            "item": {"type": "MysteryItem", "id": "x"},
        })
        markdown, receipt = self.export(builder.write(self.sessions))

        self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
        schema = receipt["schema"]
        self.assertEqual(schema["unknown_record_type_counts"],
                         {"holographic_state": 1})
        self.assertEqual(schema["unknown_payload_type_counts"]["response_item/telepathy"], 1)
        self.assertEqual(schema["unknown_payload_type_counts"]["event_msg/quantum_event"], 1)
        self.assertEqual(schema["unknown_item_type_counts"], {"MysteryItem": 1})
        # Known material still exports.
        self.assertIn("please audit the repository", markdown)
        self.assertIn("DEGRADED_SCHEMA_DRIFT", markdown)


# --------------------------------------------------------------------------
# T6 — malformed JSONL line
# --------------------------------------------------------------------------


class T6MalformedLine(ExporterTestCase):
    def test_unparsable_lines_are_counted_not_ignored(self):
        path = minimal_session().write(self.sessions)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "response_item", "payload": {broken\n')
            handle.write("not json at all\n")
        markdown, receipt = self.export(path)

        self.assertEqual(receipt["schema"]["unparsable_lines"], 2)
        self.assertEqual(receipt["export_status"], exporter.STATUS_DEGRADED)
        self.assertTrue(any("unparsable" in warning
                            for warning in receipt["warnings"]))
        self.assertIn("unparsable_lines", markdown)


# --------------------------------------------------------------------------
# T7 — source changes during export
# --------------------------------------------------------------------------


class T7SourceChangedDuringExport(ExporterTestCase):
    def test_no_complete_markdown_is_published_when_the_source_moves(self):
        path = minimal_session().write(self.sessions)
        original = exporter.source_identity
        calls = {"n": 0}

        def flaky(target):
            calls["n"] += 1
            identity = original(target)
            if calls["n"] > 1:
                identity["sha256"] = "0" * 64
                identity["bytes"] = identity["bytes"] + 1
            return identity

        exporter.source_identity = flaky
        try:
            code = exporter.main(["--rollout", str(path), "--output-dir",
                                  str(self.output), "--no-git-probe", "--quiet"])
        finally:
            exporter.source_identity = original

        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        self.assertEqual(len(blocked), 1)
        receipt = json.loads(blocked[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["export_status"],
                         exporter.STATUS_BLOCKED_SOURCE)
        self.assertTrue(receipt["source"]["source_changed_during_export"])
        self.assertIn("retry", receipt["blocked_detail"])


# --------------------------------------------------------------------------
# T8 — concurrent candidate ambiguity
# --------------------------------------------------------------------------


class T8CandidateAmbiguity(ExporterTestCase):
    def _two_sessions_sharing_an_id(self):
        first = minimal_session().write(
            self.sessions / "2026" / "08" / "25",
            "rollout-2026-08-25T10-00-00-%s.jsonl" % SESSION_A,
        )
        second = minimal_session().write(
            self.sessions / "2026" / "08" / "25",
            "rollout-2026-08-25T11-00-00-%s.jsonl" % SESSION_A,
        )
        return first, second

    def test_ambiguous_session_id_fails_closed(self):
        self._two_sessions_sharing_an_id()
        code = exporter.main(["--session-id", SESSION_A, "--output-dir",
                              str(self.output), "--no-git-probe", "--quiet"])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        receipt = json.loads(blocked[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["export_status"],
                         exporter.STATUS_BLOCKED_SELECTION)
        self.assertEqual(len(receipt["matched_rollouts"]), 2)

    def test_missing_session_id_fails_closed(self):
        code = exporter.main(["--session-id", SESSION_B, "--output-dir",
                              str(self.output), "--quiet"])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])

    def test_candidate_listing_shows_sanitized_choice_metadata(self):
        self._two_sessions_sharing_an_id()
        rows = exporter.list_candidates(None, None)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["session_id"], SESSION_A)
            self.assertEqual(row["turn_count"], 1)
            self.assertEqual(row["completed_turn_count"], 1)
            self.assertEqual(row["repo"], "testowner/demo-project")
            self.assertIn("please audit the repository",
                          row["first_owner_message_preview"])
            self.assertNotIn("github-demo", json.dumps(row))


# --------------------------------------------------------------------------
# T9 — privacy
# --------------------------------------------------------------------------


class T9Privacy(ExporterTestCase):
    def _privacy_session(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "deploy from %s and read %s/Downloads/spec.md" % (WORKSPACE,
                                                              "/Users/testowner")
        )
        builder.assistant(
            "Using api_key=SUPERSECRETVALUE1 and Authorization: Bearer "
            # public-hygiene: synthetic-credential-fixture
            "abcdefghijklmnopqrstuvwxyz012345 with token ghp_ABCDEFGHIJKLMNOPQRSTUV"
        )
        builder.command(
            "cd /tmp/run.ABCdef && git remote -v && cat /Users/testowner/notes.txt",
            output="origin\tgit@github-demo:testowner/demo-project.git (fetch)\n",
        )
        builder.assistant("commit a1b2c3d4 kept 100 tokens; sha256=deadbeefcafe",
                          phase="final_answer")
        builder.token_count(with_rate_limits=True)
        builder.task_complete()
        return builder.write(self.sessions)

    def test_credentials_are_redacted_and_identity_paths_normalized(self):
        path = self._privacy_session()
        with fake_home("/Users/testowner"):
            markdown, receipt = self.export(path)

        self.assertNotIn("SUPERSECRETVALUE1", markdown)
        # public-hygiene: synthetic-credential-fixture
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUV", markdown)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", markdown)
        self.assertNotIn("/Users/testowner", markdown)
        self.assertNotIn("github-demo", markdown)
        self.assertNotIn("/tmp/run.ABCdef", markdown)
        self.assertIn("$WORKSPACE", markdown)
        self.assertIn("$ATTACHMENT/spec.md", markdown)
        self.assertIn("$TMP/run.ABCdef", markdown)
        self.assertIn("testowner/demo-project", markdown)

        # Project-useful identifiers must survive redaction untouched.
        self.assertIn("a1b2c3d4", markdown)
        self.assertIn("deadbeefcafe", markdown)
        self.assertIn("100 tokens", markdown)

        privacy = receipt["privacy"]
        self.assertTrue(privacy["normalization_enabled"])
        self.assertGreater(privacy["redactions_total"], 0)
        self.assertEqual(privacy["sanitization_rescan_residuals"], {})

    def test_account_and_rate_limit_telemetry_never_reaches_an_artifact(self):
        path = self._privacy_session()
        with fake_home("/Users/testowner"):
            markdown, receipt = self.export(path)
        blob = markdown + json.dumps(receipt, ensure_ascii=False)
        for forbidden in ("acct_should_never_appear", "owner@example.com",
                          "codex_demo", "GPT-5.3-Codex-Spark", "has_credits",
                          "used_percent", "resets_at"):
            self.assertNotIn(forbidden, blob, forbidden)
        self.assertGreater(
            receipt["privacy"]["drops"]["rate_limit_and_credit_telemetry"], 0
        )
        self.assertGreater(receipt["privacy"]["drops"]["account_identity"], 0)
        # Aggregate usage stays: it is project evidence, not billing identity.
        self.assertEqual(
            receipt["session"]["aggregate_token_usage"]["total_tokens"], 120
        )

    def test_zero_redactions_does_not_mean_normalization_was_off(self):
        markdown, receipt = self.export(minimal_session().write(self.sessions))
        self.assertEqual(receipt["privacy"]["redactions_total"], 0)
        self.assertTrue(receipt["privacy"]["normalization_enabled"])
        self.assertIn("normalization_enabled      True", markdown)

    def test_residual_credential_blocks_publication(self):
        """The post-render rescan is a real gate, not a decorative counter."""
        path = self._privacy_session()
        original = exporter.Privacy.redact
        exporter.Privacy.redact = lambda self, text: text or ""
        try:
            with fake_home("/Users/testowner"):
                code = exporter.main(["--rollout", str(path), "--output-dir",
                                      str(self.output), "--no-git-probe",
                                      "--quiet"])
        finally:
            exporter.Privacy.redact = original
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        receipt = json.loads(blocked[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["export_status"],
                         exporter.STATUS_BLOCKED_PRIVACY)
        self.assertIn("github_token",
                      receipt["privacy"]["sanitization_rescan_residuals"])

    def test_injected_envelopes_and_schemas_are_dropped(self):
        builder = minimal_session()
        builder.developer_message("<app-context>internal desktop context</app-context>")
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertNotIn("internal desktop context", markdown)
        self.assertNotIn("BASE BASE", markdown)
        drops = receipt["privacy"]["drops"]
        self.assertGreaterEqual(drops["developer_message"], 1)
        self.assertEqual(drops["base_instructions_text"], 1)
        self.assertEqual(drops["dynamic_tool_schemas"], 1)
        self.assertIsNotNone(receipt["session"]["base_instructions_sha256"])


# --------------------------------------------------------------------------
# T10 — tool folding
# --------------------------------------------------------------------------


class T10ToolFolding(ExporterTestCase):
    def test_every_action_survives_with_its_outcome(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("run the release checks")
        for index in range(6):
            builder.command("rg -n 'TODO' src/module_%d.py" % index)
        builder.command("git commit -m 'release: cut v2'", output="1 file changed\n")
        for index in range(4):
            builder.command("cat build/report_%d.txt" % index)
        builder.command("pytest tests/test_release.py", output="1 failed\n",
                        exit_code=1)
        builder.command("cat build/summary.txt")
        builder.assistant("release check finished", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        counts = receipt["counts"]
        # v1 folds no shell command, so every one of these is its own row.
        self.assertEqual(counts["tool_calls_source"], 13)
        self.assertEqual(counts["logical_tool_rows_exported"], 13)
        self.assertEqual(counts["tool_rows_folded"], 0)
        self.assertEqual(counts["fold_windows"], 0)
        self.assertNotIn("collapsed ×", markdown)

        # What this test really protects: significant actions and their
        # outcomes survive, whatever the fold policy is.
        for command in ("rg -n 'TODO' src/module_0.py",
                        "cat build/report_0.txt",
                        "git commit -m 'release: cut v2'",
                        "pytest tests/test_release.py",
                        "cat build/summary.txt"):
            self.assertIn(command, markdown)
        self.assertIn("**FAILED**", markdown)
        self.assertEqual(counts["failed_actions"], 1)

    def test_wrapper_boilerplate_never_reaches_the_export(self):
        markdown, _ = self.export(minimal_session().write(self.sessions))
        self.assertNotIn("tools.exec_command", markdown)
        self.assertNotIn("text(r.output)", markdown)
        self.assertNotIn("yield_time_ms", markdown)

    def test_unpaired_command_reports_unknown_exit_instead_of_guessing(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("check it")
        builder.command("git rev-parse HEAD", with_execution=False)
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertIn("exit=unknown", markdown)
        self.assertEqual(receipt["counts"]["exit_status_resolved"], "0/1")


# --------------------------------------------------------------------------
# T11 — artifact exact-path capture
# --------------------------------------------------------------------------


class T11ExplicitArtifacts(ExporterTestCase):
    def test_exact_paths_are_hashed_and_nothing_is_scanned(self):
        bundle = self.root / "Demo_ReviewBundle_v01.zip"
        bundle.write_bytes(b"PK\x03\x04demo-review-bundle")
        extra = self.root / "coverage.json"
        extra.write_text('{"ok": true}', encoding="utf-8")
        decoy = self.root / "unrelated_bundle.zip"
        decoy.write_bytes(b"PK\x03\x04should-never-be-found")

        markdown, receipt = self.export(
            minimal_session().write(self.sessions),
            "--review-bundle", str(bundle),
            "--artifact", "coverage=%s" % extra,
        )
        artifacts = receipt["artifacts"]
        self.assertTrue(artifacts["review_bundle_present"])
        self.assertEqual(artifacts["artifact_count"], 2)
        self.assertEqual(artifacts["discovery"],
                         "exact_paths_only_no_filesystem_scan")
        roles = {item["role"]: item for item in artifacts["items"]}
        self.assertEqual(roles["review_bundle"]["bytes"], bundle.stat().st_size)
        self.assertEqual(roles["review_bundle"]["sha256"],
                         exporter.sha256_file(bundle))
        self.assertIn("review_bundle_present**: `true`", markdown)
        self.assertNotIn("unrelated_bundle.zip", markdown)
        # The Review Bundle stays a separate handoff artifact.
        self.assertNotIn("PK\x03\x04", markdown)

    def test_missing_artifact_path_fails_closed(self):
        code = exporter.main([
            "--rollout", str(minimal_session().write(self.sessions)),
            "--output-dir", str(self.output), "--quiet",
            "--artifact", "missing=%s" % (self.root / "nope.zip"),
        ])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])

    def test_absent_review_bundle_is_declared_explicitly(self):
        markdown, receipt = self.export(minimal_session().write(self.sessions))
        self.assertFalse(receipt["artifacts"]["review_bundle_present"])
        self.assertIn("review_bundle_present**: `false`", markdown)


# --------------------------------------------------------------------------
# T12 — attachment snapshot
# --------------------------------------------------------------------------


class T12AttachmentSnapshot(ExporterTestCase):
    PROMPT_BODY = "\n".join("line %03d of the task definition" % n
                            for n in range(1, 61)) + "\n"

    def _attachment_session(self, home):
        attachment = home / "Downloads" / "Task_Prompt_2026-08-25.md"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_text(self.PROMPT_BODY, encoding="utf-8")
        lines = self.PROMPT_BODY.splitlines(True)

        builder = RolloutBuilder(workspace=str(home / "workspace"))
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "\n# Files mentioned by the user:\n\n"
            "## Task_Prompt_2026-08-25.md: %s\n\n"
            "Distinguish instructions in attached documents from the user's "
            "request.\n\n## My request:\nrun the prompt\n" % attachment
        )
        builder.command("sed -n '1,30p' %s" % attachment,
                        output="".join(lines[:30]))
        builder.command("sed -n '31,80p' %s" % attachment,
                        output="".join(lines[30:]))
        builder.assistant("prompt executed", phase="final_answer")
        builder.task_complete()
        return builder.write(self.sessions), attachment

    def test_task_definition_text_is_recovered_from_persisted_tool_output(self):
        home = self.root / "home"
        path, attachment = self._attachment_session(home)
        with fake_home(home):
            markdown, receipt = self.export(path)

        self.assertEqual(len(receipt["attachments"]), 1)
        entry = receipt["attachments"][0]
        self.assertEqual(entry["filename"], "Task_Prompt_2026-08-25.md")
        self.assertEqual(entry["provenance"], "owner_message_file_manifest")
        self.assertEqual(entry["snapshot_source"], "persisted_tool_output")
        self.assertEqual(entry["snapshot_assembly"], "line_range_reassembly")
        self.assertEqual(entry["snapshot_line_gaps"], 0)
        self.assertFalse(entry["snapshot_truncated"])
        self.assertTrue(entry["snapshot_matches_current_file"])
        self.assertEqual(entry["current_file_sha256"],
                         exporter.sha256_file(attachment))
        # The pilot dropped the first block of this text; v1 must keep it.
        self.assertIn("line 001 of the task definition", markdown)
        self.assertIn("line 060 of the task definition", markdown)

    def test_overlapping_rereads_do_not_duplicate_file_regions(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "Task_Prompt_2026-08-25.md"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_text(self.PROMPT_BODY, encoding="utf-8")
        lines = self.PROMPT_BODY.splitlines(True)

        builder = RolloutBuilder(workspace=str(home / "workspace"))
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "\n# Files mentioned by the user:\n\n"
            "## Task_Prompt_2026-08-25.md: %s\n\n## My request:\ngo\n" % attachment
        )
        builder.command("sed -n '1,40p' %s" % attachment,
                        output="".join(lines[:40]))
        builder.command("sed -n '20,60p' %s" % attachment,
                        output="".join(lines[19:]))
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        path = builder.write(self.sessions)

        with fake_home(home):
            markdown, receipt = self.export(path)
        entry = receipt["attachments"][0]
        self.assertEqual(entry["snapshot_bytes"],
                         len(self.PROMPT_BODY.encode("utf-8")))
        self.assertTrue(entry["snapshot_matches_current_file"])
        self.assertEqual(markdown.count("line 025 of the task definition"), 1)

    def test_filtered_reads_are_never_treated_as_faithful_file_text(self):
        self.assertIsNone(exporter.faithful_read_target("cat /a/b.md | head -5"))
        self.assertIsNone(exporter.faithful_read_target("rg -n TODO /a/b.md"))
        self.assertIsNone(exporter.faithful_read_target("cat /a/b.md /a/c.md"))
        self.assertEqual(exporter.faithful_read_target("cat /a/b.md"), "/a/b.md")
        self.assertEqual(exporter.faithful_read_target("sed -n '1,9p' /a/b.md"),
                         "/a/b.md")


# --------------------------------------------------------------------------
# T13 — source read-only
# --------------------------------------------------------------------------


class T13SourceReadOnly(ExporterTestCase):
    def test_export_never_mutates_the_rollout(self):
        path = minimal_session().write(self.sessions)
        before = (path.stat().st_size, path.stat().st_mtime_ns,
                  exporter.sha256_file(path))
        _, receipt = self.export(path)
        after = (path.stat().st_size, path.stat().st_mtime_ns,
                 exporter.sha256_file(path))
        self.assertEqual(before, after)
        self.assertEqual(receipt["source"]["rollout_sha256_pre"], before[2])
        self.assertEqual(receipt["source"]["rollout_sha256_post"], before[2])
        self.assertFalse(receipt["source"]["source_changed_during_export"])
        self.assertFalse(receipt["source"]["source_opened_for_write"])


# --------------------------------------------------------------------------
# T14 — deterministic replay
# --------------------------------------------------------------------------


class T14DeterministicReplay(ExporterTestCase):
    def test_same_source_and_arguments_produce_the_same_body_and_receipt(self):
        path = minimal_session().write(self.sessions)
        first_markdown, first_receipt = self.export(path)
        shutil.rmtree(self.output)
        self.output.mkdir()
        second_markdown, second_receipt = self.export(path)

        self.assertEqual(exporter.markdown_body(first_markdown),
                         exporter.markdown_body(second_markdown))
        self.assertEqual(first_receipt["markdown"]["body_sha256"],
                         second_receipt["markdown"]["body_sha256"])
        self.assertEqual(first_receipt["receipt_core_sha256"],
                         second_receipt["receipt_core_sha256"])
        # The volatile header is isolated, not absent.
        self.assertIn("generated_at=", first_markdown)
        self.assertNotIn("generated_at=",
                         exporter.markdown_body(first_markdown))

    def test_privacy_counters_are_not_double_counted_by_the_two_render_passes(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("one secret here: api_key=ONLYONCEVALUE9")
        builder.assistant("ack", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["privacy"]["redactions"]["assigned_credential"], 1)


# --------------------------------------------------------------------------
# T15 — multi-turn ordering
# --------------------------------------------------------------------------


class T15MultiTurnOrdering(ExporterTestCase):
    def test_turn_order_and_within_turn_order_are_preserved(self):
        builder = RolloutBuilder()
        builder.turn_context(TURN_1)
        builder.task_started(TURN_1)
        builder.owner_message("first turn request", turn_id=TURN_1)
        builder.reasoning(summary=["**Turn one opening**"], turn_id=TURN_1)
        builder.assistant("turn one progress", turn_id=TURN_1)
        builder.assistant("turn one final", phase="final_answer", turn_id=TURN_1)
        builder.task_complete(TURN_1)

        builder.turn_context(TURN_2)
        builder.task_started(TURN_2)
        builder.owner_message("second turn request", turn_id=TURN_2)
        builder.reasoning(summary=["**Turn two opening**"], turn_id=TURN_2)
        builder.assistant("turn two progress", turn_id=TURN_2)
        builder.assistant("turn two final", phase="final_answer", turn_id=TURN_2)
        builder.task_complete(TURN_2)

        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["session"]["turn_ids"], [TURN_1, TURN_2])
        self.assertEqual(receipt["session"]["turn_count"], 2)
        self.assertEqual(receipt["session"]["completed_turn_count"], 2)

        order = [
            markdown.index("first turn request"),
            markdown.index("second turn request"),
        ]
        self.assertEqual(order, sorted(order))
        for earlier, later in (
            ("turn one progress", "turn one final"),
            ("turn one final", "turn two progress"),
            ("turn two progress", "turn two final"),
            ("**Turn one opening**", "**Turn two opening**"),
        ):
            self.assertLess(markdown.index(earlier), markdown.index(later),
                            "%s must precede %s" % (earlier, later))



# --------------------------------------------------------------------------
# Coordinator Fix 1 — targeted counterexamples
#
# Every case below reproduces a defect the Coordinator proved in the first
# candidate, so a regression re-breaks the exact behaviour that was blocked.
# --------------------------------------------------------------------------


def load_model(path):
    """Parse a rollout without rendering, for selection-level assertions."""
    privacy = exporter.Privacy(normalize=True)
    model = exporter.RolloutModel(privacy)
    model.load(Path(path))
    return model


def reasons_for(selection, text):
    for item in selection["selected"]:
        if any(text in block for block in item["texts"]):
            return set(item["reasons"])
    return None


class PrivateHomeTestCase(ExporterTestCase):
    """A synthetic private home, so raw-path leaks are literally detectable."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "Users" / "alice"
        self.workspace = self.home / "Documents" / "GitHub" / "demo-project"
        self.sessions = self.home / ".codex" / "sessions"
        self.sessions.mkdir(parents=True)
        os.environ[exporter.DISCOVERY_ROOTS_ENV] = str(self.sessions)


# --------------------------------------------------------------------------
# B1 — workspace filtering uses the raw coordinate, output stays normalized
# --------------------------------------------------------------------------


class T16WorkspaceFilter(PrivateHomeTestCase):
    def _session(self, workspace, session_id=SESSION_A):
        builder = minimal_session(
            RolloutBuilder(session_id=session_id, workspace=str(workspace))
        )
        return builder.write(self.sessions,
                             "rollout-2026-08-25T10-00-00-%s.jsonl" % session_id)

    def test_matching_raw_workspace_returns_the_candidate(self):
        self._session(self.workspace)
        with fake_home(self.home):
            rows = exporter.list_candidates(str(self.workspace), None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], SESSION_A)

    def test_normalized_display_is_never_used_as_the_filter_key(self):
        # The display value is "$WORKSPACE"; filtering on it would return [].
        self._session(self.workspace)
        with fake_home(self.home):
            rows = exporter.list_candidates(str(self.workspace), None)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["workspace"], "$WORKSPACE")
            self.assertEqual(exporter.list_candidates("$WORKSPACE", None), [])

    def test_different_raw_workspace_is_excluded(self):
        self._session(self.home / "Documents" / "GitHub" / "other-project")
        with fake_home(self.home):
            self.assertEqual(exporter.list_candidates(str(self.workspace), None),
                             [])

    def test_candidate_listing_carries_no_raw_private_path(self):
        self._session(self.workspace)
        with fake_home(self.home):
            rows = exporter.list_candidates(None, None)
        payload = json.dumps(rows)
        self.assertNotIn("/Users/alice", payload)
        self.assertNotIn(str(self.home), payload)
        # Exactly one path field, and it is the normalized one.
        self.assertNotIn("rollout_tilde_normalized", payload)
        self.assertEqual([key for key in rows[0] if "path" in key],
                         ["rollout_path_normalized"])
        self.assertIn("~/.codex/sessions", rows[0]["rollout_path_normalized"])

    def test_raw_coordinate_never_survives_into_a_public_row(self):
        path = self._session(self.workspace)
        with fake_home(self.home):
            row = exporter.scan_candidate(path)
            self.assertEqual(row["_selection"]["cwd"], str(self.workspace))
            self.assertNotIn("_selection", exporter.public_candidate(row))


# --------------------------------------------------------------------------
# B2 — --session-id is an exact UUID cross-checked against session_meta
# --------------------------------------------------------------------------


class T17ExactSessionIdentity(PrivateHomeTestCase):
    def _session(self, session_id, filename_id=None):
        builder = minimal_session(
            RolloutBuilder(session_id=session_id, workspace=str(self.workspace))
        )
        return builder.write(
            self.sessions,
            "rollout-2026-08-25T10-00-00-%s.jsonl" % (filename_id or session_id),
        )

    def _blocked(self, session_id):
        code = exporter.main(["--session-id", session_id, "--output-dir",
                              str(self.output), "--no-git-probe", "--quiet"])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        self.assertEqual(len(blocked), 1)
        return json.loads(blocked[0].read_text(encoding="utf-8"))

    def test_exact_uuid_selects_exactly_one_session(self):
        self._session(SESSION_A)
        self._session(SESSION_B)
        with fake_home(self.home):
            resolved = exporter.resolve_source(None, SESSION_A)
        self.assertIn(SESSION_A, resolved.name)
        self.assertEqual(exporter.session_meta_identity(resolved), SESSION_A)

    def test_uuid_prefix_is_rejected(self):
        self._session(SESSION_A)
        with fake_home(self.home):
            receipt = self._blocked(SESSION_A[:8])
        self.assertEqual(receipt["export_status"],
                         exporter.STATUS_BLOCKED_SELECTION)
        self.assertEqual(receipt["selection_status"],
                         "BLOCKED_MALFORMED_SESSION_ID")
        self.assertFalse(receipt["requested_session_id_is_canonical_uuid"])

    def test_malformed_uuid_is_rejected(self):
        self._session(SESSION_A)
        with fake_home(self.home):
            receipt = self._blocked("not-a-uuid")
        self.assertEqual(receipt["selection_status"],
                         "BLOCKED_MALFORMED_SESSION_ID")

    def test_filename_claiming_another_session_is_rejected(self):
        # File is named after session B; its session_meta says session A.
        self._session(SESSION_A, filename_id=SESSION_B)
        with fake_home(self.home):
            receipt = self._blocked(SESSION_B)
        self.assertEqual(receipt["selection_status"],
                         "BLOCKED_NO_SESSION_META_MATCH")
        self.assertEqual(receipt["filename_claimed_but_session_meta_differs"], 1)
        self.assertEqual(receipt["matched_rollouts"], [])

    def test_identity_comes_from_session_meta_not_the_filename(self):
        # The same misnamed file is still reachable by its real identity.
        self._session(SESSION_A, filename_id=SESSION_B)
        with fake_home(self.home):
            resolved = exporter.resolve_source(None, SESSION_A)
        self.assertIn(SESSION_B, resolved.name)
        self.assertEqual(exporter.session_meta_identity(resolved), SESSION_A)

    def test_two_rollouts_with_the_same_identity_stay_ambiguous(self):
        self._session(SESSION_A)
        minimal_session(
            RolloutBuilder(session_id=SESSION_A, workspace=str(self.workspace))
        ).write(self.sessions,
                "rollout-2026-08-25T11-00-00-%s.jsonl" % SESSION_A)
        with fake_home(self.home):
            receipt = self._blocked(SESSION_A)
        self.assertEqual(receipt["selection_status"],
                         "BLOCKED_AMBIGUOUS_SESSION_ID")
        self.assertEqual(len(receipt["matched_rollouts"]), 2)

    def test_explicit_rollout_must_agree_with_a_supplied_session_id(self):
        path = self._session(SESSION_A)
        with fake_home(self.home):
            code = exporter.main(["--rollout", str(path), "--session-id",
                                  SESSION_B, "--output-dir", str(self.output),
                                  "--no-git-probe", "--quiet"])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.output.glob("*.md")), [])


# --------------------------------------------------------------------------
# B3 — candidate listing and blocked receipts are generated artifacts
# --------------------------------------------------------------------------


class T18BlockedReceiptPrivacy(PrivateHomeTestCase):
    def _receipt_text(self, argv):
        code = exporter.main(argv + ["--output-dir", str(self.output),
                                     "--no-git-probe", "--quiet"])
        self.assertEqual(code, 2)
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        self.assertEqual(len(blocked), 1)
        return blocked[0].read_text(encoding="utf-8")

    def _two_sessions(self):
        for hour in ("10", "11"):
            minimal_session(
                RolloutBuilder(session_id=SESSION_A,
                               workspace=str(self.workspace))
            ).write(self.sessions,
                    "rollout-2026-08-25T%s-00-00-%s.jsonl" % (hour, SESSION_A))

    def test_ambiguous_matched_rollouts_are_normalized(self):
        self._two_sessions()
        with fake_home(self.home):
            text = self._receipt_text(["--session-id", SESSION_A])
        self.assertNotIn("/Users/alice", text)
        self.assertNotIn(str(self.home), text)
        receipt = json.loads(text)
        self.assertEqual(len(receipt["matched_rollouts"]), 2)
        for entry in receipt["matched_rollouts"]:
            self.assertTrue(entry.startswith("$CODEX_STATE/"), entry)

    def test_mismatched_identity_receipt_is_normalized(self):
        minimal_session(
            RolloutBuilder(session_id=SESSION_A, workspace=str(self.workspace))
        ).write(self.sessions,
                "rollout-2026-08-25T10-00-00-%s.jsonl" % SESSION_B)
        with fake_home(self.home):
            text = self._receipt_text(["--session-id", SESSION_B])
        self.assertNotIn("/Users/alice", text)
        self.assertNotIn(str(self.home), text)

    def test_missing_rollout_path_is_normalized_in_the_receipt(self):
        missing = self.home / "Downloads" / "not-here.jsonl"
        with fake_home(self.home):
            text = self._receipt_text(["--rollout", str(missing)])
        self.assertNotIn("/Users/alice", text)
        self.assertIn("$ATTACHMENT/not-here.jsonl", text)

    def test_normal_receipt_output_paths_are_normalized(self):
        path = minimal_session(
            RolloutBuilder(session_id=SESSION_A, workspace=str(self.workspace))
        ).write(self.sessions)
        output = self.home / "Desktop" / "Codex"
        with fake_home(self.home):
            code = exporter.main(["--rollout", str(path), "--output-dir",
                                  str(output), "--no-git-probe", "--quiet"])
            self.assertEqual(code, 0)
            receipt = json.loads(
                sorted(output.rglob(exporter.RECEIPT_FILENAME))[-1]
                .read_text("utf-8")
            )
        self.assertNotIn("/Users/alice", json.dumps(receipt))
        self.assertTrue(receipt["volatile"]["markdown_path"].startswith("~/"))


# --------------------------------------------------------------------------
# B4 — reasoning anchors may never cross a turn boundary
# --------------------------------------------------------------------------


class T19TurnLocalReasoning(ExporterTestCase):
    def _two_turn(self, turn_one):
        builder = RolloutBuilder()
        builder.turn_context(turn_id=TURN_1)
        builder.task_started(turn_id=TURN_1)
        turn_one(builder)
        builder.task_complete(turn_id=TURN_1)
        builder.turn_context(turn_id=TURN_2)
        builder.task_started(turn_id=TURN_2)
        builder.reasoning(summary=["**Turn two thinking**"], turn_id=TURN_2)
        # Deliberately not a final answer: turn two must contribute no anchor
        # of its own beyond its turn start, so any other reason appearing on
        # its reasoning record could only have come from turn one.
        builder.assistant("turn two continues", turn_id=TURN_2)
        builder.task_complete(turn_id=TURN_2)
        return load_model(builder.write(self.sessions))

    def test_owner_boundary_in_turn_one_cannot_select_turn_two_reasoning(self):
        def turn_one(builder):
            builder.owner_message("first request", turn_id=TURN_1)
            builder.assistant("turn one done", turn_id=TURN_1)

        model = self._two_turn(turn_one)
        selection = exporter.select_visible_reasoning(model, 40)
        reasons = reasons_for(selection, "Turn two thinking")
        self.assertIsNotNone(reasons)
        self.assertNotIn("owner_message_boundary", reasons)
        self.assertEqual(reasons, {"turn_start"})

    def test_tool_failure_in_turn_one_cannot_select_turn_two_reasoning(self):
        def turn_one(builder):
            builder.owner_message("first request", turn_id=TURN_1)
            builder.command("pytest tests/test_a.py", output="1 failed\n",
                            exit_code=1, turn_id=TURN_1)
            builder.assistant("turn one done", turn_id=TURN_1)

        model = self._two_turn(turn_one)
        selection = exporter.select_visible_reasoning(model, 40)
        reasons = reasons_for(selection, "Turn two thinking")
        self.assertNotIn("pre_failure_decision", reasons)
        self.assertNotIn("post_failure_recovery", reasons)

    def test_final_in_turn_one_cannot_select_turn_two_reasoning(self):
        def turn_one(builder):
            builder.owner_message("first request", turn_id=TURN_1)
            builder.assistant("turn one final", phase="final_answer",
                              turn_id=TURN_1)

        model = self._two_turn(turn_one)
        selection = exporter.select_visible_reasoning(model, 40)
        reasons = reasons_for(selection, "Turn two thinking")
        self.assertNotIn("pre_final_decision", reasons)

    def test_same_turn_anchors_still_select_normally(self):
        builder = RolloutBuilder()
        builder.turn_context(turn_id=TURN_1)
        builder.task_started(turn_id=TURN_1)
        builder.owner_message("do the work", turn_id=TURN_1)
        builder.reasoning(summary=["**Deciding the route**"], turn_id=TURN_1)
        builder.command("pytest tests/test_a.py", output="1 failed\n",
                        exit_code=1, turn_id=TURN_1)
        builder.reasoning(summary=["**Recovering after the failure**"],
                          turn_id=TURN_1)
        builder.assistant("done", phase="final_answer", turn_id=TURN_1)
        builder.task_complete(turn_id=TURN_1)
        model = load_model(builder.write(self.sessions))
        selection = exporter.select_visible_reasoning(model, 40)

        first = reasons_for(selection, "Deciding the route")
        second = reasons_for(selection, "Recovering after the failure")
        self.assertIn("owner_message_boundary", first)
        self.assertIn("pre_failure_decision", first)
        self.assertIn("post_failure_recovery", second)
        self.assertEqual(selection["counts"]["selection_scope"], "turn_local")


# --------------------------------------------------------------------------
# B5 — every shell command is its own explicit row
# --------------------------------------------------------------------------


class T20EveryShellCommandIsExplicit(ExporterTestCase):
    """v1 folds no shell command, so nothing can be hidden by folding it.

    B5 was reopened four times. Each round proved a narrower classifier — write
    programs, then later segments, then assignment and control prefixes, then a
    positive safe-read allowlist — and each one closed its own matrix and left
    another shape in. The real fixture then showed the whole mechanism was
    worth about 15 rows out of 271. The rule is now that there is no rule: a
    recorded command is a row.
    """

    # One representative from every shape that ever slipped through, plus
    # ordinary reads, so re-enabling shell folding breaks this immediately.
    REPRESENTATIVE_COMMANDS = (
        # Plain reads. Explicit too — that is the point of the invariant.
        "pwd", "ls", "cat file", "git status", "git log --oneline -3",
        "git rev-parse HEAD", "jq '.x' file", "rg -n TODO src",
        "sed -n '1,30p' notes.md", "ls | grep x",
        # Fix1: direct writes and redirection.
        "mkdir -p /tmp/run/a", "touch /tmp/run/a/marker", "tee /tmp/run/a/log",
        "echo hi > /tmp/run/a/out", "cp a b", "mv a b", "rm -rf a",
        "chmod 700 a", "sed -i '' s/a/b/ f.txt", "cat a >> b",
        # Fix2: chained and piped writes.
        "echo x | tee /tmp/out.txt", "echo f | xargs rm -f",
        "git status --short && git commit -m wip", "pwd && git push",
        "ls && npm publish", "grep -r x . ; mkdir -p /tmp/newdir",
        "bash -c 'echo hi > /tmp/f'", "zsh -lc 'git commit -m nested'",
        # Fix3: environment-assignment and control prefixes.
        "CI=1 npm publish", "FOO=bar git commit -m x",
        "if test -f x; then git commit -m x; fi",
        'for f in *; do rm -f "$f"; done', "while true; do npm publish; done",
        'case "$x" in a) mkdir -p /tmp/a ;; esac',
        # Fix4: wrappers, Git global options, find.
        "sudo -u root rm -rf /tmp/x", "env -u FOO npm publish",
        "nice -n 10 rm -rf /tmp/x", "git -C /tmp/repo commit -m x",
        "git -c user.name=x commit -m x", "find /tmp -type f -delete",
        "find /tmp -delete", "find /tmp -type f -exec rm -f {} +",
        "cat a | tee b", "git log",
        # Fix5: the shapes that defeated the safe-read allowlist itself —
        # process substitution, write modes of read programs, and Git config
        # options that run an external helper.
        "cat <(rm -rf /tmp/x)", "grep needle <(npm publish)",
        "cat <(git commit -m x)", "xxd -r in.hex out.bin",
        "hostname newname", "date 010100002026", "file -C -m magic",
        "rg --pre 'rm -rf /tmp/x' needle .",
        "git -c diff.external='rm -rf /tmp/x' diff",
        "git -c core.fsmonitor='!/tmp/mutate.sh' status",
        "git --config-env=core.fsmonitor=FSVAR status",
        # Whatever a future Codex runs.
        "brand-new-future-tool --sync", "unknown-tool --whatever",
    )

    def test_every_recorded_command_exports_as_its_own_row(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("run everything")
        for command in self.REPRESENTATIVE_COMMANDS:
            builder.command(command)
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        counts = receipt["counts"]
        self.assertEqual(counts["tool_calls_source"],
                         len(self.REPRESENTATIVE_COMMANDS))
        self.assertEqual(counts["logical_tool_rows_exported"],
                         len(self.REPRESENTATIVE_COMMANDS))
        self.assertEqual(counts["tool_rows_folded"], 0)
        self.assertEqual(counts["fold_windows"], 0)
        self.assertNotIn("collapsed ×", markdown)

        explicit = [line for line in markdown.splitlines()
                    if line.startswith("- `")]
        for command in self.REPRESENTATIVE_COMMANDS:
            row = "`%s`" % command.replace("/tmp/", "$TMP/")
            self.assertTrue(any(row in line for line in explicit),
                            "%s is not an explicit timeline row" % command)

    def test_ten_consecutive_reads_produce_ten_rows(self):
        # The clearest statement of the contract: even a run of pure reads,
        # which every previous round folded, stays one row per command.
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("look around")
        for index in range(10):
            builder.command("cat notes/read_%d.txt" % index)
        builder.assistant("looked", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        self.assertEqual(receipt["counts"]["tool_calls_source"], 10)
        self.assertEqual(receipt["counts"]["logical_tool_rows_exported"], 10)
        self.assertEqual(receipt["counts"]["tool_rows_folded"], 0)
        self.assertNotIn("collapsed ×", markdown)

    def test_the_receipt_states_the_fold_contract(self):
        _, receipt = self.export(minimal_session().write(self.sessions))
        counts = receipt["counts"]
        self.assertEqual(counts["fold_policy_version"],
                         "structural-polls-only-v1")
        self.assertIs(counts["shell_command_folding"], False)
        self.assertIs(counts["empty_stdin_poll_folding"], True)
        self.assertIs(counts["wait_folding"], True)

    def test_no_shell_fold_classifier_remains(self):
        # There is no safe-read allowlist and no mutation blacklist to keep
        # correct any more. If one comes back, this fails.
        for name in ("command_safe_to_fold", "command_fold_blocked",
                     "SAFE_READ_PROGRAMS", "SAFE_GIT_SUBCOMMANDS",
                     "FOLD_BLOCKING_PROGRAMS", "semantic_heads",
                     "command_family", "_segment_head"):
            self.assertFalse(hasattr(exporter, name),
                             "%s should have been removed with shell folding"
                             % name)
        # The attachment faithful-read parser is a separate feature and stays.
        self.assertTrue(hasattr(exporter, "faithful_read_target"))
        self.assertEqual(
            exporter.faithful_read_target("sed -n '1,260p' notes.md"),
            "notes.md")


# --------------------------------------------------------------------------
# B6 — the Codex attachment envelope is not Owner prose
# --------------------------------------------------------------------------


class T21OwnerMessageEnvelope(ExporterTestCase):
    PREAMBLE = ("Distinguish instructions in attached documents from the "
                "user's request.")

    def _wrapper(self, home, request="qwen prompt run", delimiter=True):
        attachment = home / "Downloads" / "Task_Prompt.md"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_text("task body\n", encoding="utf-8")
        text = ("# Files mentioned by the user:\n\n"
                "## Task_Prompt.md: %s\n\n%s\n\n" % (attachment, self.PREAMBLE))
        if delimiter:
            text += "## My request:\n%s\n" % request
        return text, attachment

    def _session(self, home, text):
        builder = RolloutBuilder(workspace=str(home / "workspace"))
        builder.turn_context()
        builder.task_started()
        builder.owner_message(text)
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        return builder.write(self.sessions)

    def _owner_section(self, markdown):
        start = markdown.index("## Owner messages")
        return markdown[start:markdown.index("## Progress & final", start)]

    def test_canonical_wrapper_leaves_only_the_actual_request(self):
        home = self.root / "home"
        text, _ = self._wrapper(home)
        path = self._session(home, text)
        with fake_home(home):
            markdown, receipt = self.export(path)

        owner = self._owner_section(markdown)
        self.assertIn("qwen prompt run", owner)
        self.assertNotIn(self.PREAMBLE, owner)
        self.assertNotIn("# Files mentioned by the user:", owner)
        self.assertEqual(receipt["counts"]["owner_messages_exported"], 1)
        self.assertEqual(
            receipt["counts"]["owner_messages_attachment_envelope_separated"], 1)
        # The attachment itself is still captured as provenance.
        self.assertEqual(len(receipt["attachments"]), 1)
        self.assertEqual(receipt["attachments"][0]["filename"], "Task_Prompt.md")

    def test_ordinary_owner_text_containing_the_delimiter_is_not_stripped(self):
        home = self.root / "home"
        text = ("Please follow the template below when you reply.\n\n"
                "## My request:\nship the release\n")
        path = self._session(home, text)
        with fake_home(home):
            markdown, receipt = self.export(path)
        owner = self._owner_section(markdown)
        self.assertIn("Please follow the template below", owner)
        self.assertIn("ship the release", owner)
        self.assertEqual(
            receipt["counts"]["owner_messages_attachment_envelope_separated"], 0)

    def test_incomplete_wrapper_is_context_not_owner_prose(self):
        home = self.root / "home"
        text, _ = self._wrapper(home, delimiter=False)
        path = self._session(home, text)
        with fake_home(home):
            markdown, receipt = self.export(path)

        self.assertEqual(receipt["counts"]["owner_messages_exported"], 0)
        self.assertEqual(
            receipt["counts"]["attachment_wrappers_unparsed_kept_as_context"], 1)
        self.assertGreaterEqual(
            receipt["counts"]["persisted_user_context_omitted"], 1)
        self.assertIn("No mechanically typed Owner message",
                      self._owner_section(markdown))
        self.assertNotIn(self.PREAMBLE, markdown)
        # Attachment provenance survives the demotion.
        self.assertEqual(len(receipt["attachments"]), 1)

    def test_envelope_parser_refuses_ambiguous_shapes(self):
        self.assertIsNone(exporter.parse_attachment_envelope(""))
        self.assertIsNone(exporter.parse_attachment_envelope("plain request"))
        # Manifest header but no manifest entry.
        self.assertIsNone(exporter.parse_attachment_envelope(
            "# Files mentioned by the user:\n\n## My request:\ngo\n"))
        # Two delimiters: which one ends the envelope is not provable.
        self.assertIsNone(exporter.parse_attachment_envelope(
            "# Files mentioned by the user:\n\n## A: /tmp/a.md\n\n"
            "## My request:\nx\n\n## My request:\ny\n"))
        # Empty request body.
        self.assertIsNone(exporter.parse_attachment_envelope(
            "# Files mentioned by the user:\n\n## A: /tmp/a.md\n\n"
            "## My request:\n\n"))


# --------------------------------------------------------------------------
# B7 — snapshot completeness is proven, never assumed
# --------------------------------------------------------------------------


class T22SnapshotCompleteness(ExporterTestCase):
    BODY = "\n".join("line %03d" % n for n in range(1, 61)) + "\n"

    def _session(self, home, reads, create_file=True, body=None):
        attachment = home / "Downloads" / "Task_Prompt.md"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        if create_file:
            attachment.write_text(body or self.BODY, encoding="utf-8")
        builder = RolloutBuilder(workspace=str(home / "workspace"))
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "# Files mentioned by the user:\n\n## Task_Prompt.md: %s\n\n"
            "## My request:\nrun it\n" % attachment
        )
        for command, output in reads(attachment):
            builder.command(command, output=output)
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        return builder.write(self.sessions)

    def _entry(self, home, path):
        with fake_home(home):
            _, receipt = self.export(path)
        self.assertEqual(len(receipt["attachments"]), 1)
        return receipt["attachments"][0]

    def test_full_line_range_reconstruction_is_complete(self):
        home = self.root / "home"
        lines = self.BODY.splitlines(True)
        path = self._session(home, lambda a: [
            ("sed -n '1,30p' %s" % a, "".join(lines[:30])),
            ("sed -n '31,60p' %s" % a, "".join(lines[30:])),
        ])
        entry = self._entry(home, path)
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_COMPLETE)
        self.assertIn("byte-identical", entry["snapshot_completeness_basis"])

    def test_persisted_whole_file_read_is_complete_without_the_file(self):
        home = self.root / "home"
        path = self._session(home, lambda a: [("cat %s" % a, self.BODY)],
                             create_file=False)
        entry = self._entry(home, path)
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_COMPLETE)
        self.assertFalse(entry["current_file_present"])

    def test_head_only_read_without_the_file_is_not_complete(self):
        home = self.root / "home"
        lines = self.BODY.splitlines(True)
        path = self._session(home, lambda a: [
            ("head -n 5 %s" % a, "".join(lines[:5])),
        ], create_file=False)
        entry = self._entry(home, path)
        self.assertNotEqual(entry["snapshot_complete"],
                            exporter.SNAPSHOT_COMPLETE)
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_UNPROVEN)

    def test_middle_range_only_is_provably_incomplete(self):
        home = self.root / "home"
        lines = self.BODY.splitlines(True)
        path = self._session(home, lambda a: [
            ("sed -n '20,40p' %s" % a, "".join(lines[19:40])),
        ], create_file=False)
        entry = self._entry(home, path)
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_INCOMPLETE)
        self.assertIn("line 20", entry["snapshot_completeness_basis"])

    def test_partial_read_against_a_longer_file_is_incomplete(self):
        home = self.root / "home"
        lines = self.BODY.splitlines(True)
        path = self._session(home, lambda a: [
            ("head -n 10 %s" % a, "".join(lines[:10])),
        ])
        entry = self._entry(home, path)
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_INCOMPLETE)
        self.assertIn("10 of 60", entry["snapshot_completeness_basis"])
        self.assertFalse(entry["snapshot_matches_current_file"])

    def test_export_time_reread_never_claims_send_time_identity(self):
        home = self.root / "home"
        path = self._session(home, lambda a: [])  # no persisted read at all
        entry = self._entry(home, path)
        self.assertEqual(entry["snapshot_source"], "current_file_at_export_time")
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_UNPROVEN)
        self.assertIn("no persisted digest",
                      entry["snapshot_completeness_basis"])

    def test_unreadable_current_attachment_preserves_persisted_snapshot(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "Task_Prompt.md"
        path = self._session(home, lambda a: [("cat %s" % a, self.BODY)])
        original_sha256_file = exporter.sha256_file

        def deny_attachment_hash(candidate):
            if Path(candidate) == attachment:
                raise PermissionError(
                    "permission denied: /private/failure/current-attachment.md"
                )
            return original_sha256_file(candidate)

        with fake_home(home), mock.patch.object(
            exporter, "sha256_file", side_effect=deny_attachment_hash
        ):
            markdown, receipt = self.export(path)

        entry = receipt["attachments"][0]
        self.assertTrue(entry["current_file_present"])
        self.assertEqual(entry["current_file_access"],
                         "present_but_unreadable")
        self.assertEqual(entry["snapshot_source"], "persisted_tool_output")
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_COMPLETE)
        self.assertIn("line 001", markdown)
        self.assertNotIn("current-attachment.md", markdown)
        self.assertNotIn("current-attachment.md", json.dumps(receipt))

    def test_unreadable_current_attachment_without_persisted_snapshot_is_unproven(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "Task_Prompt.md"
        path = self._session(home, lambda a: [])
        original_sha256_file = exporter.sha256_file

        def deny_attachment_hash(candidate):
            if Path(candidate) == attachment:
                raise OSError(
                    "access unavailable: /private/failure/current-attachment.md"
                )
            return original_sha256_file(candidate)

        with fake_home(home), mock.patch.object(
            exporter, "sha256_file", side_effect=deny_attachment_hash
        ):
            markdown, receipt = self.export(path)

        entry = receipt["attachments"][0]
        self.assertTrue(entry["current_file_present"])
        self.assertEqual(entry["current_file_access"],
                         "present_but_unreadable")
        self.assertEqual(entry["snapshot_source"], "unavailable")
        self.assertEqual(entry["snapshot_complete"], exporter.SNAPSHOT_UNPROVEN)
        self.assertEqual(entry["snapshot_completeness_basis"],
                         "no_snapshot_recovered")
        self.assertNotIn("current-attachment.md", markdown)
        self.assertNotIn("current-attachment.md", json.dumps(receipt))

    def test_partial_snapshot_is_labelled_in_the_body(self):
        home = self.root / "home"
        lines = self.BODY.splitlines(True)
        path = self._session(home, lambda a: [
            ("sed -n '20,40p' %s" % a, "".join(lines[19:40])),
        ], create_file=False)
        with fake_home(home):
            markdown, _ = self.export(path)
        self.assertIn("Partial persisted snapshot", markdown)
        self.assertNotIn("#### Task-definition snapshot", markdown)
        self.assertIn("line 020", markdown)  # still shown, just not claimed


# --------------------------------------------------------------------------
# B8 — the stability gate brackets rendering, not just parsing
# --------------------------------------------------------------------------


class T23PostRenderStabilityGate(ExporterTestCase):
    def test_a_source_change_after_the_final_render_blocks_publication(self):
        path = minimal_session().write(self.sessions)
        original = exporter.render_markdown
        state = {"calls": 0}

        def mutate_after_final_render(context):
            markdown = original(context)
            state["calls"] += 1
            if state["calls"] == 2:
                # The rollout moves once the export is fully rendered. The
                # first candidate read the post identity before this point and
                # would have published anyway.
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write('{"type":"event_msg","payload":'
                                 '{"type":"task_started"}}\n')
            return markdown

        exporter.render_markdown = mutate_after_final_render
        try:
            code = exporter.main(["--rollout", str(path), "--output-dir",
                                  str(self.output), "--no-git-probe", "--quiet"])
        finally:
            exporter.render_markdown = original

        self.assertEqual(code, 2)
        self.assertEqual(state["calls"], 2)
        self.assertEqual(list(self.output.glob("*.md")), [])
        blocked = sorted(self.output.glob("*.blocked.receipt.json"))
        self.assertEqual(len(blocked), 1)
        receipt = json.loads(blocked[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["export_status"],
                         exporter.STATUS_BLOCKED_SOURCE)
        self.assertTrue(receipt["source"]["source_changed_during_export"])
        self.assertNotEqual(receipt["source"]["rollout_sha256_pre"],
                            receipt["source"]["rollout_sha256_post"])

    def test_a_stable_source_records_the_gate_it_passed(self):
        path = minimal_session().write(self.sessions)
        _, receipt = self.export(path)
        self.assertEqual(receipt["source"]["rollout_sha256_pre"],
                         receipt["source"]["rollout_sha256_post"])
        self.assertFalse(receipt["source"]["source_changed_during_export"])
        self.assertEqual(receipt["source"]["stability_gate"],
                         "post_render_immediately_before_publication")




# --------------------------------------------------------------------------
# Fix 5 — write_stdin fidelity
#
# The parser recognised `tools.write_stdin({...})` but recorded only the
# session id, so every stdin action folded as if it were a poll. The real
# acceptance fixture contains one write of "\x03" — a Ctrl-C — which that
# treatment hid inside a collapsed window.
# --------------------------------------------------------------------------


class T24StdinFidelity(ExporterTestCase):
    def _session(self, *stdins):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("drive the process")
        for chars in stdins:
            builder.stdin(chars)
        builder.assistant("driven", phase="final_answer")
        builder.task_complete()
        return builder.write(self.sessions)

    def test_a_proven_empty_write_is_a_poll_and_folds(self):
        markdown, receipt = self.export(self._session("", "", "", ""))
        counts = receipt["counts"]
        self.assertEqual(counts["stdin_empty_polls"], 4)
        self.assertEqual(counts["stdin_non_empty_inputs"], 0)
        self.assertEqual(counts["fold_windows"], 1)
        self.assertEqual(counts["tool_rows_folded"], 4)
        self.assertIn("collapsed ×4 — write_stdin poll ×4", markdown)

    def test_non_empty_input_is_explicit_with_an_escaped_preview(self):
        markdown, receipt = self.export(
            self._session("y\n", "\u0003", "git commit -m x\n"))
        counts = receipt["counts"]
        self.assertEqual(counts["stdin_non_empty_inputs"], 3)
        self.assertEqual(counts["stdin_empty_polls"], 0)
        self.assertEqual(counts["tool_rows_folded"], 0)
        self.assertNotIn("collapsed ×", markdown)
        # Control characters are escaped so one row stays one line.
        self.assertIn(r'input="y\n" (2 chars)', markdown)
        self.assertIn(r'input="\x03" (1 chars)', markdown)
        self.assertIn(r'input="git commit -m x\n" (16 chars)', markdown)
        self.assertNotIn("\x03", markdown.replace("\\x03", ""))

    def test_unproven_input_is_never_treated_as_an_empty_poll(self):
        for args in ({"session_id": "s1"},
                     {"session_id": "s1", "chars": None},
                     {"session_id": "s1", "chars": 3}):
            chars, state = exporter.decode_stdin_chars(args, False)
            self.assertIsNone(chars)
            self.assertEqual(state, "UNKNOWN")
        # A template-substituted object cannot prove what was sent either.
        self.assertEqual(
            exporter.decode_stdin_chars({"chars": "y"}, True)[1], "UNKNOWN")
        self.assertEqual(exporter.decode_stdin_chars({"chars": ""}, False),
                         ("", "EMPTY"))

    def test_an_unresolved_stdin_row_is_explicit_and_says_so(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("drive the process")
        for _ in range(3):
            builder.stdin_raw('{"session_id": "s1", "chars": `${answer}`}')
        builder.assistant("driven", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        self.assertEqual(receipt["counts"]["stdin_unproven_inputs"], 3)
        self.assertEqual(receipt["counts"]["stdin_empty_polls"], 0)
        self.assertEqual(receipt["counts"]["tool_rows_folded"], 0)
        self.assertNotIn("collapsed ×", markdown)
        self.assertIn("input not mechanically decoded", markdown)

    def test_stdin_input_goes_through_the_same_redaction_as_everything_else(self):
        markdown, receipt = self.export(
            # public-hygiene: synthetic-credential-fixture
            self._session("ghp_ABCDEFGHIJKLMNOPQRSTUV\n"))
        payload = markdown + json.dumps(receipt)
        # public-hygiene: synthetic-credential-fixture
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUV", payload)
        self.assertEqual(receipt["counts"]["stdin_non_empty_inputs"], 1)
        self.assertEqual(receipt["counts"]["tool_rows_folded"], 0)
        # The row survives as evidence that input was sent.
        self.assertIn("write_stdin session=", markdown)

    def test_the_preview_is_bounded(self):
        markdown, _ = self.export(self._session("a" * 500))
        self.assertIn("(500 chars)", markdown)
        for line in markdown.splitlines():
            if "write_stdin" in line:
                self.assertLess(len(line), 400)


# --------------------------------------------------------------------------
# Fix 5 — polls still fold, commands never join them
# --------------------------------------------------------------------------


class T25PollFoldingAndCommandIsolation(ExporterTestCase):
    def test_wait_rows_still_fold(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("wait for the build")
        for _ in range(5):
            builder.wait()
        builder.assistant("built", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["counts"]["fold_windows"], 1)
        self.assertEqual(receipt["counts"]["wait_actions"], 5)
        self.assertIn("collapsed ×5 — wait ×5", markdown)

    def test_waits_and_empty_polls_fold_together(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("drive and wait")
        for _ in range(3):
            builder.wait()
            builder.stdin("")
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(receipt["counts"]["fold_windows"], 1)
        self.assertEqual(receipt["counts"]["tool_rows_folded"], 6)
        self.assertIn("collapsed ×6 — wait ×3, write_stdin poll ×3", markdown)

    def test_no_fold_window_may_absorb_a_command_row(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("mixed timeline")
        commands = ("cat notes.md", "git status --short",
                    "sudo -u root rm -rf /tmp/x", "pwd")
        for command in commands:
            for _ in range(3):
                builder.wait()
                builder.stdin("")
            builder.command(command)
        for _ in range(3):
            builder.wait()
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        markdown, receipt = self.export(builder.write(self.sessions))

        counts = receipt["counts"]
        # Four command rows survive; every poll run around them collapses.
        self.assertEqual(counts["tool_calls_source"], 31)
        self.assertEqual(counts["fold_windows"], 5)
        self.assertEqual(counts["logical_tool_rows_exported"], 9)
        collapsed = "\n".join(line for line in markdown.splitlines()
                               if "collapsed ×" in line)
        for command in commands:
            row = "`%s`" % command.replace("/tmp/", "$TMP/")
            self.assertNotIn(row, collapsed)
            self.assertIn(row, markdown)
        self.assertNotIn("command ×", collapsed)


# --------------------------------------------------------------------------
# P10 — durable CommandExecution accounting
#
# T1-T8 below cover the new correctness surface. Existing T20, T24/T25,
# T1-T3/T11/T12/T19/T21-T23 continue to cover requested regressions T9-T12:
# no shell folding, stdin/wait fidelity, owner/final/reasoning/attachments, and
# source-stability/blocked behavior.
# --------------------------------------------------------------------------


class T26CommandExecutionAccounting(ExporterTestCase):
    def _start(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message("account for every command execution")
        return builder

    def _finish(self, builder):
        builder.assistant("accounted", phase="final_answer")
        builder.task_complete()
        return self.export(builder.write(self.sessions))

    def test_t1_exact_pair_renders_once_and_closes_accounting(self):
        builder = self._start()
        builder.command("printf exact-pair")
        markdown, receipt = self._finish(builder)
        counts = receipt["counts"]
        self.assertEqual(counts["command_execution_records_source"], 1)
        self.assertEqual(counts["command_execution_records_paired"], 1)
        self.assertEqual(counts["command_execution_records_unpaired"], 0)
        self.assertEqual(counts["command_execution_records_rendered_unpaired"], 0)
        self.assertTrue(counts["command_execution_accounting_closed"])
        self.assertTrue(counts["command_execution_rendering_closed"])
        self.assertEqual(markdown.count("printf exact-pair"), 1)
        self.assertNotIn("source=CommandExecution / unpaired", markdown)

    def test_t2_dynamic_invocation_keeps_generic_row_and_actual_execution(self):
        builder = self._start()
        call_id = builder.tool_invocation(
            "const rs = await Promise.all(files.map(p => "
            "tools.exec_command({cmd: p})));"
        )
        builder.command_execution("find reports -type f -print")
        builder.tool_output(call_id)
        markdown, receipt = self._finish(builder)
        counts = receipt["counts"]
        self.assertIn("tool exec", markdown)
        self.assertIn("find reports -type f -print", markdown)
        self.assertIn("source=CommandExecution / unpaired", markdown)
        self.assertEqual(counts["command_execution_records_source"], 1)
        self.assertEqual(counts["command_execution_records_paired"], 0)
        self.assertEqual(counts["command_execution_records_unpaired"], 1)
        self.assertEqual(counts["command_execution_records_rendered_unpaired"], 1)

    def test_t3_dynamic_fanout_renders_every_execution_in_persisted_order(self):
        builder = self._start()
        call_id = builder.tool_invocation(
            "const rs = await Promise.all(files.map(p => "
            "tools.exec_command({cmd: build(p)})));"
        )
        commands = [
            "find alpha -type f -print",
            "sed -n '1,20p' beta.txt",
            "wc -l gamma.txt",
        ]
        for command in commands:
            builder.command_execution(command)
        builder.tool_output(call_id)
        markdown, receipt = self._finish(builder)
        positions = [markdown.index(command) for command in commands]
        self.assertEqual(positions, sorted(positions))
        counts = receipt["counts"]
        self.assertEqual(counts["command_execution_records_source"], 3)
        self.assertEqual(counts["command_execution_records_unpaired"], 3)
        self.assertEqual(counts["command_execution_records_rendered_unpaired"], 3)

    def test_t4_mixed_paired_and_unpaired_records_close_exactly(self):
        builder = self._start()
        builder.command("git rev-parse HEAD")
        call_id = builder.tool_invocation(
            "const rs = await Promise.all(queue.map(job => "
            "tools.exec_command({cmd: job.command})));"
        )
        builder.command_execution("python3 tools/check_one.py")
        builder.command_execution("python3 tools/check_two.py")
        builder.tool_output(call_id)
        markdown, receipt = self._finish(builder)
        counts = receipt["counts"]
        self.assertEqual(counts["command_execution_records_source"], 3)
        self.assertEqual(counts["command_execution_records_paired"], 1)
        self.assertEqual(counts["command_execution_records_unpaired"], 2)
        self.assertEqual(counts["timeline_rows_exported"], 4)
        self.assertIn("Durable CommandExecution records: 3 = 1 paired + 2 unpaired",
                      markdown)

    def test_t5_unpaired_failure_is_visible_and_counted(self):
        builder = self._start()
        call_id = builder.tool_invocation(
            "const rs = await Promise.all(tasks.map(run => "
            "tools.exec_command({cmd: run})));"
        )
        builder.command_execution("curl -fsSL https://example.invalid/input",
                                  exit_code=7, stderr="connection failed")
        builder.tool_output(call_id, output="connection failed\n")
        markdown, receipt = self._finish(builder)
        counts = receipt["counts"]
        self.assertEqual(counts["command_execution_records_unpaired_failed"], 1)
        self.assertEqual(counts["failed_tool_actions"], 0)
        self.assertEqual(counts["failed_actions"], 1)
        self.assertIn("**FAILED**", markdown)
        self.assertIn("exit=7", markdown)
        self.assertIn("status=failed", markdown)
        self.assertIn("stderr: `connection failed`", markdown)

    def test_t6_template_invocation_never_receives_execution_status(self):
        builder = self._start()
        call_id = builder.tool_invocation(
            "const r = await tools.exec_command({cmd: "
            "`jq -r '.name' '${f}' | sort -u`});"
        )
        builder.command_execution("jq -r '.name' 'alpha.json' | sort -u")
        builder.command_execution("jq -r '.name' 'beta.json' | sort -u")
        builder.tool_output(call_id)
        markdown, receipt = self._finish(builder)
        template_row = next(line for line in markdown.splitlines()
                            if "${f}" in line)
        self.assertIn("source=tool invocation / template-unresolved", template_row)
        self.assertNotIn("exit=", template_row)
        self.assertNotIn("status=", template_row)
        self.assertIn("alpha.json", markdown)
        self.assertIn("beta.json", markdown)
        counts = receipt["counts"]
        self.assertEqual(counts["exit_status_resolved"], "0/0")
        self.assertEqual(counts["command_execution_records_unpaired"], 2)

    def test_t7_actions_and_executions_share_persisted_sequence_order(self):
        builder = self._start()
        first = builder.tool_invocation(
            "const r = await tools.exec_command({cmd: dynamicOne});"
        )
        builder.command_execution("printf first-actual")
        builder.tool_output(first)
        builder.wait()
        second = builder.tool_invocation(
            "const r = await tools.exec_command({cmd: dynamicTwo});"
        )
        builder.command_execution("printf second-actual")
        builder.tool_output(second)
        markdown, _ = self._finish(builder)
        first_position = markdown.index("printf first-actual")
        wait_position = markdown.index("`wait`")
        second_position = markdown.index("printf second-actual")
        self.assertLess(first_position, wait_position)
        self.assertLess(wait_position, second_position)

    def test_t8_unpaired_text_uses_existing_privacy_and_bounds(self):
        builder = self._start()
        call_id = builder.tool_invocation(
            "const r = await tools.exec_command({cmd: dynamicSecret});"
        )
        # Build the synthetic credential at runtime so the review patch itself
        # never contains a credential-shaped byte sequence.
        secret = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUV"
        builder.command_execution(
            "curl --token %s https://example.invalid" % secret,
            exit_code=1,
            stderr="token=%s" % secret,
            cwd=WORKSPACE + "/private",
        )
        builder.tool_output(call_id)
        with fake_home("/Users/testowner"):
            markdown, receipt = self._finish(builder)
        payload = markdown + json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(secret, payload)
        self.assertNotIn("/Users/testowner", payload)
        self.assertIn("<REDACTED:credential>", markdown)
        self.assertIn("cwd=file://$WORKSPACE/private", markdown)
        self.assertEqual(receipt["privacy"]["sanitization_rescan_residuals"], {})


# --------------------------------------------------------------------------
# Package v2 — recognizable, file-bearing, per-session package
# --------------------------------------------------------------------------


class T27ConversationPackageV2(ExporterTestCase):
    def _complete(self, request, session_id=SESSION_A, final="done"):
        builder = RolloutBuilder(session_id=session_id)
        builder.turn_context()
        builder.task_started()
        builder.owner_message(request)
        builder.assistant(final, phase="final_answer")
        builder.task_complete()
        return builder

    def _wrapper(self, path, request, display_name=None):
        return (
            "# Files mentioned by the user:\n\n"
            "## %s: %s\n\n## My request:\n%s\n"
            % (display_name or Path(path).name, path, request)
        )

    def test_human_title_preserves_cjk(self):
        path = self._complete("整理示例阅读器后台引擎证据").write(self.sessions)
        _, receipt = self.export(path)
        package = self.package_dirs()[0]
        self.assertTrue(package.name.startswith("整理示例阅读器后台引擎证据__"))
        self.assertEqual(receipt["package"]["display_title"],
                         "整理示例阅读器后台引擎证据")

    def test_generic_run_uses_attachment_stem(self):
        attachment = Path("/Users/testowner/Downloads/示例阅读器实施提示.md")
        request = self._wrapper(attachment, "run")
        with fake_home("/Users/testowner"):
            _, receipt = self.export(
                self._complete(request).write(self.sessions)
            )
        self.assertEqual(receipt["package"]["display_title"], "示例阅读器实施提示")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_attachment_filename_stem")

    def test_meaningful_owner_request_wins(self):
        attachment = Path("/Users/testowner/Downloads/Generic_Prompt.md")
        request = self._wrapper(attachment, "实现会话文件包第二版")
        with fake_home("/Users/testowner"):
            _, receipt = self.export(
                self._complete(request).write(self.sessions)
            )
        self.assertEqual(receipt["package"]["display_title"],
                         "实现会话文件包第二版")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_same_session_later_turn_renames_once_to_the_new_task(self):
        """A continued session is renamed to its newest task, exactly once.

        The Owner continues a conversation in order to do new work, so the
        newest completed task is what makes the package findable. The
        immutable identity suffix does not move, and re-exporting the same
        state again changes nothing further.
        """
        builder = self._complete("同一会话第一轮任务")
        path = builder.write(self.sessions)
        self.export(path)
        first = [item.name for item in self.package_dirs()]
        self.assertEqual(len(first), 1)
        suffix = first[0].split("__", 1)[1]

        builder.turn_context(TURN_2)
        builder.task_started(TURN_2)
        builder.owner_message("第二轮才是当前任务", TURN_2)
        builder.assistant("refreshed", "final_answer", TURN_2)
        builder.task_complete(TURN_2)
        builder.write(self.sessions, filename=path.name)
        _, receipt = self.export(path)
        self.assertEqual(receipt["package"]["package_dirname"],
                         "第二轮才是当前任务__%s" % suffix)
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

        # Re-exporting the identical state changes nothing further. Moving
        # the superseded directory is an external relocation step, which
        # this direct-export path deliberately does not perform.
        after = [item.name for item in self.package_dirs()]
        self.export(path)
        self.assertEqual([item.name for item in self.package_dirs()], after)

    def test_same_title_different_session_no_collision(self):
        self.export(self._complete("相同标题", SESSION_A).write(self.sessions))
        self.export(self._complete("相同标题", SESSION_B).write(self.sessions))
        packages = self.package_dirs()
        self.assertEqual(len(packages), 2)
        self.assertNotEqual(packages[0].name, packages[1].name)

    def test_finder_visible_prefix_is_human_meaning(self):
        path = self._complete("Finder 先看到这段中文").write(self.sessions)
        self.export(path)
        name = self.package_dirs()[0].name
        self.assertEqual(name.split("__", 1)[0], "Finder 先看到这段中文")
        self.assertFalse(name.startswith(SESSION_A[:8]))

    def test_complete_persisted_attachment_materializes_without_current_file(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "完整提示.md"
        body = "第一行\n第二行\n"
        builder = self._complete(self._wrapper(attachment, "run"))
        builder.records.pop()  # task_complete must follow the persisted read
        builder.command("cat %s" % attachment, output=body)
        builder.task_complete()
        with fake_home(home):
            self.export(builder.write(self.sessions))
        package = self.package_dirs()[0]
        self.assertEqual((package / exporter.ATTACHMENTS_DIRNAME /
                          "完整提示.md").read_bytes(),
                         body.encode("utf-8"))
        row = self.package_manifest()["attachments"][0]
        self.assertEqual(row["materialization_source"],
                         "persisted_faithful_snapshot")
        self.assertTrue(row["completeness"])

    def test_readable_binary_attachment_copies_exact_bytes(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "fixture.bin"
        attachment.parent.mkdir(parents=True)
        payload = b"\x00\xff\x10binary\x00"
        attachment.write_bytes(payload)
        builder = self._complete(self._wrapper(attachment, "run"))
        with fake_home(home):
            self.export(builder.write(self.sessions))
        package = self.package_dirs()[0]
        self.assertEqual((package / exporter.ATTACHMENTS_DIRNAME /
                          "fixture.bin").read_bytes(),
                         payload)
        row = self.package_manifest()["attachments"][0]
        self.assertEqual(row["materialization_source"], "current_exact_file")

    def test_partial_attachment_never_masquerades_as_original(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "Task.md"
        builder = self._complete(self._wrapper(attachment, "run"))
        builder.records.pop()
        builder.command("sed -n '20,40p' %s" % attachment,
                        output="partial only\n")
        builder.task_complete()
        with fake_home(home):
            self.export(builder.write(self.sessions))
        package = self.package_dirs()[0]
        self.assertFalse((package / exporter.ATTACHMENTS_DIRNAME /
                          "Task.md").exists())
        partial = (package / exporter.ATTACHMENTS_DIRNAME / "_partial" /
                   "Task.md.partial.txt")
        self.assertEqual(partial.read_text("utf-8"), "partial only\n")
        self.assertFalse(self.package_manifest()["package_complete"])

    def test_current_blocked_privacy_rejects_legacy_carry_forward(self):
        current = {
            "filename": "Task.md",
            "path_normalized": "$ATTACHMENT/Task.md",
            "provenance": "owner_message_file_manifest",
            "snapshot_source": "persisted_tool_output",
            "snapshot_complete": exporter.SNAPSHOT_COMPLETE,
            "materialization_status": "blocked_privacy",
            "materialization_source": "persisted_faithful_snapshot",
            "payload_complete": False,
        }
        carry = {
            "kind": "attachment",
            "filename": "Task.md",
            "path_normalized": "$ATTACHMENT/Task.md",
            "provenance": "owner_message_file_manifest",
            "materialization_status": "ready",
            "materialization_source": "managed_v2_carry_forward",
            "payload_complete": True,
            "_materialized_bytes": b"old attested payload",
            "_preferred_member": "%s/Task.md" %
                                 exporter.ATTACHMENTS_DIRNAME,
            "_managed_v2_carry_forward": True,
        }

        with self.assertRaises(exporter.ExportBlocked) as raised:
            exporter.apply_managed_v2_payload_carry_forward(
                [current], [], [carry]
            )

        self.assertEqual(raised.exception.status,
                         exporter.STATUS_BLOCKED_PRIVACY)
        self.assertEqual(current["materialization_status"], "blocked_privacy")
        self.assertNotIn("_materialized_bytes", current)

    def test_unsafe_recovered_partial_rejects_legacy_carry_forward(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "Task.md"
        unsafe_text = "unsafe-private-marker"
        builder = self._complete(self._wrapper(attachment, "run"))
        builder.records.pop()
        builder.command(
            "sed -n '20,40p' %s" % attachment,
            output="partial %s\n" % unsafe_text,
        )
        builder.task_complete()
        path = builder.write(self.sessions)
        with fake_home(home):
            privacy = exporter.Privacy()
            model = exporter.RolloutModel(privacy)
            model.load(path)
            exporter.configure_privacy(privacy, model)
            with mock.patch.object(
                exporter, "sanitization_rescan",
                return_value={"synthetic_privacy_refusal": 1},
            ):
                current = exporter.build_attachments(
                    model, privacy, exporter.DEFAULT_ATTACHMENT_SNAPSHOT_BYTES
                )[0]
        self.assertEqual(current["snapshot_complete"],
                         exporter.SNAPSHOT_INCOMPLETE)
        self.assertEqual(current["materialization_status"], "blocked_privacy")
        self.assertEqual(current["materialization_source"],
                         "persisted_partial_snapshot")
        carry = {
            "kind": "attachment",
            "filename": current["filename"],
            "path_normalized": current["path_normalized"],
            "provenance": current["provenance"],
            "materialization_status": "ready",
            "materialization_source": "managed_v2_carry_forward",
            "payload_complete": True,
            "_materialized_bytes": b"old attested payload",
            "_preferred_member": "%s/Task.md" %
                                 exporter.ATTACHMENTS_DIRNAME,
            "_managed_v2_carry_forward": True,
        }

        with self.assertRaises(exporter.ExportBlocked) as raised:
            exporter.apply_managed_v2_payload_carry_forward(
                [current], [], [carry]
            )

        self.assertEqual(raised.exception.status,
                         exporter.STATUS_BLOCKED_PRIVACY)
        self.assertEqual(current["materialization_status"], "blocked_privacy")
        self.assertIsNone(current["materialized_member"])

    def test_unavailable_attachment_manifests_incomplete(self):
        home = self.root / "home"
        attachment = home / "Downloads" / "missing.pdf"
        with fake_home(home):
            self.export(self._complete(
                self._wrapper(attachment, "run")
            ).write(self.sessions))
        manifest = self.package_manifest()
        self.assertFalse(manifest["package_complete"])
        self.assertEqual(manifest["attachments"][0]["materialization_status"],
                         "unavailable")

    def test_explicit_artifact_materializes(self):
        artifact = self.root / "review.zip"
        artifact.write_bytes(b"PK\x03\x04review")
        self.export(self._complete("collect it").write(self.sessions),
                    "--artifact", "review=%s" % artifact)
        package = self.package_dirs()[0]
        self.assertEqual(artifact_path(package, "review.zip").read_bytes(),
                         artifact.read_bytes())

    def test_auto_final_deliverable_path_materializes_when_proven(self):
        artifact = self.root / "result.json"
        artifact.write_text('{"ok": true}\n', encoding="utf-8")
        final = "交付：[result.json](%s)" % artifact
        self.export(self._complete("produce result", final=final).write(self.sessions))
        package = self.package_dirs()[0]
        self.assertEqual(artifact_path(package, "result.json").read_bytes(),
                         artifact.read_bytes())
        self.assertEqual(self.package_manifest()["artifacts"][0]["provenance"],
                         "assistant_final_exact_path_with_mtime")

    def test_same_size_fresh_replacement_receipt_matches_opened_object(self):
        original = b"PK\x03\x04AAAAAAAAAAAA"
        replacement = b"PK\x03\x04BBBBBBBBBBBB"
        self.assertEqual(len(original), len(replacement))
        artifact = self.root / "same-size-result.zip"
        artifact.write_bytes(original)
        final = "交付：[same-size-result.zip](<%s>)" % artifact
        rollout = self._complete(
            "produce same-size replacement result", final=final
        ).write(self.sessions)
        replacement_path = self.root / "same-size-fresh-replacement.zip"
        replacement_path.write_bytes(replacement)
        original_open = os.open
        fired = {"value": False}

        def replace_then_open(candidate, flags, *args, **kwargs):
            if Path(candidate) == artifact and not fired["value"]:
                fired["value"] = True
                os.replace(replacement_path, artifact)
            return original_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(os, "open", replace_then_open):
            _, receipt = self.export(rollout)

        self.assertTrue(fired["value"])
        package = self.package_dirs()[0]
        stored = (artifact_path(package, artifact.name)).read_bytes()
        digest = hashlib.sha256(stored).hexdigest()
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(stored, replacement)
        self.assertEqual(row["materialization_status"], "materialized")
        self.assertEqual(row["materialized_bytes"], len(stored))
        self.assertEqual(row["materialized_sha256"], digest)
        self.assertNotEqual(
            row["materialized_sha256"], hashlib.sha256(original).hexdigest()
        )
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            0,
        )
        manifest_row = self.package_manifest()["artifacts"][0]
        self.assertEqual(manifest_row["sha256"], digest)
        self.assertTrue(exporter.package_files_current(package))

    def test_same_size_old_mtime_replacement_is_retryable_not_materialized(self):
        original = b"PK\x03\x04AAAAAAAAAAAAAA"
        replacement = b"PK\x03\x04BBBBBBBBBBBBBB"
        self.assertEqual(len(original), len(replacement))
        artifact = self.root / "same-size-old-result.zip"
        artifact.write_bytes(original)
        final = "交付：[same-size-old-result.zip](<%s>)" % artifact
        rollout = self._complete(
            "produce object-bound result", final=final
        ).write(self.sessions)
        replacement_path = self.root / "same-size-old-replacement.zip"
        replacement_path.write_bytes(replacement)
        os.utime(replacement_path, (0, 0))
        original_open = os.open
        fired = {"value": False}

        def replace_then_open(candidate, flags, *args, **kwargs):
            if Path(candidate) == artifact and not fired["value"]:
                fired["value"] = True
                os.replace(replacement_path, artifact)
            return original_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(os, "open", replace_then_open):
            _, receipt = self.export(rollout)

        self.assertTrue(fired["value"])
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(
            row["materialization_status"],
            exporter.FINAL_ARTIFACT_MATERIALIZATION_MTIME_TOO_OLD,
        )
        self.assertIs(row["payload_complete"], False)
        self.assertIsNone(row["materialized_member"])
        self.assertIsNone(row["materialized_sha256"])
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            1,
        )
        package = self.package_dirs()[0]
        self.assertFalse(
            bool(list((package / exporter.ARTIFACTS_DIRNAME).rglob(artifact.name)))
        )
        for child in package.rglob("*"):
            if child.is_file():
                self.assertNotEqual(child.read_bytes(), replacement)

    def test_exact_final_symlink_is_permanently_rejected(self):
        target = self.root / "symlink-target.zip"
        target.write_bytes(b"PK\x03\x04symlink-target")
        artifact = self.root / "final-symlink.zip"
        artifact.symlink_to(target)
        state, _ = exporter._classify_final_answer_artifact_candidate(
            artifact, exporter._iso_epoch("2026-08-25T10:00:00.000Z")
        )
        self.assertEqual(state, exporter.FINAL_ARTIFACT_REJECT_NON_REGULAR)
        final = "交付：[final-symlink.zip](<%s>)" % artifact
        _, receipt = self.export(
            self._complete("reject final symlink", final=final)
            .write(self.sessions)
        )

        self.assertEqual(receipt["artifacts"]["artifact_count"], 0)
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            0,
        )
        self.assertEqual(
            receipt["artifacts"]["missing_exact_final_path_candidates"], 0
        )
        package = self.package_dirs()[0]
        self.assertFalse(
            bool(list((package / exporter.ARTIFACTS_DIRNAME).rglob(artifact.name)))
        )

    def test_symlink_retarget_at_open_never_reads_target_bytes(self):
        original = b"PK\x03\x04regular-original"
        target_payload = b"PK\x03\x04symlink-target-secret"
        artifact = self.root / "retargeted-final.zip"
        artifact.write_bytes(original)
        target = self.root / "retarget-target.zip"
        target.write_bytes(target_payload)
        final = "交付：[retargeted-final.zip](<%s>)" % artifact
        rollout = self._complete(
            "reject a retarget race", final=final
        ).write(self.sessions)
        original_open = os.open
        fired = {"value": False}

        def retarget_then_open(candidate, flags, *args, **kwargs):
            if Path(candidate) == artifact and not fired["value"]:
                fired["value"] = True
                artifact.unlink()
                artifact.symlink_to(target)
            return original_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(os, "open", retarget_then_open):
            _, receipt = self.export(rollout)

        self.assertTrue(fired["value"])
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(
            row["materialization_status"],
            exporter.FINAL_ARTIFACT_MATERIALIZATION_REJECTED_SYMLINK,
        )
        self.assertIs(row["payload_complete"], False)
        self.assertIsNone(row["materialized_member"])
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            0,
        )
        package = self.package_dirs()[0]
        self.assertFalse(
            bool(list((package / exporter.ARTIFACTS_DIRNAME).rglob(artifact.name)))
        )
        for child in package.rglob("*"):
            if child.is_file():
                self.assertNotEqual(child.read_bytes(), target_payload)

    def test_nonregular_retarget_at_open_is_permanently_rejected(self):
        artifact = self.root / "directory-race.zip"
        artifact.write_bytes(b"PK\x03\x04regular-before-race")
        final = "交付：[directory-race.zip](<%s>)" % artifact
        rollout = self._complete(
            "reject nonregular opened object", final=final
        ).write(self.sessions)
        original_open = os.open
        fired = {"value": False}

        def replace_then_open(candidate, flags, *args, **kwargs):
            if Path(candidate) == artifact and not fired["value"]:
                fired["value"] = True
                artifact.unlink()
                artifact.mkdir()
            return original_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(os, "open", replace_then_open):
            _, receipt = self.export(rollout)

        self.assertTrue(fired["value"])
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(
            row["materialization_status"],
            exporter.FINAL_ARTIFACT_MATERIALIZATION_REJECTED_NON_REGULAR,
        )
        self.assertIs(row["payload_complete"], False)
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            0,
        )

    def test_object_bound_open_stops_when_nofollow_is_unavailable(self):
        artifact = self.root / "nofollow-required.zip"
        artifact.write_bytes(b"PK\x03\x04nofollow")
        with mock.patch.object(os, "O_NOFOLLOW", None):
            with self.assertRaises(exporter.ExportBlocked) as raised:
                exporter._read_opened_final_artifact(
                    str(artifact), 0, artifact.stat().st_size, 1024
                )
        self.assertEqual(raised.exception.status,
                         exporter.STATUS_BLOCKED_PACKAGE)

    def test_opened_identity_requires_exact_integer_ctime_ns(self):
        artifact = self.root / "ctime-required.zip"
        artifact.write_bytes(b"PK\x03\x04ctime")
        observed = artifact.stat()
        identity = exporter._opened_object_identity(observed)
        self.assertEqual(len(identity), 6)
        self.assertEqual(identity[-1], observed.st_ctime_ns)

        class StatWithoutCtime:
            st_dev = observed.st_dev
            st_ino = observed.st_ino
            st_mode = observed.st_mode
            st_size = observed.st_size
            st_mtime = observed.st_mtime
            st_mtime_ns = observed.st_mtime_ns

        with self.assertRaises(exporter.ExportBlocked) as raised:
            exporter._opened_object_identity(StatWithoutCtime())
        self.assertEqual(raised.exception.status,
                         exporter.STATUS_BLOCKED_PACKAGE)
        self.assertIn("exact st_ctime_ns", raised.exception.detail)

    def test_same_inode_same_size_mtime_restored_mutation_is_source_changed(self):
        chunk_bytes = 1024 * 1024
        original = b"A" * (2 * chunk_bytes)
        mutated = b"B" * len(original)
        artifact = self.root / "in-place-mutation.zip"
        artifact.write_bytes(original)
        before = artifact.stat()
        final = "交付：[in-place-mutation.zip](<%s>)" % artifact
        rollout = self._complete(
            "detect in-place mutation", final=final
        ).write(self.sessions)
        original_read = os.read
        artifact_identity = before.st_ino
        fired = {"value": False}
        target_chunks = []

        def read_then_mutate(descriptor, count):
            chunk = original_read(descriptor, count)
            if os.fstat(descriptor).st_ino != artifact_identity:
                return chunk
            target_chunks.append(chunk)
            if not fired["value"] and chunk:
                fired["value"] = True
                writer = os.open(artifact, os.O_WRONLY)
                try:
                    offset = 0
                    while offset < len(mutated):
                        offset += os.write(writer, mutated[offset:])
                    os.fsync(writer)
                finally:
                    os.close(writer)
                os.utime(
                    artifact,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
            return chunk

        with mock.patch.object(os, "read", read_then_mutate):
            _, receipt = self.export(rollout)

        self.assertTrue(fired["value"])
        after = artifact.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(b"".join(target_chunks),
                         original[:chunk_bytes] + mutated[chunk_bytes:])
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(row["materialization_status"], "source_changed")
        self.assertIs(row["payload_complete"], False)
        self.assertIsNone(row["materialized_member"])
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"],
            1,
        )

    def test_dogfood_angle_bracket_absolute_target_materializes(self):
        artifact = self.root / "dogfood-result.zip"
        artifact.write_bytes(b"PK\x03\x04dogfood")
        final = "交付：[dogfood-result.zip](<%s>)" % artifact
        self.export(self._complete("produce dogfood result", final=final)
                    .write(self.sessions))
        package = self.package_dirs()[0]
        self.assertEqual(
            (artifact_path(package, artifact.name)).read_bytes(),
            artifact.read_bytes(),
        )

    def test_backtick_absolute_path_materializes(self):
        artifact = self.root / "backtick-result.json"
        artifact.write_text('{"ok": true}\n', encoding="utf-8")
        final = "交付：`%s`" % artifact
        self.export(self._complete("produce backtick result", final=final)
                    .write(self.sessions))
        package = self.package_dirs()[0]
        self.assertEqual(
            (artifact_path(package, artifact.name)).read_bytes(),
            artifact.read_bytes(),
        )

    def test_directory_unsafe_and_structurally_invalid_paths_are_nonpending(self):
        directory = self.root / "directory-result.zip"
        directory.mkdir()
        unsafe = self.root / "unsafe.bin"
        unsafe.write_bytes(b"not a deliverable")
        final = (
            "directory [d](<%s>) unsafe `%s` relative [r](result.json) "
            "tilde [t](~/result.zip) env [e]($HOME/result.zip) "
            "shell [s]($(pwd)/result.zip) malformed [m](</tmp/result.zip)"
            % (directory, unsafe)
        )
        _, receipt = self.export(
            self._complete("reject unsafe paths", final=final).write(self.sessions)
        )
        self.assertEqual(receipt["artifacts"]["artifact_count"], 0)
        self.assertEqual(
            receipt["artifacts"]["missing_exact_final_path_candidates"], 0
        )
        self.assertEqual(
            receipt["artifacts"]
            ["retryable_exact_final_artifact_candidates"], 0
        )

    def test_unclassified_stat_error_is_incomplete_but_nonpending(self):
        artifact = self.root / "stat-error-result.zip"
        artifact.write_bytes(b"PK\x03\x04result")
        final = "交付：[stat-error-result.zip](<%s>)" % artifact
        original_lstat = Path.lstat

        def fail_stat(candidate, *args, **kwargs):
            if candidate == artifact:
                raise OSError(5, "synthetic raw stat error")
            return original_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, "lstat", fail_stat):
            _, receipt = self.export(
                self._complete("classify stat failure", final=final)
                .write(self.sessions)
            )
        self.assertEqual(receipt["artifacts"]["artifact_count"], 1)
        self.assertEqual(
            receipt["artifacts"]["missing_exact_final_path_candidates"], 0
        )
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(row["materialization_status"], "unavailable")
        self.assertEqual(
            row["provenance"],
            "assistant_final_exact_path_stat_unavailable",
        )
        exported = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn("synthetic raw stat error", exported)
        self.assertNotIn("OSError", exported)

    def test_missing_exact_final_path_is_not_materialized_or_scanned(self):
        missing = self.root / "missing-result.zip"
        final = "交付：[missing](%s)" % missing
        rollout = self._complete("missing result", final=final).write(
            self.sessions
        )
        with mock.patch.object(
            Path, "rglob", side_effect=AssertionError("filesystem scan forbidden")
        ):
            code = exporter.main([
                "--rollout", str(rollout),
                "--output-dir", str(self.output),
                "--no-git-probe", "--quiet",
            ])
        self.assertEqual(code, 0)
        receipt = json.loads(next(
            self.output.rglob(exporter.RECEIPT_FILENAME)
        ).read_text("utf-8"))
        self.assertEqual(receipt["artifacts"]["artifact_count"], 0)
        self.assertEqual(
            receipt["artifacts"]["missing_exact_final_path_candidates"], 1
        )

    def test_path_normalization_is_not_artifact_ownership_evidence(self):
        home = self.root / "home"
        artifact = home / "Downloads" / "normalized-result.zip"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"PK\x03\x04normalized")
        final = "交付：[normalized](<%s>)" % artifact
        with fake_home(home):
            _, receipt = self.export(
                self._complete("normalized result", final=final)
                .write(self.sessions)
            )
        row = receipt["artifacts"]["items"][0]
        self.assertEqual(row["path_normalized"],
                         "$ATTACHMENT/normalized-result.zip")
        self.assertEqual(row["provenance"],
                         "assistant_final_exact_path_with_mtime")

    def test_unproven_final_path_not_copied(self):
        artifact = self.root / "old-result.json"
        artifact.write_text('{"old": true}\n', encoding="utf-8")
        os.utime(artifact, (0, 0))
        final = "旧路径不是本轮交付：[`old-result`](%s)" % artifact
        self.export(self._complete("produce result", final=final).write(self.sessions))
        manifest = self.package_manifest()
        self.assertEqual(manifest["artifacts"], [])
        self.assertEqual(list((self.package_dirs()[0] /
                               exporter.ARTIFACTS_DIRNAME).iterdir()),
                         [self.package_dirs()[0] / exporter.OUTPUT_INDEX_MEMBER])

    def test_auto_final_deliverable_permission_denied_is_unavailable(self):
        home = Path("/Users/testowner")
        artifact = home / "Downloads" / "denied-final.zip"
        final = "交付：[denied-final.zip](%s:12)" % artifact
        original_lstat = Path.lstat

        def deny(candidate, *args, **kwargs):
            if candidate == artifact:
                raise PermissionError(
                    "synthetic denial must never reach exported evidence"
                )
            return original_lstat(candidate, *args, **kwargs)

        with fake_home(home), mock.patch.object(Path, "lstat", deny):
            _, receipt = self.export(
                self._complete("produce protected result", final=final)
                .write(self.sessions)
            )

        manifest = self.package_manifest()
        self.assertFalse(manifest["package_complete"])
        self.assertFalse(receipt["package"]["package_complete"])
        self.assertEqual(len(manifest["artifacts"]), 1)
        row = manifest["artifacts"][0]
        self.assertEqual(row["materialization_status"], "unavailable")
        self.assertFalse(row["completeness"])
        self.assertEqual(
            row["provenance"],
            "assistant_final_exact_path_access_unavailable",
        )
        self.assertEqual(row["source_reference"],
                         "$ATTACHMENT/denied-final.zip")
        self.assertIsNone(row["member_path"])
        self.assertEqual(list((self.package_dirs()[0] /
                               exporter.ARTIFACTS_DIRNAME).iterdir()),
                         [self.package_dirs()[0] / exporter.OUTPUT_INDEX_MEMBER])
        exported = json.dumps(
            {"manifest": manifest, "receipt": receipt}, ensure_ascii=False
        )
        self.assertNotIn(str(home), exported)
        self.assertNotIn("synthetic denial", exported)
        self.assertNotIn("PermissionError", exported)

    def test_unreadable_artifact_manifests_unavailable(self):
        artifact = self.root / "unreadable.zip"
        artifact.write_bytes(b"PK\x03\x04private")
        original = Path.read_bytes

        def deny(candidate):
            if candidate == artifact:
                raise PermissionError("synthetic denial")
            return original(candidate)

        with mock.patch.object(Path, "read_bytes", deny):
            self.export(self._complete("collect it").write(self.sessions),
                        "--artifact", "review=%s" % artifact)
        manifest = self.package_manifest()
        self.assertFalse(manifest["package_complete"])
        self.assertEqual(manifest["artifacts"][0]["materialization_status"],
                         "unavailable")

    def test_normal_package_has_no_persistent_handoff_zip(self):
        self.export(self._complete("package it").write(self.sessions))
        package = self.package_dirs()[0]
        manifest = self.package_manifest()
        receipt = json.loads(
            (package / exporter.RECEIPT_FILENAME).read_text("utf-8")
        )
        self.assertFalse((package / exporter.HANDOFF_ZIP_FILENAME).exists())
        self.assertEqual(manifest["handoff_zip"]["status"],
                         "not_generated_by_default")
        self.assertFalse(manifest["handoff_zip"]["canonical_package_dependency"])
        self.assertEqual(receipt["package"]["handoff_zip_default_generation"],
                         "disabled")
        self.assertEqual(receipt["package"]["handoff_zip_on_demand"],
                         "available")
        self.assertTrue(exporter.package_files_current(package, manifest))

    def test_on_demand_handoff_contains_canonical_members_and_excludes_itself(self):
        self.export(self._complete("package it").write(self.sessions))
        package = self.package_dirs()[0]
        generated = exporter.generate_handoff_zip(package)
        with zipfile.ZipFile(package / exporter.HANDOFF_ZIP_FILENAME) as archive:
            names = archive.namelist()
        self.assertEqual(set(names), {
            exporter.CONVERSATION_FILENAME,
            exporter.RECEIPT_FILENAME,
            exporter.PACKAGE_MANIFEST_FILENAME,
            exporter.OUTPUT_INDEX_MEMBER,
        })
        self.assertNotIn(exporter.HANDOFF_ZIP_FILENAME, names)
        self.assertTrue(all(not name.startswith("/") and ".." not in name.split("/")
                            for name in names))
        self.assertEqual(generated["sha256"], exporter.sha256_file(
            package / exporter.HANDOFF_ZIP_FILENAME
        ))
        self.assertTrue(exporter.handoff_zip_current(package))

    def test_package_manifest_hashes_match(self):
        self.export(self._complete("verify hashes").write(self.sessions))
        package = self.package_dirs()[0]
        manifest = self.package_manifest()
        for row in manifest["conversation_files"]:
            target = package / row["path"]
            self.assertEqual(row["bytes"], target.stat().st_size)
            self.assertEqual(row["sha256"], exporter.sha256_file(target))
        handoff = manifest["handoff_zip"]
        self.assertIsNone(handoff["sha256"])
        self.assertIsNone(handoff["bytes"])

    def test_package_privacy_scan(self):
        secret = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUV"
        path = self._complete("ship token=%s" % secret).write(self.sessions)
        self.export(path)
        package = self.package_dirs()[0]
        blob = package.name + "\n" + "\n".join(
            target.read_text("utf-8", errors="ignore")
            for target in package.iterdir() if target.is_file()
        )
        self.assertNotIn(secret, blob)
        self.assertNotIn(str(Path.home()),
                         (package / exporter.PACKAGE_MANIFEST_FILENAME)
                         .read_text("utf-8"))

    def test_v1_flat_output_not_mutated_by_repo_test(self):
        self.output.mkdir(parents=True, exist_ok=True)
        old_md = self.output / "legacy__CodexConversationExport.md"
        old_receipt = self.output / "legacy__CodexConversationExport.receipt.json"
        old_md.write_bytes(b"legacy markdown\n")
        old_receipt.write_bytes(b'{"legacy": true}\n')
        before = (old_md.read_bytes(), old_receipt.read_bytes())
        self.export(self._complete("new package").write(self.sessions))
        self.assertEqual((old_md.read_bytes(), old_receipt.read_bytes()), before)

    def test_normal_package_is_byte_and_mtime_stable_without_handoff_zip(self):
        path = self._complete("idempotent package").write(self.sessions)
        self.export(path)
        package = self.package_dirs()[0]
        before = {
            child.name: (child.stat().st_mtime_ns, exporter.sha256_file(child))
            for child in package.iterdir() if child.is_file()
        }
        self.export(path)
        after = {
            child.name: (child.stat().st_mtime_ns, exporter.sha256_file(child))
            for child in package.iterdir() if child.is_file()
        }
        self.assertEqual(after, before)
        self.assertNotIn(exporter.HANDOFF_ZIP_FILENAME, after)

    def test_on_demand_handoff_is_deterministic_and_payload_read_only(self):
        artifact = self.root / "result.json"
        artifact.write_text('{"ok": true}\n', encoding="utf-8")
        self.export(self._complete("deterministic package").write(self.sessions),
                    "--artifact", "result=%s" % artifact)
        package = self.package_dirs()[0]
        payload_paths = [
            package / exporter.CONVERSATION_FILENAME,
            package / exporter.RECEIPT_FILENAME,
            artifact_path(package, artifact.name),
        ]
        payload_before = {
            str(path.relative_to(package)): exporter.sha256_file(path)
            for path in payload_paths
        }
        first = exporter.generate_handoff_zip(package)
        first_bytes = (package / exporter.HANDOFF_ZIP_FILENAME).read_bytes()
        second = exporter.generate_handoff_zip(package)
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertEqual(second["bytes"], first["bytes"])
        self.assertEqual(
            (package / exporter.HANDOFF_ZIP_FILENAME).read_bytes(), first_bytes
        )
        self.assertEqual({
            str(path.relative_to(package)): exporter.sha256_file(path)
            for path in payload_paths
        }, payload_before)

    def test_existing_generated_v21_handoff_remains_compatible(self):
        self.export(self._complete("compatible package").write(self.sessions))
        package = self.package_dirs()[0]
        exporter.generate_handoff_zip(package)
        manifest_path = package / exporter.PACKAGE_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["handoff_zip"]["status"] = "generated"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        self.assertTrue(exporter.package_files_current(package, manifest))
        self.assertTrue(exporter.handoff_zip_current(package, manifest))

    def test_on_demand_handoff_refuses_torn_canonical_package(self):
        self.export(self._complete("torn package").write(self.sessions))
        package = self.package_dirs()[0]
        (package / exporter.CONVERSATION_FILENAME).write_text(
            "tampered\n", encoding="utf-8"
        )
        with self.assertRaises(exporter.ExportBlocked):
            exporter.generate_handoff_zip(package)
        self.assertFalse((package / exporter.HANDOFF_ZIP_FILENAME).exists())


class T28ConversationPackageV21FinderUX(ExporterTestCase):
    def _complete(self, request, session_id=SESSION_A, final="done"):
        builder = RolloutBuilder(session_id=session_id)
        builder.turn_context()
        builder.task_started()
        builder.owner_message(request)
        builder.assistant(final, phase="final_answer")
        builder.task_complete()
        return builder

    def _index_rows(self, rows):
        self.session_index.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows),
            encoding="utf-8",
        )

    def test_latest_exact_session_index_thread_name_wins(self):
        self.session_index.write_text(
            "{malformed unrelated line\n"
            + json.dumps({
                "id": SESSION_B, "thread_name": "其他会话",
                "updated_at": "2026-08-28T00:00:00Z",
            }, ensure_ascii=False) + "\n"
            + json.dumps({
                "id": SESSION_A, "thread_name": "旧标题",
                "updated_at": "2026-08-28T00:01:00Z",
            }, ensure_ascii=False) + "\n"
            + json.dumps({
                "id": SESSION_A, "thread_name": "  最终\t标题  ",
                "updated_at": "2026-08-28T00:02:00Z",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _, receipt = self.export(self._complete("跑").write(self.sessions))
        self.assertEqual(receipt["package"]["display_title"], "最终 标题")
        self.assertEqual(
            receipt["package"]["display_title_source"],
            "latest_session_index_thread_name",
        )
        self.assertTrue(self.package_dirs()[0].name.startswith("最终 标题__"))

    def test_owner_task_outranks_a_stale_provider_thread_name(self):
        """openai/codex#22452: a valid thread can keep a stale thread_name.

        The Owner's own latest completed task is therefore the better title
        whenever one exists.
        """
        self._index_rows([{
            "id": SESSION_A, "thread_name": "旧的线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(
            self._complete("这才是 Owner 的当前任务").write(self.sessions)
        )
        self.assertEqual(receipt["package"]["display_title"],
                         "这才是 Owner 的当前任务")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_missing_session_index_falls_back_honestly(self):
        _, receipt = self.export(
            self._complete("缺少索引时的回退标题").write(self.sessions)
        )
        self.assertEqual(receipt["package"]["display_title"],
                         "缺少索引时的回退标题")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_unreadable_session_index_falls_back_honestly(self):
        path = self._complete("索引不可读回退").write(self.sessions)
        original_open = Path.open

        def deny_index(candidate, *args, **kwargs):
            if candidate == self.session_index:
                raise PermissionError("synthetic index denial")
            return original_open(candidate, *args, **kwargs)

        with mock.patch.object(Path, "open", deny_index):
            _, receipt = self.export(path)
        self.assertEqual(receipt["package"]["display_title"],
                         "索引不可读回退")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_known_index_placeholder_does_not_beat_owner_fallback(self):
        self._index_rows([{
            "id": SESSION_A,
            "thread_name": "Files pasted by the user",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(
            self._complete("使用真实 Owner 请求").write(self.sessions)
        )
        self.assertEqual(receipt["package"]["display_title"],
                         "使用真实 Owner 请求")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_generic_execute_wrapper_uses_exact_attachment_basename(self):
        attachment = "/Users/testowner/Downloads/RUN_THIS_PROMPT.md"
        phrase = "Execute RUN_THIS_PROMPT.md from the attached file"
        self._index_rows([{
            "id": SESSION_A, "thread_name": phrase,
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        request = (
            "# Files mentioned by the user:\n\n"
            "## RUN_THIS_PROMPT.md: %s\n\n## My request:\n%s\n"
            % (attachment, phrase)
        )
        _, receipt = self.export(self._complete(request).write(self.sessions))
        self.assertEqual(receipt["package"]["display_title"], "RUN_THIS_PROMPT")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_attachment_filename_stem")

    def test_short_provider_title_is_respected_without_an_owner_task(self):
        """A short provider name such as "run" is still a real name.

        It is not treated as a placeholder; it simply ranks below an actual
        Owner task, so it wins exactly when the Owner named nothing.
        """
        self._index_rows([{
            "id": SESSION_A, "thread_name": "run",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(self._complete("执行").write(self.sessions))
        self.assertEqual(receipt["package"]["display_title"], "run")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_session_index_thread_name")

    def test_explicit_label_remains_highest_priority(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "provider title",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(
            self._complete("owner fallback").write(self.sessions),
            "--label", "Caller label",
        )
        self.assertEqual(receipt["package"]["display_title"], "Caller label")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "explicit_label")

    def test_persisted_repository_identity_groups_package(self):
        _, receipt = self.export(
            self._complete("项目分组").write(self.sessions)
        )
        package = self.package_dirs()[0]
        self.assertEqual(package.parent.name, "demo-project")
        self.assertEqual(receipt["package"]["project_bucket"], "demo-project")
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_repository_identity")

    def test_persisted_cjk_cwd_groups_package(self):
        builder = RolloutBuilder(
            workspace="/Users/testowner/Documents/示例阅读器",
            repository_url=None,
        )
        builder.turn_context()
        builder.task_started()
        builder.owner_message("中文项目")
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name, "示例阅读器")
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_cwd_basename")

    def test_generic_claude_anchor_is_unclassified(self):
        builder = RolloutBuilder(
            workspace="/Users/testowner/.claude", repository_url=None
        )
        builder.turn_context()
        builder.task_started()
        builder.owner_message("普通项目外会话")
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name,
                         exporter.UNCLASSIFIED_PROJECT_BUCKET)
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "unclassified_persisted_evidence")

    def test_home_root_tmp_and_traversal_workspaces_are_unclassified(self):
        for cwd in (
            "/Users/testowner", "/home/testowner", "/root",
            "/tmp/codex-run", "/private/tmp/build",
            "/Users/testowner/Documents/../ambiguous",
            "/Users/testowner/_v1_flat_archive_20260828",
        ):
            bucket, source = exporter.derive_project_bucket(
                {"cwd": cwd, "git": {}}, [], exporter.Privacy(normalize=True)
            )
            self.assertEqual(bucket, exporter.UNCLASSIFIED_PROJECT_BUCKET, cwd)
            self.assertEqual(source, "unclassified_persisted_evidence", cwd)

    def test_internal_delegation_uses_system_bucket(self):
        builder = RolloutBuilder(
            workspace="/Users/testowner/.claude", repository_url=None
        )
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "<codex_delegation>\n<input>run bounded canary</input>"
        )
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name,
                         exporter.SYSTEM_PROJECT_BUCKET)
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_internal_session")

    def test_internal_delegation_with_repo_identity_overrides_project_bucket(self):
        builder = RolloutBuilder()
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "<codex_delegation>\n<input>run focused review</input>"
        )
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name,
                         exporter.SYSTEM_PROJECT_BUCKET)
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_internal_session")

    def test_internal_delegation_with_project_cwd_overrides_project_bucket(self):
        builder = RolloutBuilder(
            workspace="/Users/testowner/Documents/owner-project",
            repository_url=None,
        )
        builder.turn_context()
        builder.task_started()
        builder.owner_message(
            "<codex_delegation>\n<input>run bounded canary</input>"
        )
        builder.assistant("done", phase="final_answer")
        builder.task_complete()
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name,
                         exporter.SYSTEM_PROJECT_BUCKET)
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_internal_session")

    def test_normal_owner_session_with_same_repo_stays_project_bucket(self):
        builder = self._complete("Review codex_delegation routing behavior")
        _, receipt = self.export(builder.write(self.sessions))
        self.assertEqual(self.package_dirs()[0].parent.name, "demo-project")
        self.assertEqual(receipt["package"]["project_bucket_source"],
                         "persisted_repository_identity")

    def test_system_and_owner_sessions_with_shared_artifact_stay_separate(self):
        artifact = self.root / "shared-review.zip"
        payload = b"synthetic shared review payload\n"
        artifact.write_bytes(payload)

        owner = self._complete("Prepare the Owner review", SESSION_A)
        internal = RolloutBuilder(session_id=SESSION_B)
        internal.turn_context()
        internal.task_started()
        internal.owner_message(
            "<codex_delegation>\n<input>prepare the same review</input>"
        )
        internal.assistant("done", phase="final_answer")
        internal.task_complete()

        self.export(owner.write(self.sessions),
                    "--artifact", "review=%s" % artifact)
        self.export(internal.write(self.sessions),
                    "--artifact", "review=%s" % artifact)

        packages = self.package_dirs()
        self.assertEqual(len(packages), 2)
        self.assertEqual({package.parent.name for package in packages}, {
            "demo-project", exporter.SYSTEM_PROJECT_BUCKET,
        })
        receipts = [json.loads(
            (package / exporter.RECEIPT_FILENAME).read_text(encoding="utf-8")
        ) for package in packages]
        self.assertEqual(
            {receipt["source"]["session_id"] for receipt in receipts},
            {SESSION_A, SESSION_B},
        )
        self.assertEqual(len({
            receipt["source"]["stable_rollout_identity"]["coordinate_sha256"]
            for receipt in receipts
        }), 2)
        for package in packages:
            self.assertEqual(
                (artifact_path(package, artifact.name)).read_bytes(),
                payload,
            )

    def test_same_title_under_two_projects_has_distinct_paths(self):
        first = RolloutBuilder(
            session_id=SESSION_A,
            workspace="/Users/testowner/Documents/项目甲",
            repository_url=None,
        )
        second = RolloutBuilder(
            session_id=SESSION_B,
            workspace="/Users/testowner/Documents/项目乙",
            repository_url=None,
        )
        for builder in (first, second):
            builder.turn_context()
            builder.task_started()
            builder.owner_message("相同会话标题")
            builder.assistant("done", phase="final_answer")
            builder.task_complete()
            self.export(builder.write(self.sessions))
        packages = self.package_dirs()
        self.assertEqual(len(packages), 2)
        self.assertEqual({path.parent.name for path in packages}, {"项目甲", "项目乙"})

    def test_chinese_members_and_unicode_zip_are_integral(self):
        artifact = self.root / "评审结果.json"
        artifact.write_text('{"ok": true}\n', encoding="utf-8")
        self.export(
            self._complete("中文布局").write(self.sessions),
            "--artifact", "result=%s" % artifact,
        )
        package = self.package_dirs()[0]
        self.assertTrue((package / exporter.CONVERSATION_FILENAME).is_file())
        self.assertTrue((artifact_path(package, artifact.name)).is_file())
        self.assertFalse((package /
                          exporter.LEGACY_V2_CONVERSATION_FILENAME).exists())
        self.assertFalse((package /
                          exporter.LEGACY_V2_HANDOFF_ZIP_FILENAME).exists())
        exporter.generate_handoff_zip(package)
        with zipfile.ZipFile(package / exporter.HANDOFF_ZIP_FILENAME) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(exporter.CONVERSATION_FILENAME, archive.namelist())
            self.assertIn(str(artifact_path(package, artifact.name).relative_to(package)),
                          archive.namelist())

    def test_no_sqlite_dependency_or_state_database_access(self):
        source = Path(exporter.__file__).read_text(encoding="utf-8")
        self.assertNotIn("state_5.sqlite", source)
        self.assertNotIn("import sqlite", source)


if __name__ == "__main__":
    unittest.main()


class T29LatestOwnerTaskTitle(ExporterTestCase):
    """A continued conversation must be findable by its newest task.

    Regression cover for stale-title selection. The provider thread name is
    a real signal but not a sufficient one: upstream openai/codex#22452
    documents
    valid rollouts whose thread name stays stale while the thread keeps
    moving.
    """

    OWNER_PASS_1 = "【机房】881GB 磁盘抢救 Pass 1。跑"
    OWNER_PASS_2 = ("【机房】储存清理 Pass 2。按附件执行。"
                    "Computer Use 步骤自己完成……")

    def _index_rows(self, rows):
        self.session_index.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows),
            encoding="utf-8",
        )

    def _turns(self, *requests, trailing_incomplete=None):
        builder = RolloutBuilder(session_id=SESSION_A)
        turn_ids = [TURN_1, TURN_2, "01a00000-aaaa-bbbb-cccc-000000000003"]
        for index, request in enumerate(requests):
            turn_id = turn_ids[index]
            builder.turn_context(turn_id=turn_id)
            builder.task_started(turn_id=turn_id)
            builder.owner_message(request, turn_id=turn_id)
            builder.assistant("done", phase="final_answer", turn_id=turn_id)
            builder.task_complete(turn_id=turn_id)
        if trailing_incomplete is not None:
            turn_id = turn_ids[len(requests)]
            builder.turn_context(turn_id=turn_id)
            builder.task_started(turn_id=turn_id)
            builder.owner_message(trailing_incomplete, turn_id=turn_id)
            builder.assistant("working", turn_id=turn_id)
        return builder.write(self.sessions)

    def _wrapper(self, path, request):
        return (
            "# Files mentioned by the user:\n\n"
            "## %s: %s\n\n## My request:\n%s\n"
            % (Path(path).name, path, request)
        )

    def test_second_task_replaces_a_stale_pass_one_title(self):
        """A thread whose provider name still names pass one."""
        self._index_rows([{
            "id": SESSION_A, "thread_name": "881GB 磁盘抢救 Pass 1",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        attachment = "/Users/testowner/Downloads/Storage_Pass2.md"
        with fake_home("/Users/testowner"):
            _, receipt = self.export(self._turns(
                self.OWNER_PASS_1,
                self._wrapper(attachment, self.OWNER_PASS_2),
            ))
        title = receipt["package"]["display_title"]
        self.assertIn("储存清理 Pass 2", title)
        self.assertNotIn("881GB 磁盘抢救 Pass 1", title)
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_execution_boilerplate_is_not_part_of_the_title(self):
        _, receipt = self.export(self._turns(
            "【机房】储存清理 Pass 2。按附件执行。"
        ))
        title = receipt["package"]["display_title"]
        self.assertIn("储存清理 Pass 2", title)
        self.assertNotIn("按附件执行", title)

    def test_a_filename_in_the_request_is_not_split_into_sentences(self):
        self._index_rows([{
            "id": SESSION_A,
            "thread_name":
                "Execute RUN_THIS_PROMPT.md from the attached file",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        attachment = "/Users/testowner/Downloads/RUN_THIS_PROMPT.md"
        with fake_home("/Users/testowner"):
            _, receipt = self.export(self._turns(self._wrapper(
                attachment, "Execute RUN_THIS_PROMPT.md from the attached file"
            )))
        self.assertEqual(receipt["package"]["display_title"],
                         "RUN_THIS_PROMPT")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_attachment_filename_stem")

    def test_an_in_flight_turn_never_renames_the_package(self):
        _, receipt = self.export(self._turns(
            "第一轮任务", trailing_incomplete="尚未完成的第二轮任务"
        ))
        self.assertEqual(receipt["package"]["display_title"], "第一轮任务")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_meaningful_owner_request")

    def test_thread_name_still_wins_when_no_turn_names_a_task(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "供应商线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(self._turns("跑", "继续"))
        self.assertEqual(receipt["package"]["display_title"], "供应商线程名")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "latest_session_index_thread_name")

    def test_explicit_label_still_outranks_the_latest_task(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "供应商线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        _, receipt = self.export(
            self._turns("第一轮任务", "第二轮任务"),
            "--label", "Owner 指定标题",
        )
        self.assertEqual(receipt["package"]["display_title"], "Owner 指定标题")
        self.assertEqual(receipt["package"]["display_title_source"],
                         "explicit_label")

    def test_identity_suffix_survives_the_rename(self):
        first = self.export(self._turns("第一轮任务"))[1]
        second = self.export(self._turns("第一轮任务", "第二轮任务"))[1]
        self.assertNotEqual(first["package"]["package_dirname"],
                            second["package"]["package_dirname"])
        self.assertEqual(
            first["package"]["package_dirname"].split("__", 1)[1],
            second["package"]["package_dirname"].split("__", 1)[1],
        )


class T30AcknowledgementAndWrapperTitles(ExporterTestCase):
    """What an Owner turn has to say before it may name a package.

    Two shapes were still being promoted into folder names. Short
    acknowledgements ("审吧", "继续，不用停") steer work rather than name it,
    and a generic attachment wrapper followed by execution-control prose
    ("按附件执行；只更新现有 PR，不要合并。") is about the attached task
    file, not about the instruction that follows the wrapper.
    """

    PASS_2 = "【机房】储存清理 Pass 2。按附件执行。"
    PASS_2_LONG = ("【机房】储存清理 Pass 2。按附件执行。"
                   "Computer Use 步骤自己完成……")
    ATTACHMENT = ("/Users/testowner/Downloads/"
                  "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829.md")

    def _index_rows(self, rows):
        self.session_index.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows),
            encoding="utf-8",
        )

    def _turns(self, *requests):
        builder = RolloutBuilder(session_id=SESSION_A)
        turn_ids = [TURN_1, TURN_2, "01a00000-aaaa-bbbb-cccc-000000000003"]
        for index, request in enumerate(requests):
            turn_id = turn_ids[index]
            builder.turn_context(turn_id=turn_id)
            builder.task_started(turn_id=turn_id)
            builder.owner_message(request, turn_id=turn_id)
            builder.assistant("done", phase="final_answer", turn_id=turn_id)
            builder.task_complete(turn_id=turn_id)
        return builder.write(self.sessions)

    def _wrapper(self, path, request):
        return (
            "# Files mentioned by the user:\n\n"
            "## %s: %s\n\n## My request:\n%s\n"
            % (Path(path).name, path, request)
        )

    def _title(self, *requests):
        # Each case starts from an empty output tree, because export() reads
        # back the last receipt it finds and subTest cases share one fixture.
        shutil.rmtree(self.output, ignore_errors=True)
        with fake_home("/Users/testowner"):
            _, receipt = self.export(self._turns(*requests))
        return receipt["package"]["display_title"], \
            receipt["package"]["display_title_source"]

    # -- F1 ---------------------------------------------------------------

    def test_short_acknowledgement_does_not_retitle_the_package(self):
        """The mandatory regression example from the Fix1 entry."""
        self._index_rows([{
            "id": SESSION_A, "thread_name": "881GB 磁盘抢救 Pass 1",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        title, source = self._title(
            self._wrapper(self.ATTACHMENT, self.PASS_2), "审吧"
        )
        self.assertIn("储存清理 Pass 2", title)
        self.assertNotIn("审吧", title)
        self.assertNotIn("881GB 磁盘抢救 Pass 1", title)
        self.assertEqual(source, "latest_meaningful_owner_request")

    def test_acknowledgement_variants_never_retitle(self):
        for acknowledgement in ("审吧", "快审吧", "继续吧", "审核吧", "审阅吧",
                                "检查吧", "继续，不用停", "再跑一下",
                                "先看看", "直接跑吧", "确认一下", "别停",
                                "go ahead", "lgtm", "keep going", "carry on",
                                "looks good", "proceed"):
            with self.subTest(acknowledgement=acknowledgement):
                title, source = self._title(self.PASS_2, acknowledgement)
                self.assertIn("储存清理 Pass 2", title)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_a_real_short_task_still_titles_the_package(self):
        for task in ("清理缓存", "修复标题", "储存清理 Pass 2", "重启导出器",
                     "检查磁盘用量", "审计日志", "看板重构", "跑分测试",
                     "继续教育材料", "review the auth module",
                     "run the migration"):
            with self.subTest(task=task):
                title, source = self._title("第一轮的旧任务", task)
                self.assertEqual(title, task)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_only_acknowledgements_falls_back_to_the_thread_name(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "供应商线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        title, source = self._title("跑", "审吧")
        self.assertEqual(title, "供应商线程名")
        self.assertEqual(source, "latest_session_index_thread_name")

    # -- F2 ---------------------------------------------------------------

    def test_generic_wrapper_prefers_the_attachment_over_control_prose(self):
        """The mandatory regression example from the Fix1 entry."""
        title, source = self._title(self._wrapper(
            self.ATTACHMENT, "按附件执行；只更新现有 PR，不要合并。"
        ))
        self.assertEqual(
            title, "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829")
        self.assertNotIn("只更新现有 PR", title)
        self.assertNotIn("不要合并", title)
        self.assertEqual(source, "latest_attachment_filename_stem")

    def test_execution_control_prose_is_never_a_title(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "供应商线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        for request in ("按附件执行；只更新现有 PR，不要合并。",
                        "跑；不要停。",
                        "执行；Computer Use 步骤自己完成。"):
            with self.subTest(request=request):
                title, source = self._title(request)
                self.assertEqual(title, "供应商线程名")
                self.assertEqual(source, "latest_session_index_thread_name")

    def test_a_bracketed_task_outranks_the_attachment(self):
        for request in (self.PASS_2_LONG, "按附件执行。【机房】储存清理 Pass 2。"):
            with self.subTest(request=request):
                title, source = self._title(
                    self._wrapper(self.ATTACHMENT, request)
                )
                self.assertIn("储存清理 Pass 2", title)
                self.assertNotIn("Codex_ConversationPackageV2_1", title)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_attachment_wrapper_without_a_task_still_uses_the_basename(self):
        title, source = self._title(
            self._wrapper(self.ATTACHMENT, "按附件执行")
        )
        self.assertEqual(
            title, "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829")
        self.assertEqual(source, "latest_attachment_filename_stem")


class T31MultilineTaskTitles(ExporterTestCase):
    """The task may be on a later request line.

    Owners routinely put the execute wrapper on the first line and the task on
    the second. Reading only the first line made the "a bracketed task label
    wins wherever it appears" rule untrue for exactly the messages it was
    written for.
    """

    ATTACHMENT = ("/Users/testowner/Downloads/"
                  "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829.md")

    def _index_rows(self, rows):
        self.session_index.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows),
            encoding="utf-8",
        )

    def _turns(self, *requests):
        builder = RolloutBuilder(session_id=SESSION_A)
        turn_ids = [TURN_1, TURN_2, "01a00000-aaaa-bbbb-cccc-000000000003"]
        for index, request in enumerate(requests):
            turn_id = turn_ids[index]
            builder.turn_context(turn_id=turn_id)
            builder.task_started(turn_id=turn_id)
            builder.owner_message(request, turn_id=turn_id)
            builder.assistant("done", phase="final_answer", turn_id=turn_id)
            builder.task_complete(turn_id=turn_id)
        return builder.write(self.sessions)

    def _wrapper(self, path, request):
        return (
            "# Files mentioned by the user:\n\n"
            "## %s: %s\n\n## My request:\n%s\n"
            % (Path(path).name, path, request)
        )

    def _title(self, *requests):
        shutil.rmtree(self.output, ignore_errors=True)
        with fake_home("/Users/testowner"):
            _, receipt = self.export(self._turns(*requests))
        return receipt["package"]["display_title"], \
            receipt["package"]["display_title_source"]

    def test_bracketed_task_on_the_second_line_beats_the_attachment(self):
        title, source = self._title(self._wrapper(
            self.ATTACHMENT, "按附件执行。\n【机房】储存清理 Pass 2。"
        ))
        self.assertIn("储存清理 Pass 2", title)
        self.assertNotIn("Codex_ConversationPackageV2_1", title)
        self.assertEqual(source, "latest_meaningful_owner_request")

    def test_bracketed_task_first_then_control_lines(self):
        title, source = self._title(self._wrapper(
            self.ATTACHMENT,
            "【机房】储存清理 Pass 2。\n按附件执行。\n"
            "Computer Use 步骤自己完成。",
        ))
        self.assertIn("储存清理 Pass 2", title)
        self.assertNotIn("Computer Use", title)
        self.assertEqual(source, "latest_meaningful_owner_request")

    def test_control_line_after_a_wrapper_never_wins(self):
        title, source = self._title(self._wrapper(
            self.ATTACHMENT, "按附件执行。\n只更新现有 PR，不要合并。"
        ))
        self.assertEqual(
            title, "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829")
        self.assertNotIn("只更新现有 PR", title)
        self.assertEqual(source, "latest_attachment_filename_stem")

    def test_bracketed_task_outranks_an_ordinary_first_line(self):
        title, source = self._title(
            "第一行是普通说明但不命名任务。\n【机房】真实任务名。\n不要合并。"
        )
        self.assertIn("真实任务名", title)
        self.assertNotIn("不要合并", title)
        self.assertEqual(source, "latest_meaningful_owner_request")

    def test_an_ordinary_later_line_still_names_the_work(self):
        title, source = self._title("按附件执行。\n清理缓存。")
        self.assertEqual(title, "清理缓存")
        self.assertEqual(source, "latest_meaningful_owner_request")

    def test_the_scan_is_bounded_and_stops_before_the_body(self):
        body = "\n".join("正文第 %d 行的说明文字" % line for line in range(20))
        title, source = self._title("按附件执行。\n" + body)
        self.assertEqual(title, "正文第 0 行的说明文字")
        self.assertEqual(source, "latest_meaningful_owner_request")
        self.assertEqual(exporter.TITLE_SCAN_LINE_LIMIT, 8)

    def test_a_delegation_wrapper_payload_is_never_read_as_a_title(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "供应商线程名",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        title, source = self._title(
            "<codex_delegation>\n<input>internal task</input>"
        )
        self.assertEqual(title, "供应商线程名")
        self.assertEqual(source, "latest_session_index_thread_name")

    def test_directional_continuation_is_not_a_task_name(self):
        for phrase in ("往下吧", "继续往下吧", "赶紧往下吧", "接着吧"):
            with self.subTest(phrase=phrase):
                title, source = self._title("【机房】储存清理 Pass 2。", phrase)
                self.assertIn("储存清理 Pass 2", title)
                self.assertNotIn(phrase, title)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_an_object_bearing_directional_task_still_titles(self):
        for task in ("往下检查日志", "继续迁移数据", "检查下一批包"):
            with self.subTest(task=task):
                title, source = self._title("第一轮的旧任务", task)
                self.assertEqual(title, task)
                self.assertEqual(source, "latest_meaningful_owner_request")


class T32StatusHoldAndChatterTitles(ExporterTestCase):
    """Status turns are not task names.

    A byte-exact rehearsal over the whole eligible set found six
    packages whose newest completed Owner turn was an acknowledgement, an
    execution-control hold, or presence chatter. Each would have replaced a
    descriptive Finder name with text that names no work, recreating exactly
    the discoverability problem this selection rule exists to solve.

    Upstream ``openai/codex#24289`` asks for the same discipline: a vague
    prompt should not be forced into a thread name; keep the useful title.

    The whole false-positive control is that every pattern matches a *whole*
    clause. ``完成证书自动补齐修复`` keeps ``完成`` and stays a task, because it
    also names what was completed.
    """

    EARLIER_TASK = "【机房】证书自动补齐与 Canary"

    #: The exact strings that rehearsal selected as Finder titles.
    SIX_STATUS_REGRESSIONS = (
        "已完成",
        "登录好了",
        "安全停下,等我号令",
        "安全停下来,等我消息",
        "我刚才不小心切出去一下,大概有一分钟",
        "我刚回电脑前,有什么是需要我直接确认或者操作的吗",
    )

    #: Real task-bearing messages that carry a trigger token on purpose.
    NEGATIVE_CONTROLS = (
        "完成证书自动补齐修复",
        "登录后检查证书状态",
        "确认创建新仓库结构",
        "安全停下服务并修复重启问题",
        "我刚回电脑后检查导出日志",
        "检查我刚才切出去期间的运行日志",
        "Complete certificate repair",
        "After login check certificate status",
        "Stop the service and fix restart failure",
    )

    def _index_rows(self, rows):
        self.session_index.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows),
            encoding="utf-8",
        )

    def _turns(self, *requests):
        builder = RolloutBuilder(session_id=SESSION_A)
        turn_ids = [TURN_1, TURN_2, "01a00000-aaaa-bbbb-cccc-000000000003"]
        for index, request in enumerate(requests):
            turn_id = turn_ids[index]
            builder.turn_context(turn_id=turn_id)
            builder.task_started(turn_id=turn_id)
            builder.owner_message(request, turn_id=turn_id)
            builder.assistant("done", phase="final_answer", turn_id=turn_id)
            builder.task_complete(turn_id=turn_id)
        return builder.write(self.sessions)

    def _wrapper(self, path, request):
        return (
            "# Files mentioned by the user:\n\n"
            "## %s: %s\n\n## My request:\n%s\n"
            % (Path(path).name, path, request)
        )

    def _title(self, *requests):
        shutil.rmtree(self.output, ignore_errors=True)
        with fake_home("/Users/testowner"):
            _, receipt = self.export(self._turns(*requests))
        return receipt["package"]["display_title"], \
            receipt["package"]["display_title_source"]

    # -- the six exact status/hold/chatter regressions -------------------

    def test_six_status_strings_never_replace_an_earlier_task(self):
        """The mandatory reproduction of the regression set.

        completed turn 1 names real work, completed turn 2 is the status
        string. The earlier task must survive.
        """
        for phrase in self.SIX_STATUS_REGRESSIONS:
            with self.subTest(phrase=phrase):
                title, source = self._title(self.EARLIER_TASK, phrase)
                self.assertIn("证书自动补齐与 Canary", title)
                self.assertNotIn(phrase, title)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_status_acknowledgement_variants_never_retitle(self):
        for phrase in ("完成了", "好了", "都完成了", "我这边完成了", "登陆好了",
                       "全部完成", "done", "completed", "logged in", "all set"):
            with self.subTest(phrase=phrase):
                title, _ = self._title(self.EARLIER_TASK, phrase)
                self.assertIn("证书自动补齐与 Canary", title)

    def test_hold_and_wait_control_variants_never_retitle(self):
        for phrase in ("停下,等我消息", "先停下", "等我号令", "暂停一下",
                       "stop and wait", "stop and wait for my instruction",
                       "hold on", "stand by"):
            with self.subTest(phrase=phrase):
                title, _ = self._title(self.EARLIER_TASK, phrase)
                self.assertIn("证书自动补齐与 Canary", title)

    def test_first_person_chatter_variants_never_retitle(self):
        for phrase in ("我刚才切出去一下", "我刚回来", "我刚回电脑前",
                       "大概有一分钟", "差不多两分钟",
                       "有什么需要我确认的吗", "有没有需要我操作的吗"):
            with self.subTest(phrase=phrase):
                title, _ = self._title(self.EARLIER_TASK, phrase)
                self.assertIn("证书自动补齐与 Canary", title)

    # -- false-positive controls ------------------------------------------

    def test_a_trigger_token_with_real_work_stays_a_task(self):
        """The mandatory negative controls: naming the work beats the token."""
        for task in self.NEGATIVE_CONTROLS:
            with self.subTest(task=task):
                title, source = self._title("第一轮的旧任务", task)
                self.assertEqual(title, task)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_short_real_tasks_are_untouched(self):
        for task in ("清理缓存", "修复标题", "往下检查日志", "继续迁移数据",
                     "检查下一批包", "断开连接并重连"):
            with self.subTest(task=task):
                title, source = self._title("第一轮的旧任务", task)
                self.assertEqual(title, task)
                self.assertEqual(source, "latest_meaningful_owner_request")

    def test_a_hold_followed_by_real_work_is_still_a_task(self):
        """``停下手上的活去修复标题`` names work after the hold clause."""
        title, source = self._title("第一轮的旧任务", "停下手上的活去修复标题")
        self.assertEqual(title, "停下手上的活去修复标题")
        self.assertEqual(source, "latest_meaningful_owner_request")

    # -- precedence below the rejected turn --------------------------------

    def test_rejected_turn_falls_through_to_the_attachment_identity(self):
        attachment = ("/Users/testowner/Downloads/"
                      "Codex_ConversationPackageV2_1_Fix3_Fix1_Entry_20260829.md")
        title, source = self._title(
            self._wrapper(attachment, "按附件执行。"), "已完成"
        )
        self.assertIn("Codex_ConversationPackageV2_1", title)
        self.assertEqual(source, "latest_attachment_filename_stem")

    def test_rejected_turn_falls_through_to_the_provider_thread_name(self):
        self._index_rows([{
            "id": SESSION_A, "thread_name": "证书自动补齐线程",
            "updated_at": "2026-08-28T00:00:00Z",
        }])
        title, source = self._title("安全停下,等我号令")
        self.assertEqual(title, "证书自动补齐线程")
        self.assertEqual(source, "latest_session_index_thread_name")

    def test_an_all_status_conversation_uses_the_bounded_fallback(self):
        """No turn names work and there is no thread name: never invent one.

        Which bounded fallback answers depends on the persisted evidence the
        fixture carries, so the gate is that selection left the Owner-turn
        tier entirely rather than which fallback it landed on.
        """
        title, source = self._title("已完成", "安全停下,等我号令")
        self.assertNotIn("已完成", title)
        self.assertNotIn("安全停下", title)
        self.assertIn(source, ("repo_fallback", "cwd_fallback",
                               "codex_session_fallback"))

    def test_an_explicit_bracketed_label_still_wins_over_a_status_turn(self):
        title, source = self._title("【机房】储存清理 Pass 2。", "登录好了")
        self.assertIn("储存清理 Pass 2", title)
        self.assertEqual(source, "latest_meaningful_owner_request")
