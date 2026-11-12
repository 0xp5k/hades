#!/bin/sh
# One connection-test round for another harness, through the same loop.py that runs Claude Code.
#   sh check-harness.sh codex     Codex CLI
#   sh check-harness.sh omp       OhMyPi
# The agent is asked only to report which harness and model it is, on the "harness-check" project. It works in an empty
# folder of its own. Codex runs in its workspace-write sandbox with network access switched on, because it has to reach
# the HAUS server on this machine; nothing else about its sandbox is relaxed.
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORK="${TMPDIR:-/tmp}/project-view-check-$1"
mkdir -p "$WORK" && cd "$WORK" || exit 1
case "$1" in
  codex) exec python3 "$ROOT/loop.py" harness-check --agent check/codex --harness "Codex CLI" --max-iterations 1 --timeout-min 4 \
           -- codex exec --skip-git-repo-check -s workspace-write -c sandbox_workspace_write.network_access=true - ;;
  omp)   exec python3 "$ROOT/loop.py" harness-check --agent check/omp --harness "OhMyPi" --max-iterations 1 --timeout-min 4 \
           -- omp -p --no-session "@{brief_file}" ;;
  *)     echo "usage: sh check-harness.sh codex|omp"; exit 2 ;;
esac
