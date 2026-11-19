#!/usr/bin/env python3
"""Run any harness against a project, nonstop. This one file is all a VM needs besides the harness.

  python3 scripts/loop.py my-project --agent vm-1/claude -- claude -p
  python3 scripts/loop.py my-project --agent vm-2/other  -- my-harness --prompt-file {brief_file}

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
import codecs
import socket
import shlex
import shutil
import sqlite3
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
import uuid
from pathlib import Path

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
        call("/api/events", {"project": a.project, "type": "activity", "text": text, "agent": a.agent, "harness": a.harness, "model": a.model, "data": {"loop_session": a.session}})
    except OSError:
        pass  # the server being briefly away must not stop the loop


def cli_name(program):
    name = Path(program).name.lower()
    return name.split(".")[0] if name in ("codex", "claude", "codex.exe", "claude.exe", "codex.cmd", "claude.cmd") else ""


def record_input(env, stage, argv, payload, source="Captured at launch"):
    db = env.get("HADES_DETAILS_DB")
    if not db:
        return
    try:
        path = Path(db)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        conn = sqlite3.connect(path, timeout=2)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS invocation
                (project TEXT, agent TEXT, session TEXT, round INTEGER, stage TEXT,
                 argv TEXT, command TEXT, stdin BLOB, source TEXT)""")
            conn.execute("INSERT INTO invocation VALUES (?,?,?,?,?,?,?,?,?)", (
                env["HADES_DETAILS_PROJECT"], env["HADES_DETAILS_AGENT"], env["HADES_LAUNCH_SESSION"],
                int(env["HADES_DETAILS_ROUND"]), stage, json.dumps(argv),
                subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv), payload, source))
            conn.commit()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, KeyError, ValueError) as error:
        print(f"[loop] Run details unavailable: {error}", file=sys.stderr, flush=True)


def forward_cli(argv):
    """Forward a batch invocation after the wrapper has finished composing its input."""
    env = os.environ.copy()
    if "HADES_ORIGINAL_PATH" in env:
        env["PATH"] = env.pop("HADES_ORIGINAL_PATH")
    if sys.stdin.isatty():
        # Interactive input cannot be snapshotted before launch.
        return subprocess.call(argv, env=env)
    payload = sys.stdin.buffer.read()
    record_input(env, "cli", argv, payload)
    # Inherit ordinary output descriptors, including the existing live terminal.
    if os.name != "nt":
        with tempfile.TemporaryFile() as stream:
            stream.write(payload)
            stream.seek(0)
            os.dup2(stream.fileno(), 0, inheritable=True)
            os.execvpe(argv[0], argv, env)
    return subprocess.run(argv, input=payload, env=env).returncode


def launch_env(a, n, argv, directory):
    env = os.environ.copy()
    if not env.get("HADES_DETAILS_DB"):
        return env
    env.update(HADES_DETAILS_PROJECT=a.project, HADES_DETAILS_AGENT=a.agent,
               HADES_DETAILS_ROUND=str(n), HADES_LAUNCH_SESSION=a.session)
    direct = cli_name(argv[0])
    if not direct:
        helper = str(Path(__file__).resolve())
        env["HADES_CAPTURE_CLI"] = helper
        if os.name == "posix":
            original = env.get("PATH", os.defpath)
            for name in ("claude", "codex"):
                program = shutil.which(name, path=original)
                if program:
                    shim = Path(directory) / name
                    prefix = shlex.join([sys.executable, helper, "--capture-cli", program])
                    shim.write_text('#!/bin/sh\nexec ' + prefix + ' "$@"\n', encoding="utf-8")
                    shim.chmod(0o700)
            env["HADES_ORIGINAL_PATH"] = original
            env["PATH"] = directory + os.pathsep + original
    return env


def run_harness(args, brief, timeout, env=None):
    """Run one round. Shows the harness's output as it arrives and also returns it. Returns (exit code or reason, output)."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    if env is not None:
        record_input(env, "cli" if cli_name(args[0]) else "wrapper", args, brief.encode("utf-8"))
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
    timer.start()

    def feed():  # on its own thread, so a harness that prints before it reads cannot deadlock us
        try:
            proc.stdin.write(brief.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    # Mirror flushed output even when the child has not written a newline. Keep just the
    # final note in memory; stderr already goes directly to the launching terminal.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    tail = ""
    while chunk := proc.stdout.read1(16384):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        tail = (tail + decoder.decode(chunk))[-6000:]
    tail = (tail + decoder.decode(b"", final=True))[-6000:]
    code = proc.wait()
    timer.cancel()
    return (f"timeout after {timeout / 60:g} min" if timed_out.is_set() else code), tail


def main():
    p = argparse.ArgumentParser(description="Run a harness against a Hades project, nonstop.",
                                usage="loop.py PROJECT [options] -- HARNESS COMMAND ...")
    p.add_argument("project")
    p.add_argument("--agent", default=os.environ.get("PROJECT_VIEW_AGENT") or socket.gethostname(), help="this agent's name on the page")
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
    a.session = os.environ.get("HADES_LAUNCH_SESSION") or uuid.uuid4().hex
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
        brief += "\n\nEnd your final response with a short paragraph starting with \"Handoff:\" that gives the observed outcome and any decision the person needs to make."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="brief-", delete=False) as f:
            f.write(brief)
        say(a, f"Iteration {n} started")
        try:
            argv = [part.replace("{brief_file}", f.name).replace("{brief}", brief) for part in command]
            with tempfile.TemporaryDirectory(prefix="hades-launch-") as directory:
                env = launch_env(a, n, argv, directory)
                code, output = run_harness(argv, brief, a.timeout_min * 60, env)
        except OSError as e:
            return say(a, f"Loop stopped: could not start the harness ({e})")
        finally:
            os.unlink(f.name)
        if output.strip() and not a.no_notes:  # keep the harness's own account of the round where the person can read it
            try:
                call("/api/events", {"project": a.project, "type": "item", "section": "round-notes", "agent": a.agent, "harness": a.harness,
                                     "key": f"Round {n} · {a.agent} · {time.strftime('%Y-%m-%d %H:%M')}", "data": {"loop_session": a.session}, "body": output.strip()[-6000:]})
            except OSError:
                pass

        try:
            call("/api/slot", {"project": a.project, "agent": a.agent, "release": True})  # let the next agent in
        except OSError:
            pass
        if code == 0:
            say(a, f"Iteration {n} finished (exit 0)")
            failures = 0
            time.sleep(a.pause)
        else:  # back off, so a harness that fails instantly does not spin
            failures += 1
            wait = min(300, a.pause * 2 ** failures)
            say(a, f"Iteration {n} ended badly ({code}); waiting {wait:g} s before the next")
            time.sleep(wait)


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--capture-cli"]:
            sys.exit(forward_cli(sys.argv[2:]))
        main()
    except KeyboardInterrupt:
        print("\n[loop] stopped by Ctrl-C")
