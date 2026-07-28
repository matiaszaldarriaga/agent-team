#!/usr/bin/env bash
# Run a job's check spine, and refuse to read a silent death as a pass.
#
# Two ways a spine lies about having run:
#
#   1. it dies before doing anything -- a zsh-only expansion under `bash`, a typo, a missing
#      interpreter -- and the shell still exits 0. Observed: a spine whose first statement was
#      `JOB_DIR=${0:a:h:h}` printed one line of error under bash 3.2, ran none of its six
#      sections, and exited 0. Two rounds recorded `checks: PASSED` with nothing behind them.
#   2. it is invoked with the wrong shell, so its own `#!` line never gets a say.
#
# So: execute the script directly when it is executable, letting its shebang choose the
# interpreter; and require it to announce that it reached the end. A spine that cannot say
# it finished has not verified anything, whatever its exit code claims.
#
# Usage:  run_spine.sh <script> [sentinel]     (default sentinel: SPINE OK)
# Exit:   0 iff the script exits 0 AND printed the sentinel. A missing script is not a failure
#         -- an early round may not have written one yet.
set -uo pipefail

SPINE="${1:?usage: run_spine.sh <script> [sentinel]}"
SENTINEL="${2:-SPINE OK}"

[ -e "$SPINE" ] || exit 0

if [ -x "$SPINE" ]; then
    out=$("$SPINE" 2>&1); status=$?
else
    out=$(sh "$SPINE" 2>&1); status=$?
fi
printf '%s\n' "$out"

[ "$status" -eq 0 ] || exit "$status"

case "$out" in
    *"$SENTINEL"*) exit 0 ;;
    *)
        printf '\nrun_spine: %s exited 0 but never printed "%s".\n' "$SPINE" "$SENTINEL"
        printf 'It stopped early, so this round has no verification behind it. Make the spine\n'
        printf 'print the sentinel as its last act on success.\n'
        exit 1 ;;
esac
