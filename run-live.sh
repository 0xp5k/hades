#!/bin/sh
# Run a real Claude Code agent against a project, through loop.py.
#   sh run-live.sh <project> [rounds] [agent-name] [work-folder]
#   defaults: 2 rounds, agent "local/claude-code", work folder ./agent-work/<agent-name>
# The agent works in its own folder (created if missing; put a copy of the target or its spec there first). It may read,
# write and edit files, and run curl and python3. Each round is capped at ten minutes. Start this several times with
# different agent names and folders to run agents side by side.
# Set MODEL to change the model (default: sonnet).
[ -n "$1" ] || { echo "usage: sh run-live.sh <project> [rounds] [agent-name] [work-folder]"; exit 2; }
ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT="${3:-local/claude-code}"
WORK="${4:-$ROOT/agent-work/$(echo "$AGENT" | tr '/' '-')}"
mkdir -p "$WORK" && cd "$WORK" || exit 1
exec python3 "$ROOT/loop.py" "$1" --agent "$AGENT" --harness "Claude Code" \
  --max-iterations "${2:-2}" --timeout-min 10 \
  -- claude -p --model "${MODEL:-sonnet}" --allowedTools "Bash(curl:*)" "Bash(python3:*)" "Read" "Write" "Edit"
