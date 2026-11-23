"""Exercise the public dashboard command in an empty workspace, without a model."""
import json
import os
import re
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ALIVE = """
import os
import re
def alive(pid):  # os.kill(pid, 0) would end the process on Windows
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32), ctypes.c_void_p
    k.GetExitCodeProcess.argtypes, k.GetExitCodeProcess.restype = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)), ctypes.c_int
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = k.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_uint32()
    try:
        return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k.CloseHandle(handle)
"""


def check_live_round():
    # A stand-in for `claude -p` in stream-json mode: it reads the brief, makes one tool call and gives its result.
    sys.path.insert(0, str(ROOT))
    import argparse, io
    from scripts import loop
    head = """
import json, sys
say = lambda packet: print(json.dumps(packet), flush=True)
text = lambda value: say({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": value}}})
done = lambda value, **more: say({"type": "result", "subtype": "success", "result": value, **more})
"""
    child = head + """
brief = sys.stdin.buffer.read().decode("utf-8")  # the brief arrives as UTF-8 whatever the console's own encoding
say({"type": "system", "subtype": "init", "model": "fixture"})
sys.stdout.buffer.write(b"plain warning \\xff from the CLI\\n" + json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
    "delta": {"type": "text_delta", "text": "Working on: " + brief}}}).encode()[:-4] + b"\\xfe\\"}}}\\n")
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on: " + brief}]}})
say({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}})
text("Checked the résumé parser")
done("Handoff: " + brief, num_turns=2, total_cost_usd=0.25, usage={"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7})
"""
    # A refuter's verdict is the last one it gave; the brief's own template lines, echoed back, are not a verdict.
    said = lambda text: [v.lower() for v in loop.VERDICT.findall(text)]
    assert said("thinking\n**Verdict:** refuted - issue 12\n") == ["refuted"] and said("- Verdict: stands - ran it twice") == ["stands"]
    assert said("Verdict: refuted - <the concrete evidence>\nVerdict: stands - <what you tried and where you looked>\nI could not decide.") == []
    assert loop.RATING.findall("It is small.\n**Rating:** 2 - minor\nRating: 4 - on reflection") == ["2", "4"] and loop.RATING.findall("Rating: <1 to 5> - <the reason>") == []
    # Only a direct `claude -p` that reads its prompt on standard input is shown as it works.
    assert loop.streams(["claude", "-p", "--model", "sonnet"])
    assert not loop.streams(["claude", "-p", "--output-format", "json"])
    assert not loop.streams(["claude", "-p", "{brief}"]) and not loop.streams(["codex", "exec", "-"])

    def stream(source, timeout=30, brief="the brief"):
        screen = io.TextIOWrapper(io.BytesIO(), newline="\n", encoding="utf-8", write_through=True)
        with patch.object(loop, "call", lambda path, body=None: None), patch.object(loop.sys, "stdout", screen):
            result = loop.run_stream([sys.executable, "-u", "-c", source], brief, timeout, None)
        return (*result, screen.buffer.getvalue().decode("utf-8"))

    # The brief goes in as plain text; the transcript shows the session, the CLI's own lines, the text once, and the tool call.
    code, output, stats, shown = stream(child, brief="the résumé brief")
    assert code == 0 and output == "Handoff: the résumé brief", (code, output)
    for expected in ("[claude] Session started · fixture", "plain warning \ufffd from the CLI", "Working on: the résumé brief\ufffd", "> Bash  echo hi",
                     "  | hi", "Checked the résumé parser"):
        assert expected in shown, (expected, shown)
    assert shown.count("Working on: the résumé brief") == 1, shown
    assert stats == {"tools": 1, "turns": 2, "cost_usd": 0.25, "input_tokens": 100, "output_tokens": 7}, stats

    # An error result and a session that ends without any result both count as failures; the time limit ends a round.
    code, output, stats, shown = stream(head + 'sys.stdin.read(); done("API Error: overloaded", is_error=True)')
    assert code == 1 and output == "API Error: overloaded" and "API Error: overloaded" in shown, (code, shown)
    code, output, stats, shown = stream(head + 'sys.stdin.read(); print("Not signed in", flush=True)')
    assert code == "exited without a result" and output.strip() == "Not signed in" and stats == {"tools": 0}, (code, stats)
    code, *_ = stream(head + 'import time; sys.stdin.read(); time.sleep(30)', timeout=1)
    assert code == "timeout after 0.0166667 min", code
    # A time limit of 0 means none: a round outlasts what a short limit would have allowed.
    slow = "import time; time.sleep(1.5); print('Handoff: slow')"
    with patch.object(loop.sys, "stdout", io.TextIOWrapper(io.BytesIO(), newline="\n")):
        assert loop.run_harness([sys.executable, "-c", slow], "b", 0) == (0, "Handoff: slow\n")
    code, output, *_ = stream(head + 'import time; sys.stdin.read(); time.sleep(1.5); done("Handoff: slow")', timeout=0)
    assert (code, output) == (0, "Handoff: slow"), code

    # Ending what a round started, on every platform: POSIX tracks the harness's descendants, Windows puts them in a job.
    exec(ALIVE, globals())
    detach = "**({'creationflags': 0x208} if os.name == 'nt' else {'start_new_session': True})"  # DETACHED | NEW_PROCESS_GROUP

    def gone(pid):  # waits briefly for the process to disappear
        for _ in range(60):
            if not alive(pid):
                return True
            time.sleep(0.1)
        return False
    server = lambda path, body=None: "{}" if path.startswith("/api/project?") else "b"
    signals = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGBREAK") if hasattr(signal, name)}
    runner_code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from scripts import loop\n"
                   "loop.call = lambda path, body=None: '{}' if path.startswith('/api/project?') else 'b'\n"
                   "loop.time.sleep = lambda s: None\n"
                   "sys.argv = ['loop.py', 'p', '--no-notes'] + sys.argv[2:]\nloop.main()")
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # never the loop's to end
    with tempfile.TemporaryDirectory() as work:
        pids, strays, quiet = Path(work) / "pids", [], io.TextIOWrapper(io.BytesIO(), newline="\n")

        def taken():  # the pids the fixtures wrote, collected for cleanup
            found = list(map(int, pids.read_text().split()))
            pids.unlink()
            strays.extend(found)
            return found
        try:
            # Output held open by something the harness left behind does not keep a round waiting, and a time limit ends it all.
            holder = (f"import subprocess, sys, time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=sys.stdout)\n"
                      f"open({str(pids)!r}, 'a').write(f'{{g.pid}} ')\n")
            with patch.object(loop, "LEFTOVER_WAIT", 0.5), patch.object(loop.sys, "stdout", quiet):
                clock = time.time()
                assert loop.run_harness([sys.executable, "-c", holder + "print('Handoff: done')"], "b", 30) == (0, "Handoff: done\n")
                assert gone(taken()[0])
                assert loop.run_harness([sys.executable, "-c", holder + "time.sleep(60)"], "b", 1)[0] == "timeout after 0.0166667 min"
                assert gone(taken()[0]) and time.time() - clock < 15, time.time() - clock
            clock = time.time()
            code, *_ = stream(head + holder + 'sys.stdin.read(); time.sleep(60)', timeout=1)
            assert code == "timeout after 0.0166667 min" and time.time() - clock < 10 and gone(taken()[0]), code

            # --keep-background: what a round started keeps running between rounds, even while holding the output open
            # (round 2 checks round 1's process is alive), the next round starts promptly, and all of it ends with the loop.
            shown = io.TextIOWrapper(io.BytesIO(), newline="\n", encoding="utf-8", write_through=True)
            keeper = ALIVE + (f"import subprocess, sys; p = {str(pids)!r}\n"
                              "for pid in (open(p).read().split() if os.path.exists(p) else []): assert alive(int(pid)), pid\n"
                              "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=sys.stdout)\n"
                              "open(p, 'a').write(f'{g.pid} '); print('Handoff: done')")
            clock = time.time()
            try:
                with patch.object(loop, "call", server), patch.object(loop.time, "sleep", lambda s: None), \
                        patch.object(loop, "LEFTOVER_WAIT", 0.5), patch.object(loop.sys, "stdout", shown), \
                        patch.object(loop.sys, "argv", ["loop.py", "p", "--max-iterations", "2", "--no-notes", "--keep-background",
                                                        "--", sys.executable, "-c", keeper]):
                    loop.main()
            finally:
                for name, handler in signals.items():
                    signal.signal(getattr(signal, name), handler)
            text = shown.buffer.getvalue().decode()
            assert text.count("finished (exit 0)") == 2 and time.time() - clock < 15, text
            assert "[loop] This run: up to 2 rounds, no time limit per round, keeps background processes between rounds\n" in text, text
            kept = taken()
            assert len(kept) == 2 and all(map(gone, kept)) and not loop.KEPT, kept
            for _ in range(50):  # POSIX: the drain thread closes each held pipe once its last holder has gone
                if not loop.LEFT:
                    break
                time.sleep(0.1)
            assert not loop.LEFT, loop.LEFT

            # Claude Code runs each shell command in its own session and process group (on Windows: detached), writing to
            # its own files, so the harness's group never reaches it: the loop must find and end it.
            spawn = (f"import os, subprocess, sys, time; out = open({str(Path(work) / 'out')!r}, 'a')\n"
                     f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=out, stderr=out, {detach})\n"
                     f"open({str(pids)!r}, 'a').write(f'{{g.pid}} ')\n")
            for keep in (False, True):
                with patch.object(loop.sys, "stdout", quiet):  # (a) a normal round end
                    assert loop.run_harness([sys.executable, "-c", spawn + "time.sleep(0.6)"], "b", 30, None, keep)[0] == 0
                pid = taken()[0]
                if keep:
                    assert alive(pid) and len(loop.KEPT) == 1  # kept, and remembered so that the loop's end ends it
                    loop.end_all(loop.KEPT)
                    loop.KEPT.clear()
                assert gone(pid), (keep, pid)
                with patch.object(loop.sys, "stdout", quiet):  # (b) the time limit
                    assert loop.run_harness([sys.executable, "-c", spawn + "time.sleep(60)"], "b", 1, None, keep)[0].startswith("timeout")
                assert gone(taken()[0]) and not loop.KEPT, keep
                # (c) the dashboard's Stop while a round runs: SIGTERM on POSIX; on Windows the loop is killed outright, and its
                # kill-on-close job handles end the rounds. With --keep-background, round 1's kept process ends too.
                harness = spawn + f"if len(open({str(pids)!r}).read().split()) < {1 + keep}: time.sleep(0.6); sys.exit(0)\n" \
                    "print('ready', flush=True); time.sleep(60)"
                runner = subprocess.Popen([sys.executable, "-c", runner_code, str(ROOT), *(["--keep-background"] if keep else []),
                                           "--", sys.executable, "-c", harness], cwd=work, stdout=subprocess.PIPE, text=True)
                while "ready" not in runner.stdout.readline():
                    assert runner.poll() is None, keep
                runner.terminate()
                assert runner.wait(timeout=15) == (143 if os.name == "posix" else 1)
                runner.stdout.close()
                left = taken()
                assert len(left) == 1 + keep and all(map(gone, left)), (keep, left)

            # Ctrl-C while a round runs ends what the round started.
            def interrupt(data):
                raise KeyboardInterrupt
            with patch.object(loop.sys, "stdout", argparse.Namespace(buffer=argparse.Namespace(write=interrupt, flush=lambda: None))):
                try:
                    loop.run_harness([sys.executable, "-c", spawn + "print('started', flush=True); time.sleep(60)"], "b", 30)
                    raise AssertionError("the interrupt did not reach the round")
                except KeyboardInterrupt:
                    pass
            assert gone(taken()[0])
            assert alive(bystander.pid) and bystander.poll() is None
        finally:
            for pid in strays + [bystander.pid]:
                try:
                    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    pass
            bystander.wait()

    # Windows finds claude.cmd and claude.exe shims, still recognises them, runs a .cmd harness with the brief on its
    # standard input, and refuses to put the brief in a .cmd program's arguments.
    try:
        loop.resolve(["claude.cmd", "-p", "{brief}"], windows=True)
        raise AssertionError("a .cmd program was given the brief as an argument")
    except ValueError as error:
        assert "{brief_file}" in str(error)
    assert loop.resolve(["claude.cmd", "{brief_file}"], windows=True)[1] == "{brief_file}"
    if os.name == "nt":
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "echo.py").write_text("import sys; print('Handoff: ' + sys.stdin.read())")
            Path(folder, "claude.cmd").write_text(f'@"{sys.executable}" "{Path(folder, "echo.py")}" %*\r\n')
            with patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ["PATH"]}):
                command = loop.resolve(["claude", "-p"])
            assert command[0].lower().endswith("claude.cmd") and loop.streams(command), command
            with patch.object(loop.sys, "stdout", io.TextIOWrapper(io.BytesIO(), newline="\n")):
                code, output = loop.run_harness(command, "the brief", 30)
            assert code == 0 and output.strip() == "Handoff: the brief", (code, output)
        print("Skipped on Windows: Ctrl-Break and a console's standard input need an interactive console, which CI lacks.")

    # Every round reports its wall-clock time whatever the harness, and the harness receives the handoff instruction.
    events = []

    def server(path, body=None):
        events.append(body)
        return "{}" if path.startswith("/api/project?") else "brief"
    # the brief, then where the round is working, then the handoff instruction, in that order
    echo = ("import os, sys; got = sys.stdin.read(); ok = got.startswith('brief\\n') and got.endswith('\\n\\n' + sys.argv[1]) and 'You are working in ' + os.getcwd() in got\n"
            "print('Handoff: done'); sys.exit(0 if ok else 5)")
    here = os.getcwd()
    with tempfile.TemporaryDirectory() as work:
        os.chdir(work)
        try:
            with patch.object(loop, "call", server), patch.object(loop.time, "sleep", lambda s: None), \
                    patch.object(loop.sys, "stdout", io.TextIOWrapper(io.BytesIO(), newline="\n", encoding="utf-8")), \
                    patch.object(loop.sys, "argv", ["loop.py", "p", "--agent", "a", "--max-iterations", "2", "--no-notes", "--",
                                                    sys.executable, "-c", echo, loop.HANDOFF]):
                loop.main()
        finally:
            os.chdir(here)
            for name, handler in signals.items():
                signal.signal(getattr(signal, name), handler)
    texts = [e.get("text") for e in events if e and e.get("type") == "activity"]
    assert texts == ["Round 1 started", "Round 1 finished (exit 0)", "Round 2 started", "Round 2 finished (exit 0)",
                     "Loop stopped: reached 2 rounds"], texts
    finished = [e for e in events if e and e.get("text") == "Round 1 finished (exit 0)"]
    assert len(finished) == 1 and set(finished[0]["data"]["stats"]) == {"seconds"} and finished[0]["data"]["loop_session"], finished


