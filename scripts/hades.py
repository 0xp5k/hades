#!/usr/bin/env python3
"""The front door. One command checks this install and starts the page.

  python3 scripts/hades.py          check the install, then open the page (a first run starts with one empty project)
  python3 scripts/hades.py check    only check that this install works: a few seconds, no model needed, nothing left behind

Options for the first form:
  --no-open        do not open the page in a browser
  --skip-check     start without checking first
  --allow-launch   let the page start Claude Code on this machine (writes launchers.json from launchers.example.json).
                   Without it you are asked once, if a terminal is attached.

On the page, say what to research and press Start.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("Hades needs Python 3.8 or newer; this is Python " + sys.version.split()[0])

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402

PROJECT = "self-check"
HARNESSES = {"claude": "Claude Code", "codex": "Codex CLI"}


def check():
    """Start a throwaway server on a spare port with an empty data folder, and exercise every part through it."""
    tmp = Path(tempfile.mkdtemp(prefix="hades-check-"))
    server.PROJECTS = tmp / "projects"
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
        return raw if path == "/" or path.startswith("/api/brief") else json.loads(raw)  # the page and the brief are plain text

    def need(condition, problem):
        if not condition:
            raise RuntimeError(problem)

    def page():
        need("<title>Hades</title>" in http("/"), "the page did not come back")

    def project():
        made = http("/api/new", {"project": PROJECT, "goal": "Find where the parser and its specification disagree."})
        need(made.get("goal", "").startswith("Find where"), f"unexpected answer: {made}")
        need(any(p["id"] == PROJECT for p in http("/api/projects")), "the new project is not listed")

    def report():
        event = http("/api/events", {"project": PROJECT, "type": "activity", "text": "Self-check says hello", "agent": "self-check"})
        need(event.get("type") == "activity", f"unexpected answer: {event}")
        try:
            http("/api/events", {"project": PROJECT, "type": "not-a-type", "text": "x"})
        except RuntimeError as e:
            need("400" in str(e), f"a malformed report should be refused with 400, got: {e}")
        else:
            raise RuntimeError("a malformed report was accepted")

    def brief():
        http("/api/project", {"project": PROJECT, "direction": "Start with the length fields."})
        text = http(f"/api/brief?project={PROJECT}&agent=self-check")
        need("Find where the parser" in text and "Start with the length fields." in text, "the brief is missing the goal or the direction")
        return f"{len(text.splitlines())} lines"

    def loop():
        stand_in = [sys.executable, "-c", "import sys; sys.stdin.read(); print('Handoff: Stand-in round.\\nResults: The header length is trusted [new]\\n"
                    "Ruled out: The checksum is not the cause\\nDecision needed: May I fuzz the live service?\\nNext: Read the parser')"]
        done = subprocess.run([sys.executable, str(server.ROOT / "scripts" / "loop.py"), PROJECT, "--agent", "self-check", "--max-iterations", "1", "--pause", "0", "--"] + stand_in,
                              env={**os.environ, "PROJECT_VIEW_URL": url}, cwd=tmp, capture_output=True, text=True, timeout=60)
        need(done.returncode == 0, f"loop.py exited with {done.returncode}: {(done.stdout + done.stderr)[-300:]}")
        events = http(f"/api/events?project={PROJECT}")["events"]
        need(any(e.get("section") == "round-notes" and "Stand-in round" in e.get("body", "") for e in events), "the round's output did not reach the Round notes page")
        need(any(e.get("section") == "now" and e.get("key") == "Current update" for e in events), "the handoff did not become the current update")
        return "one round with a stand-in harness"

    def memory():
        threads = {n["text"]: n for n in http(f"/api/research?project={PROJECT}")}
        need(threads.get("The header length is trusted", {}).get("status") == "supported", f"the result was not kept as a thread: {list(threads)}")
        need(threads.get("The checksum is not the cause", {}).get("status") == "contradicted", "the ruled-out approach was not kept")
        asked = [e for e in http(f"/api/events?project={PROJECT}")["events"] if e["type"] == "question"]
        need(asked and "fuzz the live service" in asked[0]["text"], "the decision was not kept as a question")
        http("/api/events", {"project": PROJECT, "type": "question", "id": asked[0]["id"], "answer": "No, only the local copy.", "source": "you"})
        text = http(f"/api/brief?project={PROJECT}&agent=self-check")
        need("Stand-in round" in text and "The checksum is not the cause" in text and "only the local copy" in text,
             "the next brief is missing the last handoff, the ruled-out approach or the person's answer")
        return "results, dead ends and decisions reach the next brief"

    def research():
        first = http("/api/events", {"project": PROJECT, "type": "research", "agent": "self-check", "kind": "hypothesis",
                                      "status": "open", "text": "A boundary may be wrong", "next": "Compare the guarded paths",
                                      "scope": "parser boundary", "snapshot": "v1",
                                      "evidence": [{"ref": "parser.py", "note": "Candidate source"}]})
        second = http("/api/events", {"project": PROJECT, "type": "research", "agent": "self-check", "kind": "observation",
                                       "status": "supported", "text": "The paths differ", "links": [{"to": first["id"], "relation": "supports"}]})
        http("/api/events", {"project": PROJECT, "type": "research", "id": first["id"], "agent": "self-check",
                             "kind": "hypothesis", "status": "supported", "text": "A boundary may be wrong"})
        nodes = {n["id"]: n for n in http(f"/api/research?project={PROJECT}")}
        need(nodes[first["id"]]["status"] == "supported" and nodes[first["id"]]["evidence"][0]["ref"] == "parser.py",
             "a research update lost its evidence")
        need(nodes[first["id"]]["scope"] == "parser boundary" and nodes[first["id"]]["snapshot"] == "v1",
             "a research update lost its scope or snapshot")
        need(len(nodes[first["id"]]["history"]) == 2 and nodes[first["id"]]["history"][0]["status"] == "open"
             and nodes[first["id"]]["history"][1]["status"] == "supported", "research history lost its changes")
        need(nodes[second["id"]]["links"][0]["to"] == first["id"], "research links were lost")
        need("A boundary may be wrong" in http(f"/api/brief?project={PROJECT}&agent=self-check"), "the brief omitted research")
        return "linked research survives updates and reaches the brief"

    def gating():
        home = server.PROJECTS / "self-check-gated"
        home.mkdir(parents=True)
        (home / "replay.py").write_text("import sys\nfrom pathlib import Path\nfound = Path(sys.argv[1]).read_text()\n"
                                        "print('read', sys.argv[1])\nsys.exit(0 if 'overflow' in found else 1)\n")
        (home / "scope.md").write_text("Only faults reachable from a file the user opens.\n")
        (home / "project.json").write_text(json.dumps({"goal": "Crash the parser", "artifact": "the path of a file holding the crashing input",
            "lock": ["scope.md", "replay.py"], "gates": [{"name": "reproduces", "command": [sys.executable, "{project}/replay.py"]},
                                                         {"name": "not known", "refute": "Look for an earlier report of this fault. Cite it if you find one."}]}))
        text = http("/api/brief?project=self-check-gated&agent=self-check")
        need("PASSES THE GATES, in this order: reproduces, not known" in text and "[check: <artifact>]" in text, "the brief does not explain the gates")
        # one stand-in for both kinds of round: a research round claims three results and a lead; a refuter round kills one claim
        (tmp / "standin.py").write_text("import sys\nbrief = sys.stdin.read()\nif 'Verdict:' in brief:\n"
            "    print('Verdict: refuted - reported upstream as issue 12' if 'footer' in brief else 'Verdict: stands - searched the tracker and the changelog')\n"
            "    sys.exit(0)\nfor name, body in (('good.txt', 'overflow'), ('also.txt', 'overflow'), ('bad.txt', 'nothing')):\n    open(name, 'w').write(body)\n"
            "print('Handoff: Three candidates.\\nResults:\\n- The length field overflows [new] [check: good.txt]\\n- The footer overflows [check: also.txt]\\n"
            "- The trailer overflows [check: bad.txt]\\n- The header is odd\\nLeads:\\n- The tokenizer on nested brackets: time a deep input [score: 5]\\nNext: x')\n")
        done = subprocess.run([sys.executable, str(server.ROOT / "scripts" / "loop.py"), "self-check-gated", "--agent", "self-check", "--max-iterations", "1", "--pause", "0",
                               "--", sys.executable, str(tmp / "standin.py")],
                              env={**os.environ, "PROJECT_VIEW_URL": url}, cwd=tmp, capture_output=True, text=True, timeout=90)
        need(done.returncode == 0, f"loop.py exited with {done.returncode}: {(done.stdout + done.stderr)[-300:]}")
        now = {n["text"]: n for n in http("/api/research?project=self-check-gated")}
        stand = {text: (n["state"], n["at"], n["standing"]) for text, n in now.items() if n["status"] != "open"}
        need(stand == {"The length field overflows": ("established", None, 2), "The footer overflows": ("failed", "not known", 1),
                       "The trailer overflows": ("failed", "reproduces", 0), "The header is odd": ("unchecked", "reproduces", 0)}, f"unexpected standing: {stand}")
        text = http("/api/brief?project=self-check-gated&agent=self-check")
        frame, later = text.partition("THIS ROUND'S FRAME")[2], text.partition("ALSO QUEUED")[2].partition("RULED OUT")[0]
        need("ESTABLISHED" in text and "issue 12" in text and f'[{now["The footer overflows"]["id"]}]' in frame, "the next brief does not lead with the claim nearest to established")
        need(later.index("tokenizer") < later.index("trailer"), "the queue is not ordered by standing, then score")
        (home / "scope.md").write_text("Only faults reachable over the network.\n")  # the person changes the question
        state = {n["text"]: n["state"] for n in http("/api/research?project=self-check-gated")}
        need(state["The length field overflows"] == "waiting", "a verdict outlived a change to the locked question")
        return "two gates in order, a refuter round, the queue, and a stale verdict"

    steps = [("The page loads", page), ("A project is created", project), ("A report is accepted and a malformed one refused", report),
             ("The brief is written", brief), ("The loop runs a round", loop), ("The round's handoff becomes the record", memory),
             ("Research threads stay linked", research), ("The gates decide what is established", gating)]
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
    if server.LAUNCH_FILE.exists():
        return
    if not shutil.which("claude"):
        print("  Install Claude Code (the `claude` command) and run this again to use the Start button.")
        return
    if not yes:
        if not sys.stdin.isatty():
            return
        print("The Start button on the page runs Claude Code on this machine, in agent-work/, with your Claude account.\n"
              "It may run shell commands and edit files there without asking. You can change this later in launchers.json.")
        try:
            yes = input("Allow the Start button? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:  # no answer is a no
            yes = False
    if yes:
        shutil.copy(server.ROOT / "launchers.example.json", server.LAUNCH_FILE)
        print("  Start is on. The command it runs is in launchers.json; edit it any time, no restart needed.")
    else:
        print("  Start is off. To turn it on later, run this again with --allow-launch.")


def main(argv):
    if argv[:1] == ["check"]:
        sys.exit(0 if check() else 1)
    if [a for a in argv if a not in ("--no-open", "--skip-check", "--allow-launch")]:
        sys.exit(__doc__.strip())
    here = f"http://127.0.0.1:{server.PORT}"
    if listening(server.PORT):
        sys.exit(f"Something is already listening on port {server.PORT}. If that is Hades, it is at {here} .\n"
                 "To run a second copy, set PROJECT_VIEW_PORT to another port.")
    if "--skip-check" not in argv:  # in its own process, so nothing from the check lingers in this one
        if subprocess.run([sys.executable, str(Path(__file__).resolve()), "check"]).returncode:
            sys.exit(1)
        print()
    if not server.project_ids():
        server.starter()
    offer_launch("--allow-launch" in argv)
    print(f"Open your project dashboard at {here}/\nCtrl-C stops it.\n")
    if "--no-open" not in argv:
        threading.Timer(1, lambda: webbrowser.open(f"{here}/")).start()
    try:
        server.serve()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main(sys.argv[1:])
