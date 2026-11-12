#!/bin/sh
# Run a real Claude Code agent against a project, through loop.py.
#   sh run-live.sh <project> [rounds] [agent-name] [work-folder]
#   defaults: 2 rounds, agent "local/claude-code", work folder ./agent-work/<project>/<agent-name>
# The agent works in its own folder. A new folder starts with a copy of the project's agent files, if the project names
# any ("agent_files" in its project.json: a copy of the target, a spec). It may read, write and edit files, and run curl
# and python3. Each round is capped at ten minutes. Start this several times with different agent names to run agents
# side by side.
# Set MODEL to change the model (default: sonnet).
[ -n "$1" ] || { echo "usage: sh run-live.sh <project> [rounds] [agent-name] [work-folder]"; exit 2; }
command -v claude >/dev/null || { echo "Claude Code is not installed here (no 'claude' command). See check-harness.sh for other agent programs."; exit 1; }
ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT="${3:-local/claude-code}"
WORK="${4:-$ROOT/agent-work/$1/$(echo "$AGENT" | tr '/' '-')}"
if [ ! -d "$WORK" ]; then
  mkdir -p "$WORK" || exit 1
  python3 - "$ROOT/projects/$1/project.json" "$WORK" <<'EOF'
import json, shutil, sys
from pathlib import Path
try:
    seed = json.loads(Path(sys.argv[1]).read_text()).get("agent_files")
except (OSError, ValueError):
    seed = None
if seed and Path(seed).is_dir():
    shutil.copytree(seed, sys.argv[2], dirs_exist_ok=True)
EOF
fi
cd "$WORK" || exit 1
exec python3 "$ROOT/loop.py" "$1" --agent "$AGENT" --harness "Claude Code" \
  --max-iterations "${2:-2}" --timeout-min 10 \
  -- claude -p --model "${MODEL:-sonnet}" --allowedTools "Bash(curl:*)" "Bash(python3:*)" "Read" "Write" "Edit"
