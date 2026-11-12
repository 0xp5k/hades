#!/usr/bin/env python3
"""Run any harness against a project, nonstop. This one file is all a VM needs besides the harness.

  python3 loop.py my-project --agent vm-1/claude -- claude -p
  python3 loop.py my-project --agent vm-2/other  -- my-harness --prompt-file {brief_file}

Each iteration starts the harness fresh, with the project's current brief as its prompt:
  1. Stop if a limit is reached or a file named STOP exists here. Wait while the project is on hold (paused, or waiting on the person).
  2. Fetch the brief from the server (instructions plus where things stand right now).
  3. Run your command. The brief is sent on its standard input. In the arguments, {brief_file} is
     replaced with the path of a file holding it, and {brief} with the text itself.
  4. Save what the harness printed to the project's "Round notes" page, report how the round ended, then go again.

Everything after "--" is your harness command, exactly as you would type it. The harness needs to be
allowed to make HTTP requests to the server and to do the project's work without asking; how you grant
that is specific to the harness.

Environment: PROJECT_VIEW_URL (default http://127.0.0.1:8765), PROJECT_VIEW_TOKEN (from another machine).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765").rstrip("/")


def call(path, body=None):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("PROJECT_VIEW_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PROJECT_VIEW_TOKEN"]
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(URL + path, data, headers), timeout=15) as r:
        return r.read().decode()


def say(a, text):
    print(f"[loop] {text}", flush=True)
    try:
        call("/api/events", {"project": a.project, "type": "activity", "text": text, "agent": a.agent, "harness": a.harness, "model": a.model})
    except OSError:
        pass  # the server being briefly away must not stop the loop


def run_harness(args, brief, timeout):
    """Run one round. Shows the harness's output as it arrives and also returns it. Returns (exit code or reason, output)."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors="replace")
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
    timer.start()

    def feed():  # on its own thread, so a harness that prints before it reads cannot deadlock us
        try:
            proc.stdin.write(brief)
            proc.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    code = proc.wait()
    timer.cancel()
    return (f"timeout after {timeout / 60:g} min" if timed_out.is_set() else code), "".join(lines)


def main():
    p = argparse.ArgumentParser(description="Run a harness against a Project View project, nonstop.",
                                usage="loop.py PROJECT [options] -- HARNESS COMMAND ...")
    p.add_argument("project")
    p.add_argument("--agent", default=os.environ.get("PROJECT_VIEW_AGENT") or os.uname().nodename, help="this agent's name on the page")
    p.add_argument("--harness", help="label shown on the Agents page (default: the command's name)")
    p.add_argument("--model", help="model name shown on the Agents page (default: taken from a --model in the harness command)")
    p.add_argument("--max-iterations", type=int, default=0, help="stop after this many (default: no limit)")
    p.add_argument("--max-hours", type=float, default=0, help="stop after this long (default: no limit)")
    p.add_argument("--timeout-min", type=float, default=60, help="end one iteration after this many minutes (default 60)")
    p.add_argument("--pause", type=float, default=5, help="seconds between iterations (default 5)")
    p.add_argument("--no-notes", action="store_true", help="do not save each round's output to the project's Round notes page")
    # Split at the first "--" ourselves, so the harness command can contain any flags it likes.
    argv = sys.argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    a, command = p.parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        p.error("give the harness command after --, for example:  -- claude -p")
    a.harness = a.harness or os.path.basename(command[0])
    if not a.model and "--model" in command[:-1]:  # most harnesses take the model this way; report it so the page can show it
        a.model = command[command.index("--model") + 1]
    query = urllib.parse.urlencode({"project": a.project, "agent": a.agent})
    started, n, failures = time.time(), 0, 0
    if not a.no_notes:
        try:
            call("/api/project", {"project": a.project, "section": {"id": "round-notes", "title": "Round notes", "view": "note"}})
        except OSError:
            pass

    while True:
        if os.path.exists("STOP"):
            return say(a, "Loop stopped: found a STOP file")
        if a.max_iterations and n >= a.max_iterations:
            return say(a, f"Loop stopped: reached {a.max_iterations} iterations")
        if a.max_hours and time.time() - started > a.max_hours * 3600:
            return say(a, f"Loop stopped: reached {a.max_hours:g} hours")
        try:
            state = json.loads(call("/api/project?" + query))
            if state.get("hold"):  # paused, awaiting the person, or past the time they allowed: one reason, decided by the server
                print(f"[loop] on hold: {state['hold']}; checking again in 15 s", flush=True)
                time.sleep(15)
                continue
            turn = json.loads(call("/api/slot", {"project": a.project, "agent": a.agent}))
            if not turn.get("granted"):  # the person set a limit on agents working at once, and the slots are taken
                print(f"[loop] waiting for a free slot ({turn.get('limit')} at once; working now: {', '.join(turn.get('working') or [])}); checking again in 15 s", flush=True)
                time.sleep(15)
                continue
            if n == 0:
                time.sleep(4)  # agents started together each take their slot first, so every brief can show who else is here
            brief = call("/api/brief?" + query)
        except (OSError, ValueError) as e:
            print(f"[loop] cannot reach {URL} ({e}); retrying in 15 s", flush=True)
            time.sleep(15)
            continue

        n += 1
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="brief-", delete=False) as f:
            f.write(brief)
        say(a, f"Iteration {n} started")
        try:
            code, output = run_harness([part.replace("{brief_file}", f.name).replace("{brief}", brief) for part in command], brief, a.timeout_min * 60)
        except OSError as e:
            return say(a, f"Loop stopped: could not start the harness ({e})")
        finally:
            os.unlink(f.name)
        if output.strip() and not a.no_notes:  # keep the harness's own account of the round where the person can read it
            try:
                call("/api/events", {"project": a.project, "type": "item", "section": "round-notes", "agent": a.agent, "harness": a.harness,
                                     "key": f"Round {n} · {a.agent} · {time.strftime('%Y-%m-%d %H:%M')}", "body": output.strip()[-6000:]})
            except OSError:
                pass

        try:
            call("/api/slot", {"project": a.project, "agent": a.agent, "release": True})  # let the next agent in
        except OSError:
            pass
        if code == 0:
            failures = 0
            time.sleep(a.pause)
        else:  # back off, so a harness that fails instantly does not spin
            failures += 1
            wait = min(300, a.pause * 2 ** failures)
            say(a, f"Iteration {n} ended badly ({code}); waiting {wait:g} s before the next")
            time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[loop] stopped by Ctrl-C")
