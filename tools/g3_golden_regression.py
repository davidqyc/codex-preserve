#!/usr/bin/env python3
"""Compare deterministic synthetic exports with the published v0.1.3 core.

The source and output paths are reused between runs. Generation time is fixed
before entering the exporter, so every package member can be compared as bytes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "1ddec2ce943cdaa52e3771c59a8a9db7e42b4c7f"
STAMP = "2026-09-29T00:00:00Z"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.test_codex_conversation_export import RolloutBuilder, minimal_session  # noqa: E402

EXPORT_DRIVER = """\
import json, sys
from pathlib import Path
from codex_preserve import exporter
source, output, stamp = map(Path, sys.argv[1:4])
exporter.resolve_session_thread_names = lambda _ids: {}
arguments = [
    '--rollout', str(source), '--output-dir', str(output),
    '--no-git-probe', '--quiet']
if len(sys.argv) > 4 and sys.argv[4] != '-':
    arguments.extend(['--artifact', 'evidence=' + sys.argv[4]])
options = exporter.build_parser().parse_args(arguments)
options.generated_at = str(stamp)
context = exporter.run_export(options)
written = exporter.write_outputs(context, output)
if len(sys.argv) > 4 and sys.argv[4] != '-':
    exporter.generate_handoff_zip(written['package_dir'])
print(json.dumps({'status': context['receipt']['export_status'],
                  'package': str(written['package_dir']),
                  'changed': written['changed']}))
"""

VERIFY_DRIVER = """\
import json, sys
from pathlib import Path
from codex_preserve import verify
result = verify.verify_package(Path(sys.argv[1]))
result['package'] = '<PACKAGE>'
print(json.dumps(result, sort_keys=True, ensure_ascii=False))
sys.exit(result['exit_code'])
"""


def run_python(source_root: Path, code: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root / "src")
    return subprocess.run([sys.executable, "-c", code, *map(str, args)],
                          cwd=source_root, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def verify(source_root: Path, package: Path) -> tuple[int, dict]:
    result = run_python(source_root, VERIFY_DRIVER, str(package))
    if not result.stdout:
        raise RuntimeError(result.stderr)
    return result.returncode, json.loads(result.stdout)


def main() -> int:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "v0.1.3^{}"], cwd=ROOT, text=True).strip()
    if resolved != BASELINE:
        raise RuntimeError("v0.1.3 does not resolve to the pinned baseline")

    with tempfile.TemporaryDirectory(prefix="codex-preserve-g3-") as temporary:
        work = Path(temporary)
        baseline_root = work / "baseline"
        baseline_root.mkdir()
        archive = subprocess.Popen(["git", "archive", BASELINE], cwd=ROOT,
                                   stdout=subprocess.PIPE)
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as contents:
            contents.extractall(baseline_root)
        if archive.wait() != 0:
            raise RuntimeError("could not extract the pinned baseline")

        source_root = work / "source"
        source_root.mkdir()
        complete = minimal_session().write(source_root)
        incomplete_builder = RolloutBuilder(session_id="01b00000-1111-2222-3333-555555555555")
        incomplete_builder.turn_context()
        incomplete_builder.task_started()
        incomplete_builder.owner_message("synthetic incomplete task")
        incomplete_builder.add("future_record_type", {"unexpected": True})
        incomplete = incomplete_builder.write(source_root)
        artifact = source_root / "synthetic-evidence.txt"
        artifact.write_bytes(b"synthetic artifact payload\n")
        os.utime(artifact, ns=(1780000000000000000, 1780000000000000000))

        for label, source, payload in (("complete", complete, artifact),
                                       ("degraded", incomplete, None)):
            output = work / "output"
            snapshots = {}
            packages = {}
            for name, code_root in (("baseline", baseline_root), ("candidate", ROOT)):
                shutil.rmtree(output, ignore_errors=True)
                result = run_python(code_root, EXPORT_DRIVER, str(source),
                                    str(output), STAMP,
                                    str(payload) if payload else "-")
                if result.returncode:
                    raise RuntimeError("%s %s export: %s" %
                                       (label, name, result.stderr))
                metadata = json.loads(result.stdout)
                snapshots[name] = tree_bytes(output)
                package = Path(metadata["package"])
                saved = work / (label + "-" + name)
                shutil.copytree(package, saved)
                packages[name] = saved
                print("%s %s status=%s members=%d" %
                      (label, name, metadata["status"], len(snapshots[name])))
                if name == "candidate":
                    rerun = run_python(code_root, EXPORT_DRIVER, str(source),
                                       str(output), STAMP,
                                       str(payload) if payload else "-")
                    if rerun.returncode:
                        raise RuntimeError("%s candidate rerun: %s" %
                                           (label, rerun.stderr))
                    if tree_bytes(output) != snapshots[name]:
                        raise AssertionError("%s candidate rerun changed bytes" % label)
                    print("%s candidate DETERMINISTIC_RERUN=yes" % label)
            if snapshots["baseline"] != snapshots["candidate"]:
                names = sorted(set(snapshots["baseline"]) | set(snapshots["candidate"]))
                differences = [name for name in names if
                               snapshots["baseline"].get(name) !=
                               snapshots["candidate"].get(name)]
                raise AssertionError("%s byte differences: %s" %
                                     (label, differences))
            print("%s BYTE_IDENTICAL=yes" % label)

            matrix = {}
            for verifier_name, code_root in (("old", baseline_root),
                                             ("new", ROOT)):
                for package_name, package in packages.items():
                    result = verify(code_root, package)
                    matrix[(verifier_name, package_name)] = result
                    print("%s verifier=%s package=%s verdict=%s exit=%d" %
                          (label, verifier_name, package_name,
                           result[1]["verdict"], result[0]))
            if len(set(json.dumps(value, sort_keys=True) for value in
                       matrix.values())) != 1:
                raise AssertionError("%s verifier JSON/exit matrix drift" % label)
            failed = {}
            for package_name, package in packages.items():
                bad = work / (label + "-" + package_name + "-tampered")
                shutil.copytree(package, bad)
                member = bad / "对话记录.md"
                content = member.read_bytes()
                member.write_bytes(bytes([content[0] ^ 1]) + content[1:])
                for verifier_name, code_root in (("old", baseline_root),
                                                 ("new", ROOT)):
                    result = verify(code_root, bad)
                    failed[(verifier_name, package_name)] = result
                    print("%s verifier=%s tampered_package=%s verdict=%s exit=%d" %
                          (label, verifier_name, package_name,
                           result[1]["verdict"], result[0]))
            if any(code != 1 or receipt["verdict"] != "FAIL" for code, receipt
                   in failed.values()):
                raise AssertionError("%s tampered package accepted" % label)
            if len(set(json.dumps(value, sort_keys=True) for value in
                       failed.values())) != 1:
                raise AssertionError("%s tampered verifier JSON/exit drift" % label)
    print("G3_GOLDEN_REGRESSION=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
