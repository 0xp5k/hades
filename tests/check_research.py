"""Exercise the person's standing direction, research priority, the handoff record and terminal messages through the HTTP API, without a model.

A stand-in `claude` replies to each round at once, so a launched worker can be messaged and restarted for real."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys, time
sys.stdin.readline()
time.sleep(1)
print(json.dumps({"type": "result", "subtype": "success", "result": "Handoff: tried the parser; the length check holds."}), flush=True)
for line in sys.stdin:
    print(json.dumps({"type": "result", "subtype": "success", "result": "Heard you."}), flush=True)
"""


def alive(pid):
    """Whether a process is running, without signals on Windows (os.kill there would end it)."""
    if WINDOWS:
        shown = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        return f'"{pid}"' in shown
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def check(directory):
    work = Path(directory)
    for name in ("server.py", "index.html", "AGENTS.md"):
        shutil.copy(ROOT / name, work)
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "loop.py", work / "scripts")
    (work / "bin").mkdir()
    if WINDOWS:  # Windows starts a .cmd, not a script with a #! line
        (work / "bin" / "claude.py").write_text(FAKE_CLAUDE)
        claude = work / "bin" / "claude.cmd"
        claude.write_text(f'@echo off\r\n"{sys.executable}" "{work / "bin" / "claude.py"}" %*\r\n')
    else:
        claude = work / "bin" / "claude"
        claude.write_text(FAKE_CLAUDE.replace("python3", sys.executable, 1))
        claude.chmod(0o755)
    batch = [sys.executable, "-c", "import sys; sys.stdin.read(); print('Handoff: nothing')"]
    recipes = {"fake": {"command": [str(claude), "-p"], "rounds": 200, "timeout_min": 1},
               "wrap": {"label": "Wrapper", "command": [*batch, "-m", "base-model"], "models": ["base-model", "other"], "rounds": 200},
               "bare": {"command": batch}, "broken": {"command": []}}
    (work / "launchers.json").write_text(json.dumps(recipes))
    for project, cfg in (("plain", {"goal": "Find the parser bug", "sections": [], "gates": []}),  # "gates": [] turns the built-in refuter off
                         ("lean", {"target": "parser.c", "goal": "Find the parser bug", "rules": "Stay local.", "gates": []}),
                         ("blank", {"goal": ""}),
                         ("style", {"goal": "Map the parser", "gates": []}),
                         ("old", {"goal": "A project from before", "phase": "run", "simple": False, "metric": {"verify": "python3 score.py"}})):
        (work / "projects" / project).mkdir(parents=True)
        (work / "projects" / project / "project.json").write_text(json.dumps(cfg))
    # A log written before runs, verdicts, findings and tasks were removed still loads; only what is still known is kept.
    (work / "projects/old/events.jsonl").write_text("\n".join([
        json.dumps({"type": "run", "id": "r1", "text": "an attempt", "artifact": {}}), json.dumps({"type": "verdict", "run": "r1", "ok": True}),
        json.dumps({"type": "finding", "text": "a finding"}), json.dumps({"type": "task", "id": "t4", "text": "a task", "status": "queued"}),
        "not json", json.dumps({"type": "research", "id": "k2", "kind": "hypothesis", "status": "open", "text": "Old lead"})]) + "\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def call(route, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(base + route, body, {"Content-Type": "application/json", **(headers or {})}), timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw.decode()

    def refused(route, data, code=400, headers=None):
        try:
            call(route, data, headers)
        except HTTPError as error:
            assert error.code == code, (route, data, error.code, error.read())
            return
        raise AssertionError(f"{route} accepted {data}")

    def start():
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=work, env={**os.environ, "PROJECT_VIEW_PORT": str(port)})
        for _ in range(100):
            try:
                call("/api/projects")
                return proc
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    brief = lambda project, agent="a1": call(f"/api/brief?project={project}&agent={agent}")
    config = lambda project: next(p["config"] for p in call("/api/projects") if p["id"] == project)
    server = start()
    try:
        # The old log: its research thread survives, the rest is left out, and a new thread never takes an id already in use.
        assert [e["type"] for e in call("/api/events?project=old")["events"]] == ["research"]
        assert next(p for p in call("/api/projects") if p["id"] == "old")["count"] == 1
        fresh = call("/api/events", {"project": "old", "type": "research", "kind": "hypothesis", "status": "open", "text": "New lead"})
        assert fresh["id"] != "k2" and sorted(t["text"] for t in call("/api/research?project=old")) == ["New lead", "Old lead"], fresh
        assert "A project from before" in brief("old") and "Old lead" in brief("old")
        for gone in ("run", "verdict", "finding", "task", "tool", "review", "proposal"):
            refused("/api/events", {"project": "old", "type": gone, "text": "x", "run": "r1", "id": "t4"})
        # A mistyped project id is refused everywhere; it never starts a second project.
        for route, body in (("/api/events", {"type": "activity", "text": "x"}), ("/api/frames", {"frames": ["somewhere"]}), ("/api/project", {"goal": "g"}), ("/api/launch", {})):
            refused(route, {"project": "odl", **body})
        assert not (work / "projects" / "odl").exists()
        try:
            call("/api/events", {"project": "old", "type": "question", "id": "q999", "answer": "no", "source": "you"})
            raise AssertionError("an answer to an assumption that does not exist was accepted")
        except HTTPError as error:
            assert "no assumption or question has the id q999" in json.loads(error.read())["error"]
        try:
            call("/api/events", {"project": "old", "type": "research", "id": "k2", "gate": "refutation", "verdict": "passed"})
            raise AssertionError("a reported verdict was accepted")
        except HTTPError as error:
            assert "recorded by the loop" in json.loads(error.read())["error"]
        for gone in ("/api/claim", "/api/slot", "/api/approve", "/api/coordinate", "/mcp"):
            refused(gone, {"project": "old", "agent": "a1"}, 404)

        # A. The standing direction: set, shown first in the brief, removed with an empty string.
        steer = "Focus on the length check in parse_header; skip the network code."
        for project in ("plain", "lean"):
            assert config(project)["direction"] == ""
            assert call("/api/project", {"project": project, "direction": "  " + steer + "\n"})["direction"] == steer
            assert config(project)["direction"] == steer
            text = brief(project)
            assert text.startswith("THE PERSON'S CURRENT DIRECTION") and text.splitlines()[1] == steer, text[:300]
            assert "outranks your own choice of next step" in text
        refused("/api/project", {"project": "plain", "direction": "x" * 4001})
        refused("/api/project", {"project": "plain", "direction": 5})
        call("/api/project", {"project": "plain", "direction": ""})
        assert config("plain")["direction"] == "" and "direction" not in json.loads((work / "projects/plain/project.json").read_text())
        assert "CURRENT DIRECTION" not in brief("plain")
        call("/api/project", {"project": "plain", "direction": steer})

        # B. Priority: the person's partial update marks a thread without touching anything else; an agent's update keeps the mark.
        for project in ("plain", "lean"):
            ev = lambda **k: call("/api/events", {"project": project, "type": "research", **k})
            ev(id="old", agent="a1", kind="hypothesis", status="open", text="Older lead", next="Fuzz the footer")
            ev(id="len", agent="a1", kind="hypothesis", status="open", text="Header length is trusted", next="Send a 70000-byte header",
               evidence=[{"ref": "notes/len.md", "note": "read path"}])
            ev(id="new", agent="a1", kind="observation", status="open", text="Newest lead")
            ev(id="dead", agent="a1", kind="hypothesis", status="contradicted", text="Footer overflows", conditions="footer is bounds-checked")
            assert call(f"/api/research?project={project}")[0]["id"] == "dead"
            ev(id="len", priority=True, source="you")
            threads = call(f"/api/research?project={project}")
            top = threads[0]
            assert top["id"] == "len" and top["priority"] is True and all(t["priority"] is False for t in threads[1:])
            assert (top["text"], top["status"], top["kind"], top["next"], top["evidence"][0]["ref"], top["agent"]) == \
                ("Header length is trusted", "open", "hypothesis", "Send a 70000-byte header", "notes/len.md", "a1"), top
            assert top.get("source") != "you" and len(top["history"]) == 2 and top["history"][-1]["priority"] is True
            assert [t["id"] for t in threads[1:]] == ["dead", "new", "old"]  # a mark does not make a thread look recently worked on
            ev(id="len", agent="a1", kind="hypothesis", status="supported", text="Header length is trusted", priority=False)
            top = call(f"/api/research?project={project}")[0]
            assert top["id"] == "len" and top["priority"] is True and top["status"] == "supported" and top["next"] == "Send a 70000-byte header"
            refused("/api/events", {"project": project, "type": "research", "id": "nope", "priority": True, "source": "you"})
            refused("/api/events", {"project": project, "type": "research", "id": "len", "priority": "yes", "source": "you"})
            refused("/api/events", {"project": project, "type": "research", "id": "len", "priority": True})  # an agent's partial update
            # A focused round is given one frame: the oldest open one. The rest are titles; a claim the person marked says so.
            text = brief(project)
            frame = text.partition("THIS ROUND'S FRAME (round 1 of 3 on it)")[2]
            assert "- [old] hypothesis / open: Older lead" in frame and "Next test: Fuzz the footer" in frame, text
            assert "a Results line that starts with [old], or" in frame and "a Ruled out line that starts with [old]" in frame, frame
            assert "Work as if this frame pays off" not in text  # nothing here can say no, so nothing is assumed
            assert "- [new] Newest lead" in text.partition("ALSO QUEUED")[2].partition("RULED OUT")[0], text
            assert "- [len] Header length is trusted (the person's priority)" in text.partition("CLAIMED (another round's word")[2].partition("ALSO QUEUED")[0], text
            assert "Footer overflows" not in text.partition("RULED OUT")[0]
            plan = call(f"/api/brief?project={project}&agent=a1&as=json")
            assert plan["frame"] == "old" and plan["brief"] == text
            ev(id="new", priority=True, source="you")  # Pursue: the person's mark goes to the front
            marked = brief(project).partition("THIS ROUND'S FRAME (round 1 of 3 on it; the person marked it)")[2]
            assert "- [new] observation / open: Newest lead" in marked, brief(project)
            ev(id="new", priority=False, source="you")
        ev = lambda **k: call("/api/events", {"project": "plain", "type": "research", **k})
        ev(id="len", priority=False, source="you")
        assert call("/api/research?project=plain")[0]["priority"] is False
        assert "(the person's priority)" not in brief("plain")
        ev(id="len", priority=True, source="you")

        # 1. The brief asks for the work and the handoff only: no commands to run, nothing to report.
        lean = brief("lean")
        for expected in ("RULED OUT (do not retry without a new reason", "- Footer overflows (why: footer is bounds-checked)",
                         "Next test: Fuzz the footer", "Settle one frame this round.", "The frame is named at the end of this brief", "Nobody is watching this round.",
                         "recorded on the dashboard for you", "every result you report is a claim", "TARGET: parser.c", "RULES: Stay local."):
            assert expected in lean, (expected, lean)
        for absent in ("POST", "curl", "http://", "checker", "verifier", "task"):
            assert absent not in lean, (absent, lean)
        assert len(lean.splitlines()) < 50, len(lean.splitlines())

        # A project with no goal yet asks for one instead of inventing work.
        assert "GOAL: none yet. Do not invent work: say in your handoff that the project has no goal, and stop." in brief("blank")
        call("/api/project", {"project": "blank", "direction": "Look at the tokenizer."})
        assert "GOAL: none yet. Work from the direction above." in brief("blank") and "Do not invent" not in brief("blank")
        # With no gates of its own a project gets the built-in refuter, so nothing is taken on a round's word and nobody sets anything up.
        default = next(p for p in call("/api/projects") if p["id"] == "blank")["config"]
        assert default["default_gates"] is True and [g["name"] for g in default["gates"]] == ["refutation"] and "refute" in default["gates"][0] and len(default["fingerprint"]) == 16
        assert "A RESULT COUNTS ONLY WHEN IT PASSES THE GATE: refutation." in brief("blank") and "a separate round that sees only your claim" in brief("blank")
        off = next(p for p in call("/api/projects") if p["id"] == "plain")["config"]
        assert (off["default_gates"], off["gates"], off["fingerprint"]) == (False, [], "")
        assert "GOAL: none yet" not in lean and "AGENTS.md" not in lean

        # A new project is ready to run at once; a Library page needs a title and a body.
        made = call("/api/new", {"project": "fresh", "goal": "Bound the constant", "target": "  the lemma in notes.tex "})
        assert (made["goal"], made["target"], made["sections"]) == ("Bound the constant", "the lemma in notes.tex", [{"id": "now", "title": "Project brief", "view": "note"}]), made
        assert "GOAL: Bound the constant" in brief("fresh") and call("/api/project?project=fresh")["hold"] is None
        refused("/api/new", {"project": "fresh", "goal": "again"})
        refused("/api/new", {"project": "bad name", "goal": "x"})
        call("/api/project", {"project": "fresh", "paused": True})
        assert call("/api/project?project=fresh")["hold"] == "paused by the person"
        page = call("/api/events", {"project": "fresh", "type": "topic", "key": "Why the bound holds", "body": "Because.", "diagram": {"nodes": []}})
        assert page["key"] == "Why the bound holds" and "diagram" not in page
        refused("/api/events", {"project": "fresh", "type": "topic", "key": "No body"})
        made = call("/api/project", {"project": "fresh", "section": {"id": "refs", "title": "References", "view": "board", "statuses": ["a"], "measured": {"a": 1}}})
        assert made["sections"][-1] == {"id": "refs", "title": "References", "view": "note"}, made

        # 2. The handoff is the record: the current update, a thread per result, ruled-out approach and lead, a question per decision.
        before = len(call("/api/research?project=lean"))
        def note(project, body, agent="a1"):
            call("/api/events", {"project": project, "type": "item", "section": "round-notes", "agent": agent, "key": f"Round · {time.time()}", "body": body})
        note("lean", "Earlier Handoff: a draft.\nMore work.\n\n**Handoff:** Checked the header path. Only you can say whether fuzzing is in scope.\n"
                     "**Results:**\n- Off-by-one in footer length [known: CVE-2020-1]\n1. Parser crashes on empty input [new]\n"
                     "- **Header length is trusted.**\n* parser crashes on empty  input\nRuled out: none\n"
                     "Decision needed: Is fuzzing the live service in scope?\nNext: try the footer")
        threads = call("/api/research?project=lean")
        made = [t for t in threads if t.get("origin") == "handoff"]
        assert len(threads) == before + 2 and len(made) == 2, made
        by = {t["text"]: t for t in made}
        off = by["Off-by-one in footer length"]
        assert (off["kind"], off["status"], off["conditions"], off["agent"]) == ("observation", "contradicted", "already known: CVE-2020-1", "a1") and "known" not in off
        crash = by["Parser crashes on empty input"]
        assert crash["known"] == "new"
        now = [e for e in call("/api/events?project=lean")["events"] if e["type"] == "item" and e.get("section") == "now"][-1]
        assert now["key"] == "Current update" and now["body"].startswith("**Handoff:** Checked") and now["agent"] == "a1"
        # The decision reaches the person as a question; it is not asked twice, and their answer is given to later rounds as decided.
        asked = [e for e in call("/api/events?project=lean")["events"] if e["type"] == "question"]
        assert [q["text"] for q in asked] == ["Is fuzzing the live service in scope?"] and asked[0]["origin"] == "handoff", asked
        waiting = brief("lean", "a2")
        assert "ASSUMPTIONS IN FORCE (earlier rounds made them" in waiting and "Is fuzzing the live service in scope?" in waiting and "THE PERSON DECIDED" not in waiting
        assert "WAITING ON THE PERSON" not in waiting  # the loop never waits on anyone
        note("lean", "Handoff: again\nResults: none\nDecision needed: is fuzzing the live service in scope\nNext: x")
        assert len([e for e in call("/api/events?project=lean")["events"] if e["type"] == "question"]) == 1
        call("/api/events", {"project": "lean", "type": "question", "id": asked[0]["id"], "answer": "No. Local copies only.", "source": "you"})
        decided = brief("lean", "a2")
        assert "THE PERSON DECIDED (treat as fact)" in decided and "Answer: No. Local copies only." in decided and "ASSUMPTIONS IN FORCE" not in decided
        # What a round would once have asked, it now assumes, cautiously, and says so; that is in force until the person says otherwise.
        note("lean", "Handoff: again\nResults: none\nAssumed: the staging host is out of bounds, so I only used the local copy\nNext: x")
        assumed = [e for e in call("/api/events?project=lean")["events"] if e["type"] == "question"]
        assert len({e["id"] for e in assumed}) == 2 and assumed[-1]["text"] == "the staging host is out of bounds, so I only used the local copy", assumed
        assert "- the staging host is out of bounds" in brief("lean", "a2").partition("ASSUMPTIONS IN FORCE")[2]
        note("lean", "handoff: second round\nResult: nothing\nRULED OUT:\n* Brute force over 2^32: too slow\n  on this laptop\n"
                     "2) Fuzzing the footer: footer is bounds-checked\nNext: x")
        ruled = [t for t in call("/api/research?project=lean") if t.get("origin") == "handoff" and t["status"] == "contradicted"]
        assert sorted(t["text"] for t in ruled) == ["Brute force over 2^32: too slow on this laptop", "Fuzzing the footer: footer is bounds-checked", "Off-by-one in footer length"], ruled
        assert all(t["kind"] == "observation" for t in ruled)
        # Leads, in any style: open hypotheses, without repeating a thread that exists
        note("lean", "Handoff: wandered\nResults: none\nLeads:\n- Tokens over 64k wrap: send 65537 bytes\n- Header length is trusted\nLead: the footer\nNext: x")
        leads = [t for t in call("/api/research?project=lean") if t.get("origin") == "handoff" and t["kind"] == "hypothesis"]
        assert sorted(t["text"] for t in leads) == ["Tokens over 64k wrap: send 65537 bytes", "the footer"] and all(t["status"] == "open" for t in leads), leads
        count = len(call("/api/events?project=lean")["events"])
        note("lean", "No marker here.\nResults:\n- something")
        tail = call("/api/events?project=lean")["events"][count:]  # the note itself, and a line in the log saying nothing was recorded from it
        assert [e["type"] for e in tail] == ["item", "activity"] and "without a Handoff block" in tail[1]["text"], tail
        note("plain", "Handoff: done\nResults:\n- a claim")
        assert [t["text"] for t in call("/api/research?project=plain") if t.get("origin")] == ["a claim"]
        lean = brief("lean", "a2")
        assert "- Brute force over 2^32: too slow on this laptop" in lean and "Parser crashes on empty input" in lean
        assert "WHERE THINGS STAND" in lean and "Handoff: wandered" in lean
        # The person's Drop and Reopen: a status change alone keeps everything else.
        call("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "contradicted", "source": "you"})
        dropped = next(t for t in call("/api/research?project=lean") if t["id"] == crash["id"])
        assert dropped["status"] == "contradicted" and dropped["text"] == crash["text"] and dropped["known"] == "new" and dropped["agent"] == "a1"
        assert "- Parser crashes on empty input" in brief("lean", "a2").split("RULED OUT (do not")[1]
        call("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "open", "source": "you"})
        assert next(t for t in call("/api/research?project=lean") if t["id"] == crash["id"])["status"] == "open"
        refused("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "gone", "source": "you"})
        # Three rounds in a row that record nothing are called out in the next brief.
        for n in (1, 2, 3):
            call("/api/events", {"project": "lean", "type": "activity", "agent": "a1", "text": f"Iteration {n} finished (exit 0)"})
        assert "THE LAST 3 ROUNDS RECORDED NOTHING NEW" in brief("lean")

        # C. A message typed in the terminal reaches the live worker and every later round's brief.
        started = call("/api/launch", {"project": "lean", "recipe": "fake"})["started"]
        assert started == ["fake-1"], started
        worker = next(x for x in call("/api/projects") if x["id"] == "lean")["launched"][0]
        assert worker["interactive"] and worker["alive"]
        try:  # one loop per project
            call("/api/launch", {"project": "lean", "recipe": "wrap"})
            raise AssertionError("a second loop started on the same project")
        except HTTPError as error:
            assert error.code == 400 and "already running on this project (fake-1)" in json.loads(error.read())["error"]
        page = {"Origin": base, "X-Hades-Terminal": "1"}
        said = "Ignore the footer from now on — it is out of scope."
        message = {"project": "lean", "agent": "fake-1", "session": worker["session"], "text": said}
        refused("/api/terminal/send", message, 403, {"Origin": "https://other.example", "X-Hades-Terminal": "1"})
        refused("/api/terminal/send", {**message, "session": "stale"}, 400, page)
        assert call("/api/terminal/send", message, page) == {"sent": True}
        call("/api/terminal/send", {**message, "text": "Second note " + "y" * 3000}, page)
        assert "text" in call(f"/api/terminal?project=lean&agent=fake-1&session={worker['session']}")
        refused(f"/api/terminal?project=lean&agent=fake-1&session={worker['session']}", None, 403, {"Host": "evil.example"})
        log = call("/api/events?project=lean")["events"]
        kept = [e for e in log if e["type"] == "activity" and e.get("source") == "you" and e.get("harness") == "terminal"]
        assert [e["text"] for e in kept][0] == said and kept[0]["key"] == "fake-1" and len(kept[1]["body"]) > 2000
        for project, agent in (("lean", "fake-1"), ("lean", "someone-else")):
            text = brief(project, agent)
            assert "WHAT THE PERSON SAID IN THE TERMINAL TO EARLIER ROUNDS" in text and said in text
            assert text.index("CURRENT DIRECTION") < text.index("SAID IN THE TERMINAL") < text.index(said) < text.index("Second note")
            assert "y" * 601 not in text  # long messages are clipped in the brief
        call("/api/events", {"project": "plain", "type": "activity", "source": "you", "harness": "terminal", "key": "w", "text": "Use the fixture corpus."})
        assert "Use the fixture corpus." in brief("plain")
        # The closing note loop.py saves after a round (the live round above stays open for a reply, so post one as it would)
        call("/api/events", {"project": "lean", "type": "item", "section": "round-notes", "agent": "a1", "key": "Round 1 · a1",
                             "body": "Handoff: tried the parser; the length check holds."})
        text = brief("lean", "a1")
        assert "YOUR CLOSING NOTE FROM YOUR LAST ROUND" in text and "the length check holds" in text
        assert "YOUR CLOSING NOTE" not in brief("lean", "a2")

        # A restart forgets the launched loop, but it keeps going: a new launch is still refused, and stopping still reaches it.
        orphan = int((work / "agent-work/lean/fake-1/loop.pid").read_text())
        server.terminate()
        server.wait(timeout=10)
        server = start()
        assert alive(orphan)
        refused("/api/launch", {"project": "lean", "recipe": "fake"})
        assert call("/api/stop", {"project": "lean"})["stopped"] == ["fake-1"]
        assert not alive(orphan), "the loop from before the restart was not stopped"

        def launch(project="lean", **settings):  # a project runs one loop, so the last one is stopped first
            call("/api/stop", {"project": project})
            for stale in (work / "agent-work" / project).glob("*/argv.json"):
                stale.unlink()
            return call("/api/launch", {"project": project, **settings})

        # 4. Launchers as the page offers them, and the model chosen for one launch.
        listing = next(x for x in call("/api/projects") if x["id"] == "lean")
        offered = listing["launchers"]
        assert sorted(offered) == ["bare", "fake", "wrap"] and offered["wrap"]["label"] == "Wrapper"
        assert sorted(listing) == ["config", "count", "epoch", "hold", "id", "launched", "launcherError", "launchers"], sorted(listing)
        assert offered["fake"] == {"label": "fake", "model": "", "models": ["fable", "opus", "sonnet", "haiku"], "interactive": True,
                                   "command": [str(claude), "-p"], "takes_tools": True, "rounds": 200, "timeout_min": 1, "reply_wait": 120, "keep_background": False, "tools": "recipe"}, offered["fake"]
        assert offered["wrap"]["model"] == "base-model" and offered["wrap"]["models"] == ["base-model", "other"] and not offered["wrap"]["interactive"]
        assert offered["bare"]["models"] == [] and "broken" in listing["launcherError"]
        out = launch(recipe="fake", model="fable")
        assert out["model"] == "fable" and out["started"] == ["fake-1"]
        assert next(x for x in call("/api/projects") if x["id"] == "lean")["launched"][-1]["model"] == "fable"
        assert launch(recipe="wrap", model="other")["model"] == "other"
        assert launch(recipe="wrap")["model"] == "base-model"
        call("/api/stop", {"project": "lean"})
        refused("/api/launch", {"project": "lean", "recipe": "bare", "model": "opus"})
        refused("/api/launch", {"project": "lean", "recipe": "fake", "model": "bad model; rm"})
        (work / "launchers.json").write_text("{")
        listing = next(x for x in call("/api/projects") if x["id"] == "lean")
        assert listing["launchers"] == {} and "could not be read" in listing["launcherError"]
        (work / "launchers.json").write_text(json.dumps(recipes))
        assert next(x for x in call("/api/projects") if x["id"] == "lean")["launcherError"].startswith("Skipped")

        # Loop settings: saved once per project, validated, and resolved request > project > recipe > default.
        assert config("lean")["loop"] == {}
        assert offered["fake"]["rounds"] == 200 and offered["fake"]["timeout_min"] == 1 and offered["fake"]["reply_wait"] == 120
        assert offered["bare"]["rounds"] == 4 and offered["bare"]["timeout_min"] == 0 and offered["bare"]["keep_background"] is False
        saved = call("/api/project", {"project": "lean", "loop": {"recipe": "wrap", "model": "other", "rounds": 7, "timeout_min": 0,
                                                                   "reply_wait": 0, "keep_background": True, "junk": 1, "label": None, "agents": 3, "style": "explore"}})["loop"]
        assert saved == {"recipe": "wrap", "model": "other", "rounds": 7, "timeout_min": 0, "reply_wait": 0, "keep_background": True}, saved
        for bad, key in (({"rounds": -1}, "loop.rounds"), ({"tools": "all"}, "loop.tools"), ({"timeout_min": 1441}, "loop.timeout_min"), ({"reply_wait": 1.5}, "loop.reply_wait"),
                         ({"keep_background": "yes"}, "loop.keep_background"), ({"model": "a b"}, "loop.model"), ("fast", "loop must")):
            try:
                call("/api/project", {"project": "lean", "loop": bad})
                raise AssertionError(f"loop {bad} was accepted")
            except HTTPError as error:
                assert error.code == 400 and key in json.loads(error.read())["error"], bad
        assert config("lean")["loop"] == saved
        refused("/api/project", {"project": "lean", "loop": {"recipe": "nope"}})  # a recipe that is not in launchers.json
        # A project.json that does not parse is never written over, and the loop holds instead of running without its goal and gates.
        good = (work / "projects/blank/project.json").read_text()
        (work / "projects/blank/project.json").write_text('{"goal": "g", "gates": [],}')
        refused("/api/project", {"project": "blank", "goal": "replaced"})
        assert (work / "projects/blank/project.json").read_text() == '{"goal": "g", "gates": [],}'
        assert "cannot be read" in call("/api/project?project=blank")["hold"] and "PROBLEM: project.json cannot be read" in call("/api/status?project=blank")
        (work / "projects/blank/project.json").write_text(good)
        assert call("/api/project?project=blank")["hold"] is None
        # A stand-in loop.py records the arguments each launch would run with
        real = (work / "scripts/loop.py").read_text()
        (work / "scripts/loop.py").write_text("import json, sys, time\nfrom pathlib import Path\ncan_converse = lambda command: False\n"
                                              "if __name__ == '__main__':\n    Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n    time.sleep(60)\n")
        def argv_of(project, agent):
            for _ in range(100):
                try:
                    return json.loads((work / "agent-work" / project / agent / "argv.json").read_text())
                except (OSError, ValueError):
                    time.sleep(0.1)
            raise AssertionError("the launched loop did not start")
        def flag(argv, name):
            return argv[argv.index(name) + 1]
        try:
            out = launch()  # everything from the project's saved settings
            assert out["recipe"] == "wrap" and out["settings"] == {"rounds": 7, "timeout_min": 0, "reply_wait": 0, "keep_background": True, "tools": "recipe", "model": "other"}, out
            argv = argv_of("lean", out["started"][0])
            assert (flag(argv, "--max-iterations"), flag(argv, "--timeout-min"), flag(argv, "--reply-wait")) == ("7", "0", "0") and "--keep-background" in argv
            assert flag(argv, "-m") == "other" and flag(argv, "--agent") == "wrap-1"
            entry = next(x for x in next(p for p in call("/api/projects") if p["id"] == "lean")["launched"] if x["agent"] == out["started"][0])
            assert entry["settings"] == out["settings"]
            out = launch(recipe="fake", rounds=3, keep_background=False)  # the request wins
            assert out["settings"] == {"rounds": 3, "timeout_min": 0, "reply_wait": 0, "keep_background": False, "tools": "recipe", "model": "other"}, out
            argv = argv_of("lean", out["started"][0])
            assert flag(argv, "--max-iterations") == "3" and "--keep-background" not in argv and flag(argv, "--model") == "other"
            call("/api/stop", {"project": "lean"})
            refused("/api/launch", {"project": "lean", "recipe": "fake", "timeout_min": -1})
            call("/api/project", {"project": "lean", "loop": {}})  # cleared: the recipe's own fields, then the defaults
            assert config("lean")["loop"] == {}
            refused("/api/launch", {"project": "lean"})  # three recipes and none chosen
            out = launch(recipe="fake")
            assert out["settings"] == {"rounds": 200, "timeout_min": 1, "reply_wait": 120, "keep_background": False, "tools": "recipe", "model": ""}, out
            assert flag(argv_of("lean", out["started"][0]), "--timeout-min") == "1"
            (work / "launchers.json").write_text(json.dumps({"bare": recipes["bare"]}))  # one recipe: it is used without being named
            out = launch()
            assert out["recipe"] == "bare" and out["settings"]["rounds"] == 4 and out["settings"]["timeout_min"] == 0
            argv = argv_of("lean", out["started"][0])
            assert flag(argv, "--reply-wait") == "120" and flag(argv, "--timeout-min") == "0"  # research has no time limit by default
            call("/api/project", {"project": "lean", "loop": {"rounds": 0}})  # rounds 0: until stopped
            out = launch()
            assert out["settings"]["rounds"] == 0 and flag(argv_of("lean", out["started"][0]), "--max-iterations") == "0"
            call("/api/stop", {"project": "lean"})
            call("/api/project", {"project": "lean", "loop": {}})

            # There is no round style to set. With nothing queued a round widens; with a frame queued it settles that frame.
            wide = brief("style", "solo")
            for expected in ("Follow your nose this round: nothing is queued", "Breadth beats proof", "in parallel", "Leads: line", "dead ground", "[score: 4] you saw direct evidence"):
                assert expected in wide, (expected, wide)
            assert "THIS ROUND'S FRAME" not in wide and call("/api/brief?project=style&agent=solo&as=json")["frame"] is None
            queued = call("/api/frames", {"project": "style", "frames": [{"text": "the tokenizer on nested brackets", "score": 2, "next": "time a deep input"}]})["added"]
            narrow = brief("style", "solo")
            assert "Settle one frame this round." in narrow and "Follow your nose" not in narrow and f"- [{queued[0]}] hypothesis / open: the tokenizer" in narrow.partition("THIS ROUND'S FRAME (round 1 of 2 on it)")[2]
            call("/api/project", {"project": "style", "loop": {"recipe": "bare"}})
            out = launch("style")
            assert out["started"] == ["bare-1"] and "style" not in out["settings"], out
            call("/api/stop", {"project": "style"})
            # The digest an assistant reads first: is it running, on what, how far each claim got, what is next.
            digest = call("/api/status?project=style")
            for expected in ('Hades project "style"', "Goal: Map the parser", "Loop: not running (Loop stopped: stopped by the person",
                             "Gates: none: every result is only a claim", "0 established", "1 queued", f"- [{queued[0]}] the tokenizer on nested brackets (0 of 2 rounds spent)", "/AGENTS.md"):
                assert expected in digest, (expected, digest)
            lean_digest = call("/api/status?project=lean")
            assert "ASSUMPTIONS IN FORCE" in lean_digest and "the staging host is out of bounds" in lean_digest and "LATEST HANDOFF (a1" in lean_digest, lean_digest
            assert "style: Map the parser" in call("/api/status") and "Gates: refutation (refuter) [built in; the project set none]" in call("/api/status?project=blank")
            refused("/api/status?project=nope", None)
        finally:
            (work / "scripts/loop.py").write_text(real)
            (work / "launchers.json").write_text(json.dumps(recipes))
    finally:
        for project in ("lean", "style"):
            try:
                call("/api/stop", {"project": project})
            except OSError:
                pass
        server.terminate()
        server.wait(timeout=10)


def check_units():
    """The pieces that can be checked in process, against a scratch folder."""
    sys.path.insert(0, str(ROOT))
    import server
    with tempfile.TemporaryDirectory() as folder:
        server.PROJECTS = Path(folder)
        first = server.starter()
        assert first["goal"] == "" and first["loop"] == {} and first["sections"] == [{"id": "now", "title": "Project brief", "view": "note"}], first
        w = server.with_model
        assert w(["claude", "-p"], "opus") == ["claude", "--model", "opus", "-p"]
        assert w(["claude", "-p", "--model", "sonnet"], "opus") == ["claude", "-p", "--model", "opus"]
        assert w(["claude", "--model=sonnet"], "opus") == ["claude", "--model=opus"]
        assert w(["./wrap.sh", "-m", "gpt"], "o3") == ["./wrap.sh", "-m", "o3"]
        try:  # "python3 -m pkg" names a module, so this recipe names no model
            w(["python3", "-m", "harness"], "opus")
            raise AssertionError("a module was taken for a model")
        except ValueError:
            pass
        items = server.handoff_items("Handoff: x\n__Results__: one\n  two continues\n- three\nRuled-out: n/a\nNext: y")
        assert items == {"results": ["one two continues", "three"], "ruled out": [], "decision": [], "leads": [], "next": ["y"]}, items
        leads = server.handoff_items("Handoff: x\n**Leads:**\n1. Tokens over 64k wrap: send 65537 bytes\nLead: the footer\nNext: y")
        assert leads["leads"] == ["Tokens over 64k wrap: send 65537 bytes", "the footer"], leads
        # The Windows process helpers, with the Windows branch forced: their command lines and parsing
        assert server.own_group(False) == {"start_new_session": True} and "creationflags" in server.own_group(True)
        assert server.end_command(42, True) == ["taskkill", "/PID", "42", "/T", "/F"] and server.end_command(42, False) is None
        assert server.process_query(42, True) == ["tasklist", "/FI", "PID eq 42", "/FO", "CSV", "/NH"]
        assert server.process_query(42, False) == ["ps", "-p", "42", "-o", "command="]
        assert server.is_loop('"python.exe","42","Console","1","20,000 K"\n', "p", "a", True)
        assert not server.is_loop('"notepad.exe","42","Console","1","2,000 K"', "p", "a", True)
        assert not server.is_loop("INFO: No tasks are running which match the specified criteria.", "p", "a", True)
        assert server.is_loop("/usr/bin/python3 scripts/loop.py p --agent a --harness x", "p", "a", False)
        assert server.quiet(False) == {} and server.quiet(True)["creationflags"]
        assert server.resolve_program(["no-such-program-hades", "-p"]) == ["no-such-program-hades", "-p"]
        assert server.resolve_program([os.path.join("bin", "claude")]) == [os.path.join("bin", "claude")]
        here = Path(sys.executable).name
        assert os.path.isabs(server.resolve_program([here, "-c"])[0]) if shutil.which(here) else True
        read, write = os.pipe()  # the thread writer used on Windows: a reader keeps up, then one that never reads
        stream = os.fdopen(write, "wb")
        assert server.write_within(stream, b"hello\n", 2) == (b"", None)
        assert os.read(read, 6) == b"hello\n"
        left, stuck = server.write_within(stream, b"x" * (4 << 20), 0.3)
        assert left and stuck is not None and stuck.is_alive()
        os.close(read)  # the reader goes away: the blocked write fails on its thread instead of hanging
        stuck.join(5)
        assert not stuck.is_alive()
        try:
            stream.close()
        except OSError:
            pass
        # A Results or Ruled out line that names an open thread as [id] settles it in place: no duplicate, and a marked
        # thread leaves the brief's work list once it is ruled out
        with tempfile.TemporaryDirectory() as home:
            kept, server.PROJECTS = server.PROJECTS, Path(home)
            try:
                (Path(home) / "s").mkdir()
                (Path(home) / "s" / "project.json").write_text(json.dumps({"goal": "g", "sections": [], "gates": []}))
                note = lambda body, n: server.ingest({"project": "s", "type": "item", "section": "round-notes", "key": f"Round {n}", "agent": "a", "body": body})
                note("Handoff: x\nLeads:\n- first lead\n- second lead\nNext: y", 1)
                lead = {n["text"]: n["id"] for n in server.research("s")}
                for i in lead.values():
                    server.ingest({"project": "s", "type": "research", "id": i, "priority": True, "source": "you"})
                note(f"Handoff: x\nResults:\n- [{lead['first lead']}]: it holds [new]\nRuled out:\n- [{lead['second lead']}]: a guard stops it\nNext: y", 2)
                now = {n["id"]: n for n in server.research("s")}
                assert len(now) == 2, now
                first, second = now[lead["first lead"]], now[lead["second lead"]]
                assert (first["kind"], first["status"], first["text"], first["found_in"], first["known"], first["priority"]) == ("conclusion", "supported", "it holds", "first lead", "new", True), first
                assert (second["status"], second["conditions"], second["text"]) == ("contradicted", "a guard stops it", "second lead"), second
                listed = [line for line in server.brief("s", "b").partition("CLAIMED (another round's word")[2].partition("RULED OUT")[0].splitlines() if line.startswith("- [")]
                assert listed == [f"- [{lead['first lead']}] it holds (the person's priority)"], listed
                # An id in the middle of a line is prose, not a settlement; a marker in the middle of a line is not the Handoff.
                note(f"Handoff: x\nResults:\n- Unlike the idea in [{lead['first lead']}], the decoder leaks\nRuled out:\n- trusting the previous handoff: it was stale\nNext: y", 3)
                after = {n["id"]: n for n in server.research("s")}
                assert after[lead["first lead"]]["text"] == "it holds" and any(n["text"].startswith("Unlike the idea in") for n in after.values()), after
                assert any(n["text"] == "trusting the previous handoff: it was stale" and n["status"] == "contradicted" for n in after.values()), after
                # A heading with no colon is still a label, and a tag survives a line longer than the text limit, in any order.
                note("## Handoff\nlooked around\n**Results**\n- " + "z" * 500 + " [score: 3] [new] [check: long.bin]\nNext\n- carry on", 4)
                long = next(n for n in server.research("s") if n["text"].startswith("zzz"))
                assert (len(long["text"]), long["artifact"], long["known"]) == (400, "long.bin", "new"), long
                before = len(server.load("s"))
                note("no marker in this one", 5)
                assert server.load("s")[-1]["text"] == "A round ended without a Handoff block: nothing was recorded from it" and len(server.load("s")) == before + 2
                assert "- second lead (why: a guard stops it)" in server.brief("s", "b").partition("RULED OUT")[2]
            finally:
                server.PROJECTS = kept
        # Gates: set only by hand, run in order on what a Results line names; a verdict is bound to the gates and the locked files.
        with tempfile.TemporaryDirectory() as home:
            kept, server.PROJECTS = server.PROJECTS, Path(home)
            try:
                (Path(home) / "c").mkdir()
                write = lambda cfg: (Path(home) / "c" / "project.json").write_text(json.dumps(cfg))
                write({"goal": "g", "gates": []})
                assert server.public_config("c")["gates"] == [] and server.public_config("c")["fingerprint"] == "" and server.gates_pending("c") == {"pending": []}
                assert "Nothing re-checks your results" in server.brief("c", "a")
                for bad in ("replay", {"name": "x"}, [{"command": "true"}], [{"name": "a b!", "command": "true"}], [{"name": "x", "command": []}],
                            [{"name": "x", "command": "unclosed 'quote"}], [{"name": "x", "refute": "  "}], [5]):
                    write({"goal": "g", "gates": bad})
                    assert server.public_config("c")["gates"] == [], bad
                (Path(home) / "c" / "scope.md").write_text("in scope: the parser")
                cfg = {"goal": "g", "artifact": "a file holding the input", "lock": ["scope.md", "../outside", "missing.md"],
                       "gates": [{"name": "reproduces", "command": "python3 '{project}/re play.py' --strict", "timeout_min": 99999},
                                 {"name": "reproduces", "command": "true"},  # a second gate of the same name is ignored
                                 {"name": "default config", "command": ["sh", "{project}/default.sh", "{artifact}"], "timeout_min": 2},
                                 {"name": "not known", "refute": "Look for an earlier report."}]}
                write(cfg)
                shown = server.public_config("c")
                assert shown["gates"] == [{"name": "reproduces", "command": ["python3", "{project}/re play.py", "--strict"], "timeout_min": 10},
                                          {"name": "default config", "command": ["sh", "{project}/default.sh", "{artifact}"], "timeout_min": 2},
                                          {"name": "not known", "refute": "Look for an earlier report.", "timeout_min": 10}], shown["gates"]
                seal = shown["fingerprint"]
                assert len(seal) == 16 and shown["artifact"] == "a file holding the input"
                where = str((Path(home) / "c").resolve())
                server.set_project({"project": "c", "gates": [], "lock": [], "goal": "g2"})  # the API cannot touch the gates or the lock
                assert server.public_config("c")["fingerprint"] == seal and server.config("c")["goal"] == "g2"
                text = server.brief("c", "a")
                for expected in ("A RESULT COUNTS ONLY WHEN IT PASSES THE GATES, in this order: reproduces, default config, not known.",
                                 "- reproduces: python3 ", "--strict <artifact>", "- default config: sh ", "default.sh", "{artifact}", "to name what should be checked: a file holding the input. A command gate cannot run without one.",
                                 "- not known: a separate round that sees only your claim and tries to refute it.", "Never edit the gates"):
                    assert expected in text, (expected, text)
                assert "Nothing re-checks" not in text
                note = lambda body, n, **data: server.ingest({"project": "c", "type": "item", "section": "round-notes", "key": f"Round {n}", "agent": "a", "body": body,
                                                              **({"data": data} if data else {})})
                note("Handoff: x\nResults:\n- Lemma one holds [check: one.bin] [new]\n- Lemma two holds [check:  two.bin ]\n- Lemma zero is classical [known: Hardy 1915] [check: zero.bin]\n"
                     "- It looks true for n < 100\nNext: y", 1)
                by = {n["text"]: n for n in server.research("c")}
                one, two, loose = by["Lemma one holds"], by["Lemma two holds"], by["It looks true for n < 100"]
                assert (one["artifact"], one["known"], one["state"], one["at"], one["standing"]) == ("one.bin", "new", "waiting", "reproduces", 0), one
                assert two["artifact"] == "two.bin" and (loose["state"], loose["at"], loose["frame"]) == ("unchecked", "reproduces", False), (two, loose)
                old = by["Lemma zero is classical"]  # the round itself says it is known: ruled out with the reference, and no gate is spent on it
                assert (old["status"], old["conditions"], old["state"], old.get("artifact")) == ("contradicted", "already known: Hardy 1915", None, None), old
                todo = server.gates_pending("c")["pending"]
                assert [(x["id"], x["gate"], x["artifact"], x["timeout_min"]) for x in todo] == [(one["id"], "reproduces", "one.bin", 10), (two["id"], "reproduces", "two.bin", 10)], todo
                assert todo[0]["command"] == ["python3", where + "/re play.py", "--strict"] and "brief" not in todo[0]
                verdict = lambda i, gate, v, **more: server.ingest({"project": "c", "type": "research", "id": i, "source": "gate", "gate": gate, "verdict": v, **more})
                try:  # a verdict from a gate that ran under other gates or locked files is refused: the gate is still due
                    verdict(one["id"], "reproduces", "passed", checked="one.bin", detail="ok", fp="an earlier seal")
                    raise AssertionError("a verdict for an earlier version of the question was accepted")
                except ValueError as error:
                    assert "still due" in str(error)
                assert verdict(one["id"], "reproduces", "passed", checked="one.bin", detail="ok", fp=seal)["fp"] == seal
                assert [x["fp"] for x in todo] == [seal, seal]
                verdict(two["id"], "reproduces", "failed", checked="two.bin", detail="no fault\n  exit 1")
                for bad in (dict(id=one["id"], gate="reproduces", verdict="fine"), dict(id=one["id"], gate="nope", verdict="passed"),
                            dict(id="nope", gate="reproduces", verdict="passed"), dict(gate="reproduces", verdict="passed")):
                    try:
                        server.ingest({"project": "c", "type": "research", "source": "gate", **bad})
                        raise AssertionError(f"verdict {bad} was accepted")
                    except ValueError:
                        pass
                # One gate passed makes the next one due; a claim a gate stopped waits there and becomes work.
                now = {n["id"]: n for n in server.research("c")}
                assert (now[one["id"]]["standing"], now[one["id"]]["state"], now[one["id"]]["at"], now[one["id"]]["agent"]) == (1, "waiting", "default config", "a")
                assert (now[two["id"]]["standing"], now[two["id"]]["state"], now[two["id"]]["at"], now[two["id"]]["frame"]) == (0, "failed", "reproduces", True)
                todo = server.gates_pending("c")["pending"]
                assert [(x["id"], x["gate"]) for x in todo] == [(one["id"], "default config")] and todo[0]["command"] == ["sh", where + "/default.sh", "{artifact}"], todo
                verdict(one["id"], "default config", "passed", checked="some-other.bin")  # a verdict on another artifact is not about this claim
                assert server.gates_pending("c")["pending"][0]["gate"] == "default config"
                verdict(one["id"], "default config", "passed", checked="one.bin")
                todo = server.gates_pending("c")["pending"]
                assert [(x["id"], x["gate"]) for x in todo] == [(one["id"], "not known")] and "command" not in todo[0], todo
                for expected in ("Do not trust it.", f"THE CLAIM [{one['id']}]: Lemma one holds", "in your working folder: one.bin",
                                 "YOUR JOB (not known): Look for an earlier report.", "Verdict: refuted - ", "Verdict: stands - ", "GOAL: g2"):
                    assert expected in todo[0]["brief"], (expected, todo[0]["brief"])
                assert "Hardy" not in todo[0]["brief"] and "Handoff" not in todo[0]["brief"]  # the claim, and nothing about the round
                for expected in ("Verdict: known - ", "Do not read loop.log", "last six lines before your verdict"):
                    assert expected in todo[0]["brief"], expected
                verdict(one["id"], "not known", "passed", checked="one.bin", detail="Verdict: stands - searched")
                now = {n["id"]: n for n in server.research("c")}
                assert (now[one["id"]]["standing"], now[one["id"]]["state"], now[one["id"]]["frame"]) == (3, "established", False) and server.gates_pending("c") == {"pending": []}
                text = server.brief("c", "a")
                settled, refused = text.partition("ESTABLISHED (passed every gate")[2].partition("CLAIMED, BUT STOPPED AT A GATE")[::2]
                assert f"- [{one['id']}] Lemma one holds (artifact: one.bin)" in settled, text
                # The stopped claim is this round's frame: nearest to a result. With gates behind it, the frame is assumed to pay off.
                frame = text.partition("THIS ROUND'S FRAME (round 1 of 3 on it)")[2]
                assert f"- [{two['id']}] conclusion / supported: Lemma two holds" in frame and "It is a claim that a gate stopped." in frame and "Stopped at reproduces (failed) on two.bin:" in frame, text
                assert "no fault" in frame and "rule it out; do not reword it" in frame and "Try hard to make this frame pay off" not in frame
                assert f"a Results line that starts with [{two['id']}] and ends with [check: <artifact>], or" in frame
                assert f"- [{loose['id']}] It looks true for n < 100" in text.partition("CLAIMED, NOT YET THROUGH THE GATES")[2]
                # The person's audit is of this artifact under these gates.
                mark = lambda **k: server.ingest({"project": "c", "type": "research", "id": one["id"], "source": "you", **k})
                assert mark(audited=True)["audit_fp"] == seal
                assert next(n for n in server.research("c") if n["id"] == one["id"])["audited"] is True
                for bad in (dict(audited="yes"), dict(score=6), dict(score=True), dict(score=2.5)):
                    try:
                        mark(**bad)
                        raise AssertionError(f"mark {bad} was accepted")
                    except ValueError:
                        pass
                # The person changes the locked question: every verdict and the audit stop counting until the gates run again.
                (Path(home) / "c" / "scope.md").write_text("in scope: the parser, over the network only")
                assert server.public_config("c")["fingerprint"] != seal
                stale = {n["id"]: n for n in server.research("c")}
                assert (stale[one["id"]]["state"], stale[one["id"]]["at"], stale[one["id"]]["standing"], stale[one["id"]]["audited"]) == ("waiting", "reproduces", 0, False)
                assert stale[two["id"]]["state"] == "waiting" and len(server.gates_pending("c")["pending"]) == 2
                (Path(home) / "c" / "scope.md").write_text("in scope: the parser")
                back = {n["id"]: n for n in server.research("c")}
                assert back[one["id"]]["state"] == "established" and back[one["id"]]["audited"] is True and back[two["id"]]["state"] == "failed"
                # Claimed again by id: every gate runs again on the new artifact, and the audit lapses. Ruled out by id: it leaves the list.
                server.ingest({"project": "c", "type": "activity", "agent": "a", "text": "Iteration 2 started", "data": {"frame": two["id"]}})  # a round is spent when it starts
                note(f"Handoff: x\nResults:\n- [{two['id']}]: repaired the induction step [check: two-b.bin]\n- [{one['id']}] is not waiting, so this is a new claim [check: one.bin]\n"
                     "- It looks true for n < 100 [check: small.bin]\nNext: y", 2, frame=two["id"], cwd="/somewhere/the/round/worked")
                now = {n["id"]: n for n in server.research("c")}
                assert len(now) == 5 and now[two["id"]]["artifact"] == "two-b.bin" and now[two["id"]]["body"] == "repaired the induction step" and now[two["id"]]["text"] == "Lemma two holds", now[two["id"]]
                assert (now[two["id"]]["state"], now[two["id"]]["at"], now[two["id"]]["attempts"], now[two["id"]]["frame"]) == ("waiting", "reproduces", 1, False)
                assert now[one["id"]]["state"] == "established" and now[loose["id"]]["artifact"] == "small.bin"
                assert now[two["id"]]["folder"] == "/somewhere/the/round/worked" and server.gates_pending("c")["pending"][0]["cwd"] == "/somewhere/the/round/worked"
                # A gate that could not run is run again, twice more, before the claim is left there.
                for tries in (1, 2):
                    verdict(two["id"], "reproduces", "error", checked="two-b.bin", detail="timeout after 10 min")
                    again = next(n for n in server.research("c") if n["id"] == two["id"])
                    assert (again["state"], again["gates"]["reproduces"]["tries"]) == ("waiting", tries), again
                verdict(two["id"], "reproduces", "error", checked="two-b.bin", detail="timeout after 10 min")
                assert "Stopped at reproduces (could not run) on two-b.bin" in server.brief("c", "a")
                server.add_frames({"project": "c", "frames": ["a fresh place to look"]})  # an open frame with gates behind it is assumed to pay off
                assert "Try hard to make this frame pay off. Claim only what you ran and saw yourself." in server.brief("c", "a").partition("THIS ROUND'S FRAME")[2]
                # A refuter that cites where a claim was already reported stops it for good: it is not queued for repair.
                fresh = server.ingest({"project": "c", "type": "research", "kind": "conclusion", "status": "supported", "text": "A thing that turns out to be in a 1915 paper", "artifact": "k.bin"})["id"]
                for gate in ("reproduces", "default config"):
                    verdict(fresh, gate, "passed", checked="k.bin")
                verdict(fresh, "not known", "known", checked="k.bin", detail="Verdict: known - Hardy 1915, section 3, states it")
                cited = next(n for n in server.research("c") if n["id"] == fresh)
                assert (cited["state"], cited["at"], cited["standing"], cited["frame"]) == ("failed", "not known", 2, False), cited
                assert "says it is already known" in server.brief("c", "a") and fresh not in [n["id"] for n in server.queue(server.research("c"))]
                assert next(n for n in server.research("c") if n["id"] == two["id"])["frame"] is False  # nothing for a round to repair
                server.ingest({"project": "c", "type": "research", "id": one["id"], "status": "open", "source": "you"})  # the person reopens what was established and audited
                note(f"Handoff: x\nResults:\n- [{one['id']}]: in fact a different thing entirely\nNext: y", 9)
                redone = next(n for n in server.research("c") if n["id"] == one["id"])
                assert (redone["text"], redone["state"], redone["at"], redone["audited"], redone["artifact"]) == ("in fact a different thing entirely", "waiting", "reproduces", False, "one.bin"), redone
                for bad in ("supported", "reproduced", "gone"):  # the person's mark can reopen, drop or rule out; it cannot make a claim
                    try:
                        server.ingest({"project": "c", "type": "research", "id": one["id"], "status": bad, "source": "you"})
                        raise AssertionError(f"status {bad} was accepted as a mark")
                    except ValueError:
                        pass
                note(f"Handoff: x\nResults: none\nRuled out:\n- [{two['id']}]: the induction step is false at n = 4\nNext: y", 3)
                gone = {n["id"]: n for n in server.research("c")}[two["id"]]
                assert gone["status"] == "contradicted" and f"[{two['id']}]" not in server.brief("c", "a").partition("STOPPED AT A GATE")[2].partition("RULED OUT")[0]
                assert "Lemma two holds (why: the induction step is false at n = 4)" in server.brief("c", "a")
            finally:
                server.PROJECTS = kept
        # Frames: what the loop works through. A score is the rounds a frame is worth; a round that leaves its frame open spends one.
        with tempfile.TemporaryDirectory() as home:
            kept, server.PROJECTS = server.PROJECTS, Path(home)
            try:
                (Path(home) / "f").mkdir()
                (Path(home) / "f" / "project.json").write_text(json.dumps({"goal": "g"}))
                made = server.add_frames({"project": "f", "frames": ["src/parse.c: lengths", {"text": "src/net.c: framing", "score": 5, "area": "net", "next": "send a split header"},
                                                                     "SRC/parse.c:  lengths.", {"text": "src/util.c: quoting", "score": 1}]})
                assert len(made["added"]) == 3 and made["skipped"] == 1, made
                parse, net, util = made["added"]
                for bad in ({"frames": []}, {"frames": "x"}, {"frames": [{"score": 2}]}, {"frames": [{"text": "t", "score": 9}]}, {"project": "ff", "frames": ["a typo must not start a project"]}):
                    try:
                        server.add_frames({"project": "f", **bad})
                        raise AssertionError(f"frames {bad} were accepted")
                    except ValueError:
                        pass
                order = lambda: [n["id"] for n in server.queue(server.research("f"))]
                assert order() == [net, parse, util]  # the highest score, then the oldest
                assert server.plan("f", "a")[1] == net and "THIS ROUND'S FRAME (round 1 of 5 on it)" in server.brief("f", "a") and "Next test: send a split header" in server.brief("f", "a")
                def note(body, n, frame=None):
                    if frame:  # the loop says which frame a round was given as the round starts; that is what spends one of its rounds
                        server.ingest({"project": "f", "type": "activity", "agent": "a", "text": f"Iteration {n} started", "data": {"frame": frame}})
                    return server.ingest({"project": "f", "type": "item", "section": "round-notes", "key": f"Round {n}", "agent": "a", "body": body, **({"data": {"frame": frame}} if frame else {})})
                # A round that does not settle its frame spends one of its rounds, and its Next line is where the next one starts.
                note("Handoff: could not build it\nResults: none\nLeads:\n- src/tls.c: renegotiation [score: 4]\n- src/log.c: format strings\nNext: build with the old toolchain", 1, util)
                now = {n["id"]: n for n in server.research("f")}
                assert (now[util]["attempts"], now[util]["budget"], now[util]["stalled"], now[util]["next"]) == (1, 1, True, "build with the old toolchain"), now[util]
                leads = {n["text"]: n for n in server.research("f") if n.get("origin") == "handoff"}
                assert (leads["src/tls.c: renegotiation"]["budget"], leads["src/log.c: format strings"]["budget"]) == (4, 3)
                assert order() == [net, leads["src/tls.c: renegotiation"]["id"], parse, leads["src/log.c: format strings"]["id"]] and util not in order()
                assert f"- [{util}] src/util.c: quoting" in server.brief("f", "a").partition("SET ASIDE (their rounds are spent")[2].partition("THIS ROUND")[0]
                # Pursue puts it at the front with its rounds to spend again; a new score is the person's too.
                server.ingest({"project": "f", "type": "research", "id": util, "priority": True, "source": "you"})
                assert order()[0] == util and server.research("f")[0]["attempts"] == 0
                server.ingest({"project": "f", "type": "research", "id": util, "priority": False, "score": 2, "source": "you"})
                assert next(n for n in server.research("f") if n["id"] == util)["budget"] == 2 and order()[-1] == util
                # Settled by name: it leaves the queue either way.
                note(f"Handoff: done\nResults:\n- [{net}]: a split header desynchronises the framer\nRuled out:\n- [{parse}]: every length is checked against the buffer in read_len\nNext: x", 2, net)
                now = {n["id"]: n for n in server.research("f")}
                assert (now[net]["status"], now[net]["frame"], now[parse]["status"], now[parse]["conditions"]) == ("supported", False, "contradicted", "every length is checked against the buffer in read_len")
                assert net not in order() and parse not in order() and now[net]["attempts"] == 1
                assert (now[net]["state"], now[net]["at"], now[net]["artifact"], now[net]["text"], now[net]["found_in"]) == \
                    ("waiting", "refutation", "", "a split header desynchronises the framer", "src/net.c: framing")  # the built-in refuter needs no artifact
                due = server.gates_pending("f")["pending"]
                assert [(x["id"], x["gate"], x["artifact"]) for x in due] == [(net, "refutation", "")] and "It named no artifact. Work from the claim itself." in due[0]["brief"], due
                assert all(n["state"] is None for n in server.research("f") if n["status"] != "supported")  # only a claim is gated
                server.ingest({"project": "f", "type": "research", "id": net, "source": "gate", "gate": "refutation", "verdict": "passed", "detail": "Verdict: stands - ran it twice"})
                assert next(n for n in server.research("f") if n["id"] == net)["state"] == "established" and "ESTABLISHED (passed the gate" in server.brief("f", "a")
                (Path(home) / "w").mkdir()
                (Path(home) / "w" / "project.json").write_text(json.dumps({"goal": "g"}))
                only = server.add_frames({"project": "w", "frames": [{"text": "the one place", "score": 1}]})["added"][0]
                assert server.plan("w", "a")[1] == only
                server.ingest({"project": "w", "type": "activity", "agent": "a", "text": "Iteration 1 started", "data": {"frame": only}})  # it printed nothing, so there is no note; the round is spent all the same
                text, given = server.plan("w", "a")  # its one round is spent: nobody is asked; the next round widens, and is told what was set aside
                server.ingest({"project": "w", "type": "item", "section": "round-notes", "key": "Round 2", "agent": "a", "body": "Handoff: x\nLeads:\n- a lead a round calls certain [score: 5]\nNext: y"})
                sure = next(n for n in server.research("w") if n["text"] == "a lead a round calls certain")
                assert sure["budget"] == 4  # 5 is the person's to give: a round's own lead tops out at 4
                server.ingest({"project": "w", "type": "research", "id": sure["id"], "status": "superseded", "source": "you"})
                assert given is None and "Follow your nose this round: nothing is queued" in text and f"- [{only}] the one place" in text.partition("SET ASIDE (their rounds are spent")[2], text
            finally:
                server.PROJECTS = kept
        # Tool access is a setting: "wide" adds web and subagents to a Claude recipe's allow-list by name and leaves other programs alone
        wide = server.with_tools(["claude", "-p", "--allowedTools", "Bash", "Read"])
        assert wide[:4] == ["claude", "-p", "--allowedTools", "WebSearch"] and wide[-2:] == ["Bash", "Read"] and "Agent" in wide, wide
        assert server.with_tools(["claude", "-p", "--allowedTools=Bash"]) == ["claude", "-p", "--allowedTools=Bash,WebSearch,WebFetch,Task,Agent"]
        assert server.with_tools(["claude", "-p"])[-5:] == ["--allowedTools", "WebSearch", "WebFetch", "Task", "Agent"]
        assert server.with_tools(["codex", "exec", "-"]) == ["codex", "exec", "-"] and server.with_tools(wide) == wide
        assert server.clean_loop({"tools": "wide"}) == {"tools": "wide"}
        try:
            server.clean_loop({"tools": "everything"})
            raise AssertionError("an unknown tools setting was accepted")
        except ValueError as error:
            assert "loop.tools" in str(error)
        asks = server.handoff_items("Handoff: x\nResults: none\n**Decision needed:** Extend past 100?\nDecisions:\n- Use the slow kernel?\nAssumed: stayed under 100\nAssumptions:\n- none\nNext: y")
        assert asks["decision"] == ["Extend past 100?", "Use the slow kernel?", "stayed under 100"] and not asks["results"], asks
        assert len(server.handoff_items("Results:\n" + "\n".join(f"- r{i} " + "z" * 500 for i in range(12)))["results"]) == 8


if __name__ == "__main__":
    check_units()
    with tempfile.TemporaryDirectory(prefix="Hades research ") as workspace:
        check(workspace)
    print("Direction, priority, terminal messages, the brief and handoff record, decisions, gates, frames, old logs, launchers and model choice, and a restarted loop passed.")
