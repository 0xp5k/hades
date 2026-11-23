"""Exercise the public dashboard command in an empty workspace, without a model."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ALIVE = """
import os
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
    # A stand-in for `claude -p` in stream-json mode: one tool call, then it waits for the person's message.
    sys.path.insert(0, str(ROOT))
    import argparse, io, queue
    from scripts import loop
    head = """
import json, sys
say = lambda packet: print(json.dumps(packet), flush=True)
text = lambda value: say({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": value}}})
done = lambda value, **more: say({"type": "result", "subtype": "success", "result": value, **more})
"""
    child = head + """
brief = json.loads(sys.stdin.readline())["message"]["content"]
say({"type": "system", "subtype": "init", "model": "fixture"})
sys.stdout.buffer.write(b"plain warning \\xff from the CLI\\n" + json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
    "delta": {"type": "text_delta", "text": "Working on: " + brief}}}).encode()[:-4] + b"\\xfe\\"}}}\\n")
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on: " + brief}]}})
say({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]}})
steer = json.loads(sys.stdin.readline())["message"]["content"]
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}})
text("Heard: " + steer)
done("Handoff: " + steer, num_turns=2, total_cost_usd=0.25, usage={"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7})
reply = json.loads(sys.stdin.readline())["message"]["content"]
done("Second turn: " + reply, num_turns=3, total_cost_usd=0.5, usage={"input_tokens": 5, "output_tokens": 3})
sys.exit(0 if sys.stdin.read() == "" else 3)
"""
    # A refuter's verdict is the last one it gave; the brief's own template lines, echoed back, are not a verdict.
    said = lambda text: [v.lower() for v in loop.VERDICT.findall(text)]
    assert said("thinking\n**Verdict:** refuted - issue 12\n") == ["refuted"] and said("- Verdict: stands - ran it twice") == ["stands"]
    assert said("Verdict: refuted - <the concrete evidence>\nVerdict: stands - <what you tried and where you looked>\nI could not decide.") == []
    assert loop.can_converse(["claude", "-p", "--model", "sonnet"])
    assert not loop.can_converse(["claude", "-p", "--output-format", "json"])
    assert not loop.can_converse(["claude", "-p", "{brief}"]) and not loop.can_converse(["codex", "exec", "-"])

    def converse(source, timeout=30, messages=(), reply_wait=5):
        inbox = queue.Queue()
        inbox.live = False
        for m in messages:
            inbox.put(m)
        screen = io.TextIOWrapper(io.BytesIO(), newline="\n", encoding="utf-8", write_through=True)
        with patch.object(loop, "call", lambda path, body=None: None), patch.object(loop, "EXIT_WAIT", 1), \
                patch.object(loop.sys, "stdout", screen):
            result = loop.run_conversation([sys.executable, "-u", "-c", source], "the brief", timeout, None, inbox, reply_wait)
        return (*result, screen.buffer.getvalue().decode("utf-8"), inbox)

    sent, line = [], loop.Screen.line

    def reply(self, text):  # the person answers once invited to, after the first turn has finished
        line(self, text)
        if "Reply within" in text and not sent:
            sent.append(text)
            threading.Timer(0.4, inbox.put, ["And then?"]).start()
    inbox = queue.Queue()
    inbox.live = False
    inbox.put("Check the résumé parser")
    screen = io.TextIOWrapper(io.BytesIO(), newline="\n", encoding="utf-8", write_through=True)
    with patch.object(loop.Screen, "line", reply), patch.object(loop.sys, "stdout", screen):
        code, output, stats = loop.run_conversation([sys.executable, "-u", "-c", child], "the brief", 30, None, inbox, 5)
    shown = screen.buffer.getvalue().decode("utf-8")
    assert code == 0 and output == "Handoff: Check the résumé parser\n\nSecond turn: And then?", (code, output)
    for expected in ("[claude] Session started · fixture", "plain warning � from the CLI", "Working on: the brief�", "> Bash  echo hi",
                     "  | hi", "You: Check the résumé parser", "Heard: Check the résumé parser", "Reply within 5 s", "You: And then?"):
        assert expected in shown, (expected, shown)
    assert shown.count("Working on: the brief") == 1 and len(sent) == 1 and inbox.empty()
    # Cost and turns are cumulative for the session; token usage is per turn.
    assert stats == {"tools": 1, "messages": 2, "turns": 3, "cost_usd": 0.5, "input_tokens": 105, "output_tokens": 10}, stats

    # The time limit passing while a finished turn waits for a reply ends the round cleanly, not as a timeout.
    waits = head + """
json.loads(sys.stdin.readline()); json.loads(sys.stdin.readline())
done("Handoff: answered")
sys.exit(0 if sys.stdin.read() == "" else 3)
"""
    code, output, stats, shown, _ = converse(waits, timeout=1.5, messages=["Hello"])
    assert code == 0 and output == "Handoff: answered" and stats["messages"] == 1 and "turns" not in stats, (code, shown)
    # An error result, a session that ends without any result, and one that ignores the end of its input all count as failures.
    code, output, stats, shown, _ = converse(head + 'sys.stdin.readline(); done("API Error: overloaded", is_error=True)')
    assert code == 1 and output == "API Error: overloaded" and "API Error: overloaded" in shown, (code, shown)
    code, output, stats, shown, _ = converse(head + 'sys.stdin.readline(); print("Not signed in", flush=True)')
    assert code == "exited without a result" and output.strip() == "Not signed in" and stats == {"tools": 0, "messages": 0}, code
    code, *_ = converse(head + 'import time; sys.stdin.readline(); done("Handoff: x"); sys.stdin.read(); time.sleep(30)')
    assert code == "did not exit after its session ended", code
    code, *_ = converse(head + 'import time; sys.stdin.readline(); time.sleep(30)', timeout=1)
    assert code == "timeout after 0.0166667 min", code

    # The listener keeps a final line typed without a newline, and reads JSON lines from the dashboard.
    heard = subprocess.run([sys.executable, "-c", "import queue, sys, json; sys.path.insert(0, sys.argv[1]); from scripts import loop\n"
                            "q = queue.Queue(); q.live = True; loop.listen(q); print(json.dumps(list(q.queue)))", str(ROOT)],
                           input='{"text": "two\\nlines"}\nplain\r\nlast'.encode(), stdout=subprocess.PIPE, check=True)
    assert json.loads(heard.stdout) == ["two\nlines", "plain", "last"], heard.stdout

    # A reply wait of 0 ends the round as soon as Claude finishes, even after the person spoke, and says nothing about replying.
    clock = time.time()
    code, output, stats, shown, _ = converse(waits, messages=["Hello"], reply_wait=0)
    assert code == 0 and stats["messages"] == 1 and "Reply within" not in shown and time.time() - clock < 4, (code, shown)
    # A time limit of 0 means none: a round outlasts what a short limit would have allowed.
    slow = "import time; time.sleep(1.5); print('Handoff: slow')"
    with patch.object(loop.sys, "stdout", io.TextIOWrapper(io.BytesIO(), newline="\n")):
        assert loop.run_harness([sys.executable, "-c", slow], "b", 0) == (0, "Handoff: slow\n")
    code, output, *_ = converse(head + 'import time; sys.stdin.readline(); time.sleep(1.5); done("Handoff: slow"); '
                                'sys.exit(0 if sys.stdin.read() == "" else 3)', timeout=0)
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
            code, *_ = converse(head + holder + 'sys.stdin.readline(); time.sleep(60)', timeout=1)
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
            assert command[0].lower().endswith("claude.cmd") and loop.can_converse(command), command
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
    assert texts == ["Iteration 1 started", "Iteration 1 finished (exit 0)", "Iteration 2 started", "Iteration 2 finished (exit 0)",
                     "Loop stopped: reached 2 iterations"], texts
    finished = [e for e in events if e and e.get("text") == "Iteration 1 finished (exit 0)"]
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
        with urlopen(base + route, body, timeout=3) as response:
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
                assert b'id="view"' in page.read()
            with urlopen(base + "/AGENTS.md", timeout=3) as guide:
                assert b"topic" in guide.read()
            hello = json.dumps({"project": "workspace", "agent": "fixture", "session": "sample", "text": "Hello"}).encode()
            for origin, code in (("https://other.example", 403), (base, 400)):  # another site is refused; no such worker here
                request = Request(base + "/api/terminal/send", hello, {"Origin": origin, "X-Hades-Terminal": "1",
                                  "Content-Type": "application/json"})
                try:
                    urlopen(request, timeout=3)
                    raise AssertionError("A terminal message without a live worker succeeded")
                except HTTPError as error:
                    assert error.code == code, (origin, error.code)
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
                get("/api/events", {"project": "workspace", "type": "item", "section": "now",
                                    "key": "Current update", "body": message})
                assert get("/api/projects")[0]["count"] == count + 1
                assert get(f"/api/events?project=workspace&after={count}")["events"][0]["body"] == message
            else:
                assert get("/api/events?project=workspace")["events"][-1]["body"] == message
            assert get(route + "1")[0]["stdin"] == prompt
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    check_live_round()
    with tempfile.TemporaryDirectory(prefix="Hades dashboard ") as workspace:
        check(workspace)
    print("Dashboard startup, live updates and restart passed.")
