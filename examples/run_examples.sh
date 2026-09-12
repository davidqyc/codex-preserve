#!/bin/sh
# Run all three examples and assert the documented exit codes.
#
# Uses the installed `codex-preserve` command when there is one, and otherwise
# falls back to running this checkout directly. Exits 0 only if every example
# produced exactly the verdict it is supposed to produce.
set -u

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)

if command -v codex-preserve >/dev/null 2>&1; then
    run() { codex-preserve "$@"; }
    echo "using the installed codex-preserve command"
else
    run() { PYTHONPATH="$root/src" "${PYTHON:-python3}" -m codex_preserve "$@"; }
    echo "no installed command found; running from $root/src"
fi

status=0
check() {
    name=$1
    expected=$2
    echo
    echo "--- examples/$name (expecting exit $expected) ---"
    run verify "$root/examples/$name"
    actual=$?
    if [ "$actual" -eq "$expected" ]; then
        echo "ok: exit $actual"
    else
        echo "MISMATCH: expected exit $expected, got $actual"
        status=1
    fi
}

check pass 0
check fail 1
check unverifiable 2

echo
if [ "$status" -eq 0 ]; then
    echo "EXAMPLES=PASS (0/1/2 as documented)"
else
    echo "EXAMPLES=FAIL"
fi
exit "$status"
