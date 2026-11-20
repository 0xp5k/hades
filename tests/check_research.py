"""Exercise the person's standing direction, research priority and terminal messages through the HTTP API, without a model.

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
    for project, cfg in (("plain", {"goal": "Find the parser bug", "sections": []}),
                         ("lean", {"simple": True, "target": "parser.c", "goal": "Find the parser bug", "rules": "Stay local.",
                                   "protected": ["score.py", "checks/*.py"]}),
                         ("blank", {"simple": True, "goal": ""}),
                         ("fan", {"simple": True, "goal": "Map the parser"}),
                         ("checked", {"simple": True, "goal": "Push the bound", "metric": {"verify": "python3 score.py", "artifact": "the witness as JSON"}})):
        (work / "projects" / project).mkdir(parents=True)
        (work / "projects" / project / "project.json").write_text(json.dumps(cfg))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def call(route, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(base + route, body, {"Content-Type": "application/json", **(headers or {})}), timeout=10) as response:
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
        # A. The standing direction: set, shown first in both briefs, removed with an empty string.
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

        # B. Priority: the person's partial update marks a thread without touching anything else; agents cannot change it.
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
            text = brief(project)
            head = text.index("MARKED AS PRIORITY BY THE PERSON")
            assert head < text.index("[len]") < text.index("[new]") < text.index("[old]"), text
            assert "Footer overflows" not in text[head:text.index("[old]")]
        ev = lambda **k: call("/api/events", {"project": "plain", "type": "research", **k})
        ev(id="len", priority=False, source="you")
        assert call("/api/research?project=plain")[0]["priority"] is False
        assert "MARKED AS PRIORITY" not in brief("plain")
        ev(id="len", priority=True, source="you")

        # 1. The lean brief asks for the work and the handoff only; submitting to the check is the one call it keeps.
        lean = brief("lean")
        for expected in ("RULED OUT (do not retry without a new reason", "- Footer overflows (why: footer is bounds-checked)",
                         "Next test: Send a 70000-byte header", "One piece of work this round", "Never edit the checker",
                         "say so in the handoff", "recorded on the dashboard for you", "every result you report is a claim",
                         "watched for changes; do not edit them: score.py, checks/*.py", "TARGET: parser.c"):
            assert expected in lean, (expected, lean)
        for absent in ("POST", "curl", "Current decision", "question event", "Read the saved"):
            assert absent not in lean, (absent, lean)
        assert len(lean.splitlines()) < 45, len(lean.splitlines())
        checked = brief("checked")
        for expected in ("THE INDEPENDENT CHECK", "-X POST", '"type": "run"', "The artifact: the witness as JSON", '"run r12"', "/api/runs?project=checked&agent=a1"):
            assert expected in checked, (expected, checked)
        assert "watched for changes" not in checked

        # A lean project with no goal yet asks for one instead of inventing work; the full brief no longer asks for diagrams.
        assert "GOAL: none yet. Do not invent work: ask the person for a goal in your handoff" in brief("blank")
        call("/api/project", {"project": "blank", "direction": "Look at the tokenizer."})
        assert "GOAL: none yet. Work from the person's direction above." in brief("blank") and "Do not invent" not in brief("blank")
        assert "GOAL: none yet" not in lean and "AGENTS.md" not in lean
        assert "diagram" not in brief("plain").lower()
        call("/api/events", {"project": "plain", "type": "topic", "key": "Old page", "body": "x", "diagram": {"nodes": [{"id": "a", "label": "A"}], "edges": []}})

        # 5. The checker's files come through to the loop and survive every API write; they are set only by hand.
        assert call("/api/project?project=lean")["protected"] == ["score.py", "checks/*.py"]
        assert call("/api/project?project=plain")["protected"] == []
        call("/api/project", {"project": "lean", "goal": "Find the parser bug", "protected": ["nothing"]})
        assert json.loads((work / "projects/lean/project.json").read_text())["protected"] == ["score.py", "checks/*.py"]

        # 3. The brief style is the person's switch.
        assert config("plain")["simple"] is False and config("lean")["simple"] is True
        assert call("/api/project", {"project": "plain", "simple": True})["simple"] is True
        assert "recorded on the dashboard for you" in brief("plain")
        call("/api/project", {"project": "plain", "simple": False})
        assert "HOW YOU WORK" in brief("plain")
        refused("/api/project", {"project": "plain", "simple": "yes"})

        # 2. The handoff is recorded for a lean round: the current update, a thread per result and per ruled-out approach.
        rid = call("/api/events", {"project": "lean", "type": "run", "agent": "a1", "text": "empty input"})["id"]
        before = len(call("/api/research?project=lean"))
        def note(project, body, agent="a1"):
            call("/api/events", {"project": project, "type": "item", "section": "round-notes", "agent": agent, "key": f"Round · {time.time()}", "body": body})
        note("lean", "Earlier Handoff: a draft.\nMore work.\n\n**Handoff:** Checked the header path. Only you can say whether fuzzing is in scope.\n"
                     "**Results:**\n- Off-by-one in footer length [known: CVE-2020-1]\n1. Parser crashes on empty input, see run " + rid + " [new]\n"
                     "- **Header length is trusted.**\n* parser crashes on empty  input, see run " + rid + "\nRuled out: none\nNext: try the footer")
        threads = call("/api/research?project=lean")
        made = [t for t in threads if t.get("origin") == "handoff"]
        assert len(threads) == before + 2 and len(made) == 2, made
        by = {t["text"]: t for t in made}
        off = by["Off-by-one in footer length"]
        assert (off["kind"], off["status"], off["known"], off["agent"]) == ("conclusion", "supported", "CVE-2020-1", "a1") and "run" not in off
        crash = by["Parser crashes on empty input, see run " + rid]
        assert crash["known"] == "new" and crash["run"] == rid
        now = [e for e in call("/api/events?project=lean")["events"] if e["type"] == "item" and e.get("section") == "now"][-1]
        assert now["key"] == "Current update" and now["body"].startswith("**Handoff:** Checked") and now["agent"] == "a1"
        note("lean", "handoff: second round\nResult: nothing\nRULED OUT:\n* Brute force over 2^32: too slow\n  on this laptop\n"
                     "2) Fuzzing the footer: footer is bounds-checked\nNext: x")
        ruled = [t for t in call("/api/research?project=lean") if t.get("origin") == "handoff" and t["status"] == "contradicted"]
        assert sorted(t["text"] for t in ruled) == ["Brute force over 2^32: too slow on this laptop", "Fuzzing the footer: footer is bounds-checked"], ruled
        assert all(t["kind"] == "observation" for t in ruled)
        # Leads, in any style: open hypotheses, without repeating a thread that exists
        note("lean", "Handoff: wandered\nResults: none\nLeads:\n- Tokens over 64k wrap: send 65537 bytes\n- Header length is trusted\nLead: the footer\nNext: x")
        leads = [t for t in call("/api/research?project=lean") if t.get("origin") == "handoff" and t["kind"] == "hypothesis"]
        assert sorted(t["text"] for t in leads) == ["Tokens over 64k wrap: send 65537 bytes", "the footer"] and all(t["status"] == "open" for t in leads), leads
        count = len(call("/api/events?project=lean")["events"])
        note("lean", "No marker here.\nResults:\n- something")
        assert len(call("/api/events?project=lean")["events"]) == count + 1  # only the note itself
        note("plain", "Handoff: done\nResults:\n- a claim")
        assert not [t for t in call("/api/research?project=plain") if t.get("origin")]
        lean = brief("lean", "a2")
        assert "- Brute force over 2^32: too slow on this laptop" in lean and "Parser crashes on empty input" in lean
        assert "WHERE THINGS STAND" in lean and "Handoff: wandered" in lean
        # The person's Drop and Reopen: a status change alone keeps everything else.
        call("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "contradicted", "source": "you"})
        dropped = next(t for t in call("/api/research?project=lean") if t["id"] == crash["id"])
        assert dropped["status"] == "contradicted" and dropped["text"] == crash["text"] and dropped["run"] == rid and dropped["agent"] == "a1"
        assert "- Parser crashes on empty input" in brief("lean", "a2").split("RULED OUT (do not")[1]
        call("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "open", "source": "you"})
        assert next(t for t in call("/api/research?project=lean") if t["id"] == crash["id"])["status"] == "open"
        refused("/api/events", {"project": "lean", "type": "research", "id": crash["id"], "status": "gone", "source": "you"})

        # C. A message typed in the terminal reaches the live worker and every later round's brief.
        started = call("/api/launch", {"project": "lean", "recipe": "fake", "count": 1})["started"]
        assert started == ["fake-1"], started
        worker = next(x for x in call("/api/projects") if x["id"] == "lean")["launched"][0]
        assert worker["interactive"] and worker["alive"]
        page = {"Origin": base, "X-Hades-Terminal": "1"}
        said = "Ignore the footer from now on — it is out of scope."
        message = {"project": "lean", "agent": "fake-1", "session": worker["session"], "text": said}
        refused("/api/terminal/send", message, 403, {"Origin": "https://other.example", "X-Hades-Terminal": "1"})
        refused("/api/terminal/send", {**message, "session": "stale"}, 400, page)
        assert call("/api/terminal/send", message, page) == {"sent": True}
        call("/api/terminal/send", {**message, "text": "Second note " + "y" * 3000}, page)
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

        # A restart forgets launched workers, but their loops keep going: a new launch must not reuse a live loop's name,
        # and stopping must still reach it.
        orphan = int((work / "agent-work/lean/fake-1/loop.pid").read_text())
        server.terminate()
        server.wait(timeout=10)
        server = start()
        assert alive(orphan)
        assert call("/api/launch", {"project": "lean", "recipe": "fake", "count": 1})["started"] == ["fake-2"]
        assert sorted(call("/api/stop", {"project": "lean"})["stopped"]) == ["fake-1", "fake-2"]
        for _ in range(50):
            if not alive(orphan):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the loop from before the restart was not stopped")

        # 4. Launchers as the page offers them, and the model chosen for one launch.
        listing = next(x for x in call("/api/projects") if x["id"] == "lean")
        offered = listing["launchers"]
        assert sorted(offered) == ["bare", "fake", "wrap"] and listing["recipes"]["wrap"] == "Wrapper"
        assert offered["fake"] == {"label": "fake", "model": "", "models": ["fable", "opus", "sonnet", "haiku"], "interactive": True,
                                   "command": [str(claude), "-p"], "takes_tools": True, "rounds": 200, "timeout_min": 1, "reply_wait": 120, "keep_background": False, "style": "focused", "tools": "recipe"}, offered["fake"]
        assert offered["wrap"]["model"] == "base-model" and offered["wrap"]["models"] == ["base-model", "other"] and not offered["wrap"]["interactive"]
        assert offered["bare"]["models"] == [] and "broken" in listing["launcherError"]
        out = call("/api/launch", {"project": "lean", "recipe": "fake", "model": "fable"})
        assert out["model"] == "fable"
        assert next(x for x in call("/api/projects") if x["id"] == "lean")["launched"][-1]["model"] == "fable"
        assert call("/api/launch", {"project": "lean", "recipe": "wrap", "model": "other"})["model"] == "other"
        assert call("/api/launch", {"project": "lean", "recipe": "wrap"})["model"] == "base-model"
        refused("/api/launch", {"project": "lean", "recipe": "bare", "model": "opus"})
        refused("/api/launch", {"project": "lean", "recipe": "fake", "model": "bad model; rm"})
        (work / "launchers.json").write_text("{")
        listing = next(x for x in call("/api/projects") if x["id"] == "lean")
        assert listing["launchers"] == {} and "could not be read" in listing["launcherError"]
        (work / "launchers.json").write_text(json.dumps(recipes))
        assert next(x for x in call("/api/projects") if x["id"] == "lean")["launcherError"].startswith("Skipped")

        # 1 (round 3). Loop settings: saved once per project, validated, and resolved request > project > recipe > default.
        assert config("lean")["loop"] == {}
        assert offered["fake"]["rounds"] == 200 and offered["fake"]["timeout_min"] == 1 and offered["fake"]["reply_wait"] == 120
        assert offered["bare"]["rounds"] == 4 and offered["bare"]["timeout_min"] == 0 and offered["bare"]["keep_background"] is False
        saved = call("/api/project", {"project": "lean", "loop": {"recipe": "wrap", "model": "other", "rounds": 7, "timeout_min": 0,
                                                                   "reply_wait": 0, "keep_background": True, "junk": 1, "label": None}})["loop"]
        assert saved == {"recipe": "wrap", "model": "other", "rounds": 7, "timeout_min": 0, "reply_wait": 0, "keep_background": True}, saved
        for bad, key in (({"rounds": -1}, "loop.rounds"), ({"style": "wild"}, "loop.style"), ({"agents": 6}, "loop.agents"), ({"timeout_min": 1441}, "loop.timeout_min"), ({"reply_wait": 1.5}, "loop.reply_wait"),
                         ({"keep_background": "yes"}, "loop.keep_background"), ({"model": "a b"}, "loop.model"), ("fast", "loop must")):
            try:
                call("/api/project", {"project": "lean", "loop": bad})
                raise AssertionError(f"loop {bad} was accepted")
            except HTTPError as error:
                assert error.code == 400 and key in json.loads(error.read())["error"], bad
        assert config("lean")["loop"] == saved
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
            out = call("/api/launch", {"project": "lean"})  # everything from the project's saved settings
            assert out["recipe"] == "wrap" and out["settings"] == {"rounds": 7, "timeout_min": 0, "reply_wait": 0, "keep_background": True, "style": "focused", "tools": "recipe", "model": "other"}, out
            argv = argv_of("lean", out["started"][0])
            assert (flag(argv, "--max-iterations"), flag(argv, "--timeout-min"), flag(argv, "--reply-wait")) == ("7", "0", "0") and "--keep-background" in argv
            assert flag(argv, "-m") == "other"
            entry = next(x for x in next(p for p in call("/api/projects") if p["id"] == "lean")["launched"] if x["agent"] == out["started"][0])
            assert entry["settings"] == out["settings"]
            out = call("/api/launch", {"project": "lean", "recipe": "fake", "rounds": 3, "keep_background": False})  # the request wins
            assert out["settings"] == {"rounds": 3, "timeout_min": 0, "reply_wait": 0, "keep_background": False, "style": "focused", "tools": "recipe", "model": "other"}, out
            argv = argv_of("lean", out["started"][0])
            assert flag(argv, "--max-iterations") == "3" and "--keep-background" not in argv and flag(argv, "--model") == "other"
            refused("/api/launch", {"project": "lean", "recipe": "fake", "timeout_min": -1})
            call("/api/project", {"project": "lean", "loop": {}})  # cleared: the recipe's own fields, then the defaults
            assert config("lean")["loop"] == {}
            refused("/api/launch", {"project": "lean"})  # three recipes and none chosen
            out = call("/api/launch", {"project": "lean", "recipe": "fake"})
            assert out["settings"] == {"rounds": 200, "timeout_min": 1, "reply_wait": 120, "keep_background": False, "style": "focused", "tools": "recipe", "model": ""}, out
            assert flag(argv_of("lean", out["started"][0]), "--timeout-min") == "1"
            (work / "launchers.json").write_text(json.dumps({"bare": recipes["bare"]}))  # one recipe: it is used without being named
            out = call("/api/launch", {"project": "lean"})
            assert out["recipe"] == "bare" and out["settings"]["rounds"] == 4 and out["settings"]["timeout_min"] == 0
            argv = argv_of("lean", out["started"][0])
            assert flag(argv, "--reply-wait") == "120" and flag(argv, "--timeout-min") == "0"  # research has no time limit by default
            call("/api/project", {"project": "lean", "loop": {"rounds": 0}})  # rounds 0: until stopped
            out = call("/api/launch", {"project": "lean"})
            assert out["settings"]["rounds"] == 0 and flag(argv_of("lean", out["started"][0]), "--max-iterations") == "0"
            call("/api/stop", {"project": "lean"})
            call("/api/project", {"project": "lean", "loop": {}})

            # Round styles and fan-out. Threads to start from, newest first: f3, f2, f1.
            for i in (1, 2, 3):
                call("/api/events", {"project": "fan", "type": "research", "id": f"f{i}", "kind": "hypothesis", "status": "open", "text": f"Lead {i}"})
            focused = brief("fan", "solo")
            assert "One piece of work this round" in focused and "Follow your nose" not in focused and "WORKERS ARE ON THIS PROJECT" not in focused
            call("/api/project", {"project": "fan", "loop": {"style": "explore", "agents": 3, "recipe": "bare"}})
            explore = brief("fan", "solo")  # started by hand: the project's style
            for expected in ("Follow your nose this round", "Breadth beats proof", "in parallel", "Leads: line", "dead ground"):
                assert expected in explore, (expected, explore)
            assert "One piece of work this round" not in explore and "Never edit the checker" in explore
            out = call("/api/launch", {"project": "fan"})
            assert out["started"] == ["bare-1", "bare-2", "bare-3"] and out["settings"]["style"] == "explore", out
            lines = []
            for n, agent in enumerate(out["started"], 1):
                text = brief("fan", agent)
                assert f"3 WORKERS ARE ON THIS PROJECT AT ONCE; YOU ARE NUMBER {n} (bare-1, bare-2, bare-3)." in text, text
                lines.append(next(l for l in text.splitlines() if l.startswith(("Start from", "Pick an angle"))))
            assert lines[0].startswith("Start from lead [f3]") and lines[1].startswith("Start from lead [f2]") and lines[2].startswith("Start from lead [f1]"), lines
            assert len(set(lines)) == 3
            one = call("/api/launch", {"project": "fan", "count": 1, "style": "focused"})  # the request wins, and the worker keeps its own style
            text = brief("fan", one["started"][0])
            assert "One piece of work this round" in text and "YOU ARE NUMBER 4" in text and "Pick an angle the others are unlikely to take" in text, text
            assert "Follow your nose" in brief("fan", "bare-1")
            call("/api/stop", {"project": "fan"})
        finally:
            (work / "scripts/loop.py").write_text(real)
            (work / "launchers.json").write_text(json.dumps(recipes))
    finally:
        try:
            call("/api/stop", {"project": "lean"})
        except OSError:
            pass
        server.terminate()
        server.wait(timeout=10)


def check_trust():
    """The person-only settings refuse callers from other machines, checked in process against a scratch folder."""
    sys.path.insert(0, str(ROOT))
    import server
    with tempfile.TemporaryDirectory() as folder:
        server.PROJECTS = Path(folder)
        server.set_project({"project": "p", "goal": "g"})
        for attempt in (lambda: server.set_project({"project": "p", "direction": "go"}, "10.0.0.2"),
                        lambda: server.ingest({"project": "p", "type": "research", "id": "k1", "priority": True, "source": "you"}, "10.0.0.2")):
            try:
                attempt()
                raise AssertionError("a remote caller acted as the person")
            except PermissionError:
                pass
        server.ingest({"project": "p", "type": "research", "kind": "hypothesis", "status": "open", "text": "t"}, "10.0.0.2")
        try:  # a remote full update may not carry the person's mark either
            server.ingest({"project": "p", "type": "research", "id": "k1", "kind": "hypothesis", "status": "open", "text": "t",
                           "priority": True, "source": "you"}, "10.0.0.2")
            raise AssertionError("a remote caller set priority")
        except PermissionError:
            pass
        assert server.research("p")[0]["priority"] is False
        try:
            server.set_project({"project": "p", "simple": True}, "10.0.0.2")
            raise AssertionError("a remote caller changed the brief style")
        except PermissionError:
            pass
        try:
            server.set_project({"project": "p", "loop": {"rounds": 2}}, "10.0.0.2")
            raise AssertionError("a remote caller changed the loop settings")
        except PermissionError:
            pass
        first = server.starter()
        assert first["goal"] == "" and first["simple"] is True and first["loop"] == {}
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
        assert items == {"results": ["one two continues", "three"], "ruled out": [], "decision": [], "leads": []}, items
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
        # thread leaves the brief's work list once it is ruled out or its run is confirmed
        with tempfile.TemporaryDirectory() as home:
            kept, server.PROJECTS = server.PROJECTS, Path(home)
            try:
                (Path(home) / "s").mkdir()
                (Path(home) / "s" / "project.json").write_text(json.dumps({"goal": "g", "simple": True, "sections": []}))
                note = lambda body, n: server.ingest({"project": "s", "type": "item", "section": "round-notes", "key": f"Round {n}", "agent": "a", "body": body})
                note("Handoff: x\nLeads:\n- first lead\n- second lead\nNext: y", 1)
                lead = {n["text"]: n["id"] for n in server.research("s")}
                for i in lead.values():
                    server.ingest({"project": "s", "type": "research", "id": i, "priority": True, "source": "you"})
                run = server.ingest({"project": "s", "type": "run", "text": "r", "agent": "a", "artifact": {}})["id"]
                note(f"Handoff: x\nResults:\n- [{lead['first lead']}] run {run}: it holds [new]\nRuled out:\n- [{lead['second lead']}]: a guard stops it\nNext: y", 2)
                now = {n["id"]: n for n in server.research("s")}
                assert len(now) == 2, now
                first, second = now[lead["first lead"]], now[lead["second lead"]]
                assert (first["kind"], first["status"], first["run"], first["known"], first["priority"]) == ("conclusion", "supported", run, "new", True), first
                assert (second["status"], second["conditions"], second["text"]) == ("contradicted", "a guard stops it", "second lead"), second
                listed = lambda: [line for line in server.brief("s", "b", "http://x").partition("RESEARCH THREADS")[2].partition("RULED OUT")[0].splitlines()
                                  if line.startswith("- [")]
                assert listed() == [f"- [{lead['first lead']}] conclusion / supported: first lead"], listed()
                server.ingest({"project": "s", "type": "verdict", "run": run, "ok": True, "score": 1, "text": "ok"}, "127.0.0.1", True)
                assert listed() == [] and "MARKED AS PRIORITY" not in server.brief("s", "b", "http://x")
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
        asks = server.handoff_items("Handoff: x\nResults: none\n**Decision needed:** Extend past 100?\nDecisions:\n- Use the slow kernel?\nNext: y")
        assert asks["decision"] == ["Extend past 100?", "Use the slow kernel?"] and not asks["results"], asks
        assert len(server.handoff_items("Results:\n" + "\n".join(f"- r{i} " + "z" * 500 for i in range(12)))["results"]) == 8


if __name__ == "__main__":
    check_trust()
    with tempfile.TemporaryDirectory(prefix="Hades research ") as workspace:
        check(workspace)
    print("Direction, priority, terminal messages, the lean brief and handoff record, brief style, checker files, launchers and model choice, and restarted workers passed.")
