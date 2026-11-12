#!/usr/bin/env python3
"""The front door. One command checks this install and runs everything a single machine needs.

  python3 hades.py          check the install, then start the page and the verifier together, with a practice project ready
  python3 hades.py check    only check that this install works: a few seconds, no model needed, nothing left behind

Options for the first form:
  --no-open        do not open the page in a browser
  --skip-check     start without checking first
  --allow-launch   let the page and a coordinator start Claude Code agents on this machine (writes launchers.json
                   from launchers.example.json). Without it you are asked once, if a terminal is attached.
  --host 0.0.0.0   be reachable from other machines; requires PROJECT_VIEW_TOKEN, as with server.py

The practice project is "practice": a small interpreter with 17 planted defects and a hidden answer key, so you can put
real agents on it and see how many they find. Score it with:  python3 toys/tinyvm/answer_key.py score practice
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("Hades needs Python 3.8 or newer; this is Python " + sys.version.split()[0])

import server  # noqa: E402
import verifier  # noqa: E402

PRACTICE = "practice"
TOY = server.ROOT / "toys" / "tinyvm"
HARNESSES = {"claude": "Claude Code", "codex": "Codex CLI", "omp": "OhMyPi"}


def practice():
    """Create the practice project if it is not there yet. Returns True if it was created."""
    home = server.PROJECTS / PRACTICE
    if (home / "project.json").exists():
        return False
    seed = home / "agent-files"  # what every agent starts with: the interpreter, and nothing else from toys/
    seed.mkdir(parents=True, exist_ok=True)
    shutil.copy(TOY / "tinyvm.py", seed)
    cfg = json.loads((TOY / "practice.json").read_text())
    cfg["agent_files"] = str(seed)
    # The scoring script stays in toys/, next to the copy of the interpreter it replays against.
    cfg["metric"]["verify"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(TOY / 'score.py'))}"
    (home / "project.json").write_text(json.dumps(cfg, indent=2) + "\n")
    sys.path.insert(0, str(TOY))
    import tinyvm
    for part in ["read_header", "read_constants"] + [handler.__name__ for handler in tinyvm.OPS.values()]:
        server.ingest({"project": PRACTICE, "type": "item", "section": "parts", "key": part, "text": ""})
    return True


def check():
    """Start a throwaway server on a spare port with an empty data folder, and exercise every part through it."""
    tmp = Path(tempfile.mkdtemp(prefix="hades-check-"))
    server.PROJECTS = verifier.PROJECTS = tmp / "projects"
    server.LAUNCH_FILE = tmp / "launchers.json"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    server.PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.PORT}"

    def http(path, body=None):
        request = urllib.request.Request(url + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path} answered {e.code}: {e.read().decode()[:200]}") from None
        text = path == "/" or path.startswith("/api/brief") or path == "/api/coordinator"  # the page and the two plain-text guides
        return raw if text else json.loads(raw)

    def need(condition, problem):
        if not condition:
            raise RuntimeError(problem)

    def page():
        need("<title>Hades</title>" in http("/"), "the page did not come back")

    def project():
        need(practice(), "could not create the practice project")
        listed = {p["id"]: p for p in http("/api/projects")}
        need(PRACTICE in listed, "the practice project is not listed")
        parts = sum(1 for e in http(f"/api/events?project={PRACTICE}")["events"] if e["type"] == "item")
        need(parts > 30, f"expected the map of the machine, got {parts} entries")
        return f"{parts} parts on its map"

    def report():
        event = http("/api/events", {"project": PRACTICE, "type": "activity", "text": "Self-check says hello", "agent": "self-check"})
        need(event.get("type") == "activity", f"unexpected answer: {event}")
        try:
            http("/api/events", {"project": PRACTICE, "type": "not-a-type", "text": "x"})
        except RuntimeError as e:
            need("400" in str(e), f"a malformed report should be refused with 400, got: {e}")
        else:
            raise RuntimeError("a malformed report was accepted")

    def task():
        http("/api/events", {"project": PRACTICE, "type": "task", "text": "Self-check task", "status": "queued"})
        first = http("/api/claim", {"project": PRACTICE, "agent": "self-check"})
        need(first and first.get("text") == "Self-check task" and first.get("status") == "running", f"the task was not handed over: {first}")
        need(http("/api/claim", {"project": PRACTICE, "agent": "someone-else"}) is None, "the same task was handed out twice")

    def brief():
        text = http(f"/api/brief?project={PRACTICE}&agent=self-check")
        need("tinyvm.py" in text and "curl" in text, "the brief is missing the goal or the commands to report with")
        return f"{len(text.splitlines())} lines"

    def scoring():
        import tinyvm
        halt = next(code for code, handler in tinyvm.OPS.items() if handler.__name__ == "op_halt")
        crash, clean = b"TVM1\x01\x01\x09ab", b"TVM1\x00" + bytes([halt])
        http("/api/events", {"project": PRACTICE, "type": "run", "text": "A program that really crashes", "agent": "self-check", "artifact": {"program_hex": crash.hex()}})
        http("/api/events", {"project": PRACTICE, "type": "run", "text": "A claimed crash that is not one", "agent": "self-check", "artifact": {"program_hex": clean.hex()}})
        need(verifier.check_once(url) == 2, "the verifier did not score both runs")
        runs = {r["text"]: r for r in http(f"/api/runs?project={PRACTICE}&agent=self-check")}
        real, fake = runs["A program that really crashes"], runs["A claimed crash that is not one"]
        need(real["state"] == "confirmed" and real["detail"] == "IndexError in read_constants", f"the real crash was not confirmed: {real}")
        need(fake["state"] == "not confirmed", f"the false claim was not rejected: {fake}")
        need("read_constants" in (real.get("covers") or {}), "the verifier did not report what the run reached")
        return "real crash confirmed, false claim rejected"

    def loop():
        stand_in = [sys.executable, "-c", "import sys; print('Stand-in harness read a brief of', len(sys.stdin.read()), 'characters')"]
        done = subprocess.run([sys.executable, str(server.ROOT / "loop.py"), PRACTICE, "--agent", "self-check-loop", "--max-iterations", "1", "--pause", "0", "--"] + stand_in,
                              env={**os.environ, "PROJECT_VIEW_URL": url}, cwd=tmp, capture_output=True, text=True, timeout=60)
        need(done.returncode == 0, f"loop.py exited with {done.returncode}: {(done.stdout + done.stderr)[-300:]}")
        notes = [e for e in http(f"/api/events?project={PRACTICE}")["events"] if e.get("section") == "round-notes" and "Stand-in harness" in e.get("body", "")]
        need(notes, "the round's output did not reach the Round notes page")
        return "one round with a stand-in harness"

    def mcp():
        hello = http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        need(hello["result"]["serverInfo"]["name"] == "hades", f"unexpected answer: {hello}")
        tools = [t["name"] for t in http("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        need("report" in tools and "start_agents" in tools, f"tools are missing: {tools}")
        got = http("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_project", "arguments": {"project": PRACTICE}}})
        need("tinyvm" in got["result"]["content"][0]["text"], "get_project did not return the project")
        return f"{len(tools)} tools"

    def coordinator():
        need("list_projects" in http("/api/coordinator"), "the coordinator guide is missing its tools")
        listed = http("/api/coordinate", {"tool": "list_projects", "args": {}})["result"]
        need(any(p["project"] == PRACTICE for p in listed), "list_projects did not include the practice project")
        status = http("/api/coordinate", {"tool": "project_status", "args": {"project": PRACTICE}})["result"]
        need(status, "project_status returned nothing")

    def setup():
        made = http("/api/new", {"project": "self-check-new", "target": "A folder of files I have not read yet."})
        need(made.get("phase") == "survey", f"a new project should start in setup: {made}")
        need("SET UP" in http("/api/brief?project=self-check-new&agent=self-check"), "a new project's brief should ask for a survey")

    steps = [("The page loads", page), ("The practice project is created", project), ("A report is accepted and a malformed one refused", report),
             ("A queued task goes to exactly one agent", task), ("The brief is written", brief), ("The verifier re-scores results", scoring),
             ("The loop runs a round", loop), ("The MCP endpoint answers", mcp), ("The coordinator guide and tools answer", coordinator),
             ("A new project starts in setup", setup)]
    print(f"Checking this install (Python {sys.version.split()[0]}) ...")
    failed = 0
    for label, step in steps:
        try:
            note = step()
            print(f"  ok    {label}" + (f"  ({note})" if note else ""))
        except Exception as e:  # report every step, whatever went wrong in it
            failed += 1
            print(f"  FAIL  {label}: {e}")
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    found = [label for command, label in HARNESSES.items() if shutil.which(command)]
    print("  note  Agent programs found here: " + (", ".join(found) if found else "none. Install one (for example Claude Code) to run real agents"))
    print(f"{len(steps) - failed} of {len(steps)} checks passed." + ("" if not failed else "  Hades is not healthy on this machine; the lines marked FAIL say where."))
    return not failed


def listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def offer_launch(yes):
    """Starting agents from the page means this server runs a command for you, so it is switched on only by the person."""
    if server.LAUNCH_FILE.exists() or not shutil.which("claude"):
        return
    if not yes:
        if not sys.stdin.isatty():
            return
        answer = input("Let the page (and a coordinator you talk to) start Claude Code agents on this machine? [y/N] ")
        yes = answer.strip().lower() in ("y", "yes")
    if yes:
        shutil.copy(server.ROOT / "launchers.example.json", server.LAUNCH_FILE)
        print("  Wrote launchers.json. Edit it to change the model or add other agent programs.")


def main(argv):
    if argv[:1] == ["check"]:
        sys.exit(0 if check() else 1)
    unknown = [a for i, a in enumerate(argv) if a not in ("--no-open", "--skip-check", "--allow-launch", "--host") and not (i and argv[i - 1] == "--host")]
    if unknown or (argv[-1:] == ["--host"]):
        sys.exit(__doc__.strip())
    host = argv[argv.index("--host") + 1] if "--host" in argv else "127.0.0.1"
    if host not in server.LOCAL and not server.TOKEN:
        sys.exit("Refusing to listen beyond this machine without a token. Set PROJECT_VIEW_TOKEN first.")
    here = f"http://127.0.0.1:{server.PORT}"
    if listening(server.PORT):
        sys.exit(f"Something is already listening on port {server.PORT}. If that is Hades, it is at {here} .\n"
                 "To run a second copy, set PROJECT_VIEW_PORT to another port.")
    if "--skip-check" not in argv:  # in its own process, so nothing from the check lingers in this one
        if subprocess.run([sys.executable, str(Path(__file__).resolve()), "check"]).returncode:
            sys.exit(1)
        print()
    created = practice()
    offer_launch("--allow-launch" in argv)
    can_launch = bool(server.recipes())
    print(f"The practice project is {'ready' if created else 'still here'}: \"{PRACTICE}\", a small interpreter with 17 planted defects.")
    print("\nPut a real agent on it, whichever way you like:")
    print(f"  In another terminal     sh run-live.sh {PRACTICE} 3")
    print("  From the page           Settings, then Start agents" + ("" if can_launch else "   (off: run again with --allow-launch)"))
    print(f"  By talking to Claude    Read {here}/api/coordinator and act as my coordinator for Hades.")
    print(f"Then see how it did       python3 toys/tinyvm/answer_key.py score {PRACTICE}")
    print("\nThe verifier runs in this process too. Ctrl-C stops both.\n")
    threading.Thread(target=verifier.loop, args=(here,), daemon=True).start()
    if "--no-open" not in argv:
        threading.Timer(1, lambda: webbrowser.open(f"{here}/#/{PRACTICE}/overview")).start()
    try:
        server.serve(host)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main(sys.argv[1:])