def check(directory):
    for name in ("server.py", "index.html", "AGENTS.md"):
        shutil.copy(ROOT / name, directory)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def get(route, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(base + route, body, {"Content-Type": "application/json"}), timeout=3) as response:
            return json.load(response)

    message = "Review complete — résumé 日本語"
    for first in (True, False):
        proc = subprocess.Popen([sys.executable, "server.py", "--dashboard"], cwd=directory,
                                env={**os.environ, "PROJECT_VIEW_PORT": str(port)})
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Dashboard exited during startup")
                try:
                    projects = get("/api/projects")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Dashboard did not become ready")
            assert [p["id"] for p in projects] == ["workspace"]
            with urlopen(base + "/", timeout=3) as page:
                html = page.read().decode()
            # The page is a viewer: it reads these, and writes only a launch, a stop, the loop settings and the audited mark.
            assert 'id="view"' in html
            for read in ("/api/projects", "/api/events?project=", "/api/research?project=", "/api/terminal?", "/api/run-details?"):
                assert read in html, read
            wrote = set(re.findall(r'"(/api/[a-z/-]+)"', html)) - {"/api/projects"}
            assert wrote == {"/api/launch", "/api/stop", "/api/project", "/api/events"}, wrote
            for gone in ("/api/terminal/send", "/api/frames", "/api/new", "reply_wait", "priority:", "direction:", "paused:", "answer:", "Iteration "):
                assert gone not in html, gone
            with urlopen(base + "/AGENTS.md", timeout=3) as guide:
                assert b"/api/status" in guide.read()
            # A page on another site cannot write here: not with its Origin, not as a plain form post, not under another name.
            for headers in ({"Origin": "https://other.example", "Content-Type": "application/json"}, {"Content-Type": "text/plain"}, {"Content-Type": "application/json", "Host": "evil.example"}):
                try:
                    urlopen(Request(base + "/api/project", json.dumps({"project": "workspace", "direction": "do something else"}).encode(), headers), timeout=3)
                    raise AssertionError(f"a write was accepted with {headers}")
                except HTTPError as error:
                    assert error.code == 403, (headers, error.code)
            try:
                urlopen(Request(base + "/api/projects", headers={"Host": "evil.example"}), timeout=3)
                raise AssertionError("a read was answered under another site's name")
            except HTTPError as error:
                assert error.code == 403
            assert get("/api/project?project=workspace")["direction"] == ""
            try:  # the terminal is one-way: nothing can be sent to a round
                get("/api/terminal/send", {"project": "workspace", "agent": "fixture", "session": "sample", "text": "Hello"})
                raise AssertionError("a message to a round was accepted")
            except HTTPError as error:
                assert error.code == 404, error.code
            route = "/api/run-details?project=workspace&agent=fixture&session=sample&round="
            prompt = "\nReview Ω\r\n$HOME 'literal' <tag>\n"
            if first:
                env = {**os.environ, "HADES_DETAILS_DB": str(Path(directory) / "agent-work/.hades/invocations.sqlite3"),
                       "HADES_DETAILS_PROJECT": "workspace", "HADES_DETAILS_AGENT": "fixture",
                       "HADES_LAUNCH_SESSION": "sample", "HADES_DETAILS_ROUND": "1"}
                command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
                forwarded = subprocess.run([sys.executable, str(ROOT / "scripts/loop.py"), "--capture-cli", *command],
                                           env=env, input=prompt.encode(), stdout=subprocess.PIPE, check=True)
                assert forwarded.stdout == prompt.encode()
                assert get(route + "1")[0]["argv"] == command
                assert get(route + "2") == []
                count = get("/api/projects")[0]["count"]
                # What the page draws comes from the server: a queued frame, then a claim, each with its group and its place.
                frame = get("/api/frames", {"project": "workspace", "frames": ["Look at the résumé parser"]})["added"][0]
                claim = get("/api/events", {"project": "workspace", "type": "research", "status": "supported", "agent": "fixture",
                                            "text": "The parser drops the last line"})["id"]
                threads = {n["id"]: n for n in get("/api/research?project=workspace")}
                assert {"bucket", "place", "working", "state", "at", "standing", "attempts", "budget", "audited", "history"} <= set(threads[frame]), sorted(threads[frame])
                assert (threads[frame]["bucket"], threads[frame]["place"], threads[frame]["working"]) == ("queued", 1, None), threads[frame]
                assert threads[claim]["bucket"] == "waiting" and threads[claim]["at"] and threads[claim]["place"] is None, threads[claim]
                # The audited mark, the page's one write to the record: set, then removed.
                for on in (True, False):
                    get("/api/events", {"project": "workspace", "type": "research", "id": claim, "audited": on, "source": "you"})
                    assert next(n for n in get("/api/research?project=workspace") if n["id"] == claim)["audited"] is on
                # The settings dialog sends only these keys; the server keeps them, and refuses one it does not know.
                loop = {"rounds": 3, "timeout_min": 20, "workers": 2, "spend": 12, "keep_background": True, "model": "sonnet"}
                get("/api/project", {"project": "workspace", "loop": loop})
                assert get("/api/projects")[0]["config"]["loop"] == loop
                for bad in ({"spend": 10001}, {"workers": 5}, {"rounds": "many"}):
                    try:
                        get("/api/project", {"project": "workspace", "loop": bad})
                        raise AssertionError(f"loop settings {bad} were accepted")
                    except HTTPError as error:
                        assert error.code == 400, (bad, error.code)
                for old in ({"reply_wait": 5}, {"tools": "wide"}):  # settings that no longer exist are never kept
                    try:
                        get("/api/project", {"project": "workspace", "loop": {**loop, **old}})
                    except HTTPError as error:
                        assert error.code == 400, (old, error.code)
                    assert get("/api/projects")[0]["config"]["loop"] == loop, old
                get("/api/project", {"project": "workspace", "loop": {}})
                # A round's note is what the Live page and the Overview's latest handoff read.
                count = get("/api/projects")[0]["count"]
                get("/api/events", {"project": "workspace", "type": "note", "agent": "fixture", "key": "Round 1 · fixture · now",
                                    "body": "Handoff: " + message, "data": {"loop_session": "sample"}})
                assert get("/api/projects")[0]["count"] == count + 1
                assert get(f"/api/events?project=workspace&after={count}")["events"][0]["body"] == "Handoff: " + message
            else:
                notes = [e for e in get("/api/events?project=workspace")["events"] if e["type"] == "note"]
                assert notes[-1]["body"] == "Handoff: " + message and notes[-1]["data"]["loop_session"] == "sample"
                assert get("/api/projects")[0]["config"]["loop"] == {}
            assert get(route + "1")[0]["stdin"] == prompt
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    check_live_round()
    with tempfile.TemporaryDirectory(prefix="Hades dashboard ") as workspace:
        check(workspace)
    print("Dashboard startup, live updates and restart passed.")
