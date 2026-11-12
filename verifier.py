#!/usr/bin/env python3
"""The verifier: re-scores every run itself, so the page never has to trust an agent's own number.

  python3 verifier.py

For each run without a verdict, it runs the project's scoring command (metric.verify in the local
project.json), feeds the run's artifact to it on stdin, and posts what the command says.

The scoring command must print one line of JSON:  {"ok": true, "score": 0.41, "detail": "..."}
  ok      whether the run counts (reproduced, valid, did not diverge)
  score   the measured number
  detail  a short label; for "unique" metrics, runs with the same detail count once
  covers  optional: what this run actually exercised, as {"name": count}. The page adds these up to show
          measured coverage on any section whose project.json entry has "measured": {"<status>": <count needed>}.
          Agents can also send runs marked "probe": true, which are replayed for coverage but never count as results.

Run this somewhere the agents cannot write to. It reads the command from its own disk, never from the
server, so an agent cannot change how it is scored.

Environment: PROJECT_VIEW_URL, PROJECT_VIEW_VERIFIER_TOKEN (needed from any other machine).
"""
import json
import os
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

PROJECTS = Path(__file__).parent / "projects"
TIMEOUT = 20


def call(url, path, body=None):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("PROJECT_VIEW_VERIFIER_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PROJECT_VIEW_VERIFIER_TOKEN"]
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url + path, data, headers), timeout=10) as r:
        return json.loads(r.read())


def score(command, artifact, cwd):
    try:
        out = subprocess.run(shlex.split(command), input=json.dumps(artifact), capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        verdict = {"ok": bool(result.get("ok")), "text": str(result.get("detail") or "")[:300]}
        if isinstance(result.get("score"), (int, float)) and not isinstance(result.get("score"), bool):
            verdict["score"] = result["score"]
        if isinstance(result.get("covers"), (dict, list)):
            verdict["covers"] = result["covers"]
        return verdict
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": f"Scoring timed out after {TIMEOUT} seconds"}
    except (ValueError, IndexError, OSError, AttributeError):
        return {"ok": False, "text": "The scoring command did not return a result"}


def check_once(url):
    done = 0
    for d in sorted(p for p in PROJECTS.iterdir() if p.is_dir()) if PROJECTS.exists() else []:
        try:
            command = (json.loads((d / "project.json").read_text()).get("metric") or {}).get("verify")
        except (OSError, ValueError, AttributeError):
            command = None
        if not command:
            continue
        for run in call(url, f"/api/pending?project={d.name}"):
            verdict = score(command, run.get("artifact"), d)
            call(url, "/api/events", {"project": d.name, "type": "verdict", "run": run["id"], "agent": "verifier", "harness": "verifier.py", **verdict})
            done += 1
    return done


def loop(url=None, every=3):
    url = (url or os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765")).rstrip("/")
    while True:
        try:
            check_once(url)
        except OSError:
            pass  # server not up yet, or briefly unreachable; try again
        time.sleep(every)


if __name__ == "__main__":
    loop()
