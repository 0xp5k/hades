#!/usr/bin/env python3
"""Report one event to Hades from any harness, on any machine. Needs only Python.

  python3 emit.py <project> activity "Training for 5 minutes"
  python3 emit.py <project> run      "Cosine, peak 0.3" --score 0.41 --artifact '{"peak": 0.3}'
  python3 emit.py <project> finding  "Warmup matters most" --body "Longer explanation..."
  python3 emit.py <project> question "Does this hold at larger batch size?"
  python3 emit.py <project> item     "Swept 2% to 15%" --section coverage --key "Warmup length" --status explored
  python3 emit.py <project> task     "Sweep warmup length" --status queued
  python3 emit.py <project> tool     Bash --ok --ms 1200

Environment:
  PROJECT_VIEW_URL    where the server is (default http://127.0.0.1:8765)
  PROJECT_VIEW_TOKEN  the server's token, needed from any other machine
  PROJECT_VIEW_AGENT  who this is, e.g. vm-2/codex (or pass --agent)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def emit(project, type, text="", **extra):
    url = os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765").rstrip("/") + "/api/events"
    body = {"project": project, "type": type, "text": text, **{k: v for k, v in extra.items() if v is not None}}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("PROJECT_VIEW_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PROJECT_VIEW_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=10) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Report one event to Hades.")
    p.add_argument("project")
    p.add_argument("type", choices=["activity", "run", "finding", "question", "item", "task", "tool"])
    p.add_argument("text")
    p.add_argument("--score", type=float)
    p.add_argument("--ms", type=float)
    p.add_argument("--ok", action="store_true", default=None)
    p.add_argument("--failed", action="store_true", help="for a tool call that failed")
    p.add_argument("--artifact", type=json.loads, help="JSON the verifier needs to re-score a run")
    p.add_argument("--agent", default=os.environ.get("PROJECT_VIEW_AGENT"))
    for flag in ("id", "section", "key", "area", "status", "body", "harness"):
        p.add_argument("--" + flag)
    a = vars(p.parse_args())
    if a.pop("failed"):
        a["ok"] = False
    try:
        print(json.dumps(emit(a.pop("project"), a.pop("type"), a.pop("text"), **a)))
    except urllib.error.HTTPError as e:
        sys.exit(f"Server refused the event ({e.code}): {e.read().decode(errors='replace')}")
    except OSError as e:
        sys.exit(f"Could not reach the server: {e}")
