#!/usr/bin/env bash
# Wire agent-team into this machine so all three consumers can find it:
#   1. you at the terminal   -> `job` on PATH
#   2. a Claude session      -> skill in ~/.claude/skills/
#   3. a Codex session       -> skill in ~/.codex/skills/
# Idempotent: safe to re-run (e.g. after `git pull`). Symlinks, so updates propagate.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "agent-team installer  (repo: $REPO)"
echo

# 1. `job` on PATH -------------------------------------------------------------
BIN="$HOME/bin"
[ -d "$BIN" ] || BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$REPO/bin/job" "$BIN/job"
echo "  [job]    $BIN/job -> $REPO/bin/job"
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "  NOTE: $BIN is not on your PATH — add it in ~/.zshrc:  export PATH=\"$BIN:\$PATH\"" ;;
esac

# 2. skill into Claude and Codex (identical Agent Skills format) ---------------
for SKILLS_DIR in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  mkdir -p "$SKILLS_DIR"
  ln -sfn "$REPO/skills/agent-team" "$SKILLS_DIR/agent-team"
  echo "  [skill]  $SKILLS_DIR/agent-team -> $REPO/skills/agent-team"
done

echo
echo "Done. Open a NEW Claude or Codex session and 'agent-team' will be discoverable."
echo "Verify:  job --help   |   job recipes"
