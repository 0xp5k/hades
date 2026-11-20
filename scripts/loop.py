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

With --interactive and a direct "claude -p" command, a round is a live conversation. The terminal shows Claude's
text, tool calls and tool results as they happen, and each line typed on this program's standard input is sent to
Claude as a message from the person. Claude receives it at its next step, in the same session.

Everything after "--" is your harness command, exactly as you would type it. The harness needs to be
allowed to make HTTP requests to the server and to do the project's work without asking; how you grant
that is specific to the harness.

Environment: PROJECT_VIEW_URL (default http://127.0.0.1:8765), PROJECT_VIEW_TOKEN (from another machine).
"""
import argparse
import codecs
import hashlib
import socket
import shlex
import shutil
import signal
import sqlite3
import json
import locale
import os
import queue
import select
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePath

URL = os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765").rstrip("/")


def call(path, body=None):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("PROJECT_VIEW_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PROJECT_VIEW_TOKEN"]
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(URL + path, data, headers), timeout=15) as r:
        return r.read().decode()


def say(a, text, show=True, **extra):
    if show:
        print(f"[loop] {text}", flush=True)
    data = {"loop_session": a.session, **{k: v for k, v in extra.items() if v}}
    try:
        call("/api/events", {"project": a.project, "type": "activity", "text": text, "agent": a.agent, "harness": a.harness, "model": a.model, "data": data})
    except OSError:
        pass  # the server being briefly away must not stop the loop


def cli_name(program):
    name = Path(program).name.lower()
    return name.split(".")[0] if name in ("codex", "claude", "codex.exe", "claude.exe", "codex.cmd", "claude.cmd") else ""


def resolve(command, windows=os.name == "nt"):
    """On Windows, find the program the way a shell would (claude.cmd, claude.exe). A .cmd or .bat program runs through
    cmd.exe, which cannot pass arbitrary text safely, so the brief must never be placed in its arguments."""
    if not windows:
        return command
    command = [shutil.which(command[0]) or command[0], *command[1:]]
    if command[0].lower().endswith((".cmd", ".bat")) and any("{brief}" in part for part in command[1:]):
        raise ValueError(f"{Path(command[0]).name} runs through cmd.exe, which cannot pass the brief safely as an argument; "
                         "use {brief_file} (or nothing: the brief is also sent on standard input)")
    return command


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
    if os.name == "nt":  # CreateProcess finds only .exe by itself; claude and codex are usually .cmd shims
        argv = [shutil.which(argv[0], path=env.get("PATH")) or argv[0], *argv[1:]]
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


LEFTOVER_WAIT = 5  # seconds the output may stay open after the harness itself has exited


GRACE = 3  # seconds a process gets to finish its files after SIGTERM, before SIGKILL
KEPT = []  # with --keep-background, the families of earlier rounds; they end when the loop does


if os.name == "nt":  # job objects: the Windows way to end a process and everything it started
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER), ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION; IO_COUNTERS is six ULONGLONGs
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for _name, _args, _result in (("CreateJobObjectW", (wintypes.LPVOID, wintypes.LPCWSTR), wintypes.HANDLE),
                                  ("SetInformationJobObject", (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD), wintypes.BOOL),
                                  ("AssignProcessToJobObject", (wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
                                  ("TerminateJobObject", (wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
                                  ("CloseHandle", (wintypes.HANDLE,), wintypes.BOOL)):
        getattr(_k32, _name).argtypes, getattr(_k32, _name).restype = _args, _result


def job_for(proc):
    """A kill-on-close job holding the harness and everything it starts, so even a loop killed outright takes them along.
    None if Windows refuses (an old version, or a job that forbids nesting)."""
    job, limits = _k32.CreateJobObjectW(None, None), _Limits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job and _k32.SetInformationJobObject(job, 9, ctypes.addressof(limits), ctypes.sizeof(limits)) \
            and _k32.AssignProcessToJobObject(job, int(proc._handle)):  # 9: JobObjectExtendedLimitInformation
        return job
    error = ctypes.get_last_error()
    if job:
        _k32.CloseHandle(job)
    if not JOBLESS:
        JOBLESS.append(error)
        print(f"[loop] Windows refused a job object (error {error}); rounds end with taskkill instead, which misses "
              "processes whose parent has already exited", flush=True)
    return None


JOBLESS = []


def start(args, env):
    # On POSIX the harness gets its own process group, so the person's Ctrl-C reaches only the loop, which ends the round.
    return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, start_new_session=os.name == "posix")


def processes():
    """{pid: (parent pid, process group, start time)} for every live process on POSIX; {} elsewhere or when it cannot be read."""
    table = {}
    if os.name != "posix":
        return table
    if os.path.isdir("/proc/self"):  # Linux: no child process needed
        for name in os.listdir("/proc"):
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    fields = f.read().rsplit(b")", 1)[1].split()
                if fields[0] != b"Z":
                    table[int(name)] = (int(fields[1]), int(fields[2]), fields[19].decode())
            except (OSError, IndexError, ValueError):
                pass
        return table
    try:
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for row in rows.splitlines():
        parts = row.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit() and not parts[3].startswith("Z"):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[4])
    return table


class Family:
    """Every process a harness starts, learned while it runs. Claude Code gives each shell command its own process group,
    so killing the harness's group misses them, and once orphaned they can no longer be traced to the harness."""

    def __init__(self, proc):
        self.proc, self.known, self.seen = proc, {}, 0.0  # known: pid -> start time, which guards against pid reuse
        self.live, self.lock = True, threading.Lock()  # live: during its round, the harness's group names its own processes
        self.job = job_for(proc) if os.name == "nt" else None  # Windows: 0 once ended and closed

    def scan(self):
        with self.lock:  # the round's timer thread and the reader both scan
            return self._scan()

    def _scan(self):
        table, self.seen = processes(), time.time()
        children = {}
        for pid, (parent, _, _) in table.items():
            children.setdefault(parent, []).append(pid)
        alive = [pid for pid, began in self.known.items() if table.get(pid, (0, 0, None))[2] == began]
        # Anything left in the harness's own group counts too, so a child it started just before exiting is not missed.
        alive += [pid for pid, (_, group, _) in table.items() if self.live and group == self.proc.pid and pid not in alive]
        todo = alive + ([self.proc.pid] if self.proc.poll() is None else [])
        while todo:
            for pid in children.get(todo.pop(), []):
                if pid not in alive:
                    alive.append(pid)
                    todo.append(pid)
        self.known = {pid: table[pid][2] for pid in alive if pid != self.proc.pid}
        return alive

    def watch(self):  # from the round's timer thread: a process table every couple of seconds costs next to nothing
        if os.name == "posix" and time.time() - self.seen >= 2:
            self.scan()

    def end(self):
        end_all([self])


def end_all(families):
    """End each harness and everything it started: SIGTERM, then SIGKILL to whatever is left after GRACE. (Windows: the harness.)"""
    if os.name != "posix":  # Windows has no SIGTERM to give: the job ends at once
        for f in families:
            with f.lock:  # two threads may end the same round; a handle must be closed once
                if f.job:
                    _k32.TerminateJobObject(f.job, 1)
                    _k32.CloseHandle(f.job)
                    f.job = 0
                elif f.job is None and f.proc.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(f.proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for f in families:
            for kill, target in [(os.kill, pid) for pid in f.scan()] + [(os.killpg, f.proc.pid)] * f.live:
                try:
                    kill(target, sig)
                except OSError:
                    pass
        stop = time.time() + (GRACE if sig == signal.SIGTERM else 0)
        while time.time() < stop and any(f.proc.poll() is None or f.scan() for f in families):
            threading.Event().wait(0.2)


def finish_round(family, keep, abnormal):
    if keep and not abnormal:  # left running, but remembered so that stopping the loop still ends them
        found = family.scan()
        family.live = False  # from now on only the processes it found are its own: the group's number may be reused
        KEPT[:] = [f for f in KEPT if f.job or f.scan()] + [family] * bool(found or family.job)  # a Windows job is kept whole
    else:
        family.end()


def limit(timeout):
    return time.time() + timeout if timeout else float("inf")  # 0 means no time limit


LEFT = []  # output pipes still held by processes that earlier rounds left behind


def drain():  # one thread for the whole run reads and discards them, so those processes' writes never block or fail
    while True:
        for pipe in select.select(list(LEFT), [], [], 1)[0]:
            if not os.read(pipe.fileno(), 65536):  # its last writer has exited
                LEFT.remove(pipe)
                pipe.close()


def read_output(proc):
    """Yield the harness's output as it arrives. If the harness has exited but something it started still holds the output
    after LEFTOVER_WAIT, stop reading so the round can end; the round's end decides whether that process lives."""
    since, dropped = 0.0, threading.Event()
    if os.name == "posix":
        fd = proc.stdout.fileno()
        take = lambda: os.read(fd, 65536) if select.select([fd], [], [], 0.2)[0] else None  # None: nothing yet
    else:  # Windows pipes have no select(): a thread reads into a queue, and once abandoned discards until the output closes
        chunks = queue.Queue()

        def pump():
            try:
                while chunk := proc.stdout.read1(65536):
                    if not dropped.is_set():
                        chunks.put(chunk)
            except (OSError, ValueError):
                pass
            chunks.put(b"")

        def take():
            try:
                return chunks.get(timeout=0.2)
            except queue.Empty:
                return None
        threading.Thread(target=pump, daemon=True).start()
    while (chunk := take()) != b"":
        if chunk:
            yield chunk
        elif proc.poll() is not None:
            since = since or time.time()
            if time.time() - since > LEFTOVER_WAIT:
                dropped.set()
                if os.name == "posix":
                    LEFT.append(proc.stdout)
                    if not any(t.name == "hades-drain" for t in threading.enumerate()):
                        threading.Thread(target=drain, name="hades-drain", daemon=True).start()
                return


def lines(chunks):
    rest = b""
    for chunk in chunks:
        *whole, rest = (rest + chunk).split(b"\n")
        yield from whole
    if rest:
        yield rest


def run_harness(args, brief, timeout, env=None, keep=False):
    """Run one round. Shows the harness's output as it arrives and also returns it. Returns (exit code or reason, output)."""
    proc = start(args, env)
    family = Family(proc)
    if env is not None:
        record_input(env, "cli" if cli_name(args[0]) else "wrapper", args, brief.encode("utf-8"))
    timed_out, done = threading.Event(), threading.Event()

    def guard(deadline=limit(timeout)):
        while not done.wait(0.2):
            family.watch()
            if time.time() > deadline:  # the person's time limit ends everything, kept processes included
                timed_out.set()
                return family.end()
    threading.Thread(target=guard, daemon=True).start()

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
    try:
        for chunk in read_output(proc):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            tail = (tail + decoder.decode(chunk))[-6000:]
    except BaseException:  # Ctrl-C or the dashboard's Stop: the harness's own group does not hear it, so end it here
        family.end()
        raise
    finally:
        done.set()
    tail = (tail + decoder.decode(b"", final=True))[-6000:]
    code = proc.wait()
    finish_round(family, keep, timed_out.is_set())
    return (f"timeout after {timeout / 60:g} min" if timed_out.is_set() else code), tail


STREAM = ["--input-format", "stream-json", "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
EXIT_WAIT = 60  # seconds Claude gets to exit once its input has ended
HANDOFF = """When you finish, end your final response with this block. It is saved as your only memory of this round and recorded on the dashboard for you, so you do not need to report anything else.
Handoff: <what you did and what you observed, in a few sentences>
Results: <one line per result you are claiming, or "none". End a line with [known: reference] if it is already known, or [new] if you looked and found no prior statement>
Ruled out: <one line per approach this round showed does not work, with the reason, or "none">
Decision needed: <anything only the person can decide, as a question they can answer in a line, or "none">
Next: <the single most useful next step>"""


def can_converse(command):
    """A direct `claude -p` that takes its prompt on standard input can hold a two-way session."""
    rest = command[1:]
    return (bool(command) and cli_name(command[0]) == "claude" and any(x in ("-p", "--print") for x in rest)
            and not any(x.split("=")[0] in ("--input-format", "--output-format") for x in rest)
            and not any("{brief" in x for x in command))


def heard(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # a Windows console types in its own code page
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def listen(inbox):
    """Queue each line of standard input as a message from the person. The dashboard sends JSON lines."""
    pending, chunk = b"", b"\n"
    while chunk:  # at the end of input (the server went away, or Ctrl-D) keep what was typed without a newline
        try:  # read the descriptor itself: a thread waiting inside sys.stdin's buffer breaks interpreter shutdown
            chunk = os.read(0, 65536)
        except OSError:
            chunk = b""
        *lines, pending = (pending + chunk).split(b"\n") if chunk else (pending, b"")
        for raw in lines:
            text = heard(raw).strip()
            if text.startswith("{"):
                try:
                    packet = json.loads(text)
                    text = packet["text"].strip() if isinstance(packet, dict) and isinstance(packet.get("text"), str) else text
                except ValueError:
                    pass
            if text:
                inbox.put(text)
                if not inbox.live:
                    print("[loop] Message received. Claude gets it when the next round starts.", flush=True)


class Screen:
    """Print a Claude Code event stream as a readable terminal transcript."""
    FIELDS = ("command", "file_path", "path", "pattern", "url", "query", "prompt", "description")

    def __init__(self):
        self.fresh, self.streamed, self.tail, self.lock = True, False, "", threading.RLock()
        self.started, self.calls, self.last = False, {}, None

    def write(self, text):
        with self.lock:  # Claude's stream and the person's messages reach the screen from different threads
            if text:
                sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
                self.fresh = text.endswith("\n")
                self.tail = (self.tail + text)[-6000:]

    def line(self, text):
        with self.lock:
            self.write(("" if self.fresh else "\n") + text + "\n")

    def call(self, block, indent):
        given = block.get("input") if isinstance(block.get("input"), dict) else {}
        shown = next((str(given[k]) for k in self.FIELDS if given.get(k)), "") or (json.dumps(given, ensure_ascii=False) if given else "")
        shown = shown[:800] + (" ..." if len(shown) > 800 else "")
        self.calls[block.get("id")], self.last = f"{block.get('name', 'tool')}  {shown.splitlines()[0][:70] if shown else ''}".strip(), block.get("id")
        self.line(f"\n{indent}> {block.get('name', 'tool')}  " + shown.replace("\n", "\n" + indent + "    "))

    def outcome(self, block, indent):
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", "")) if isinstance(part, dict) and part.get("type") == "text"
                                else f"({part.get('type', 'data') if isinstance(part, dict) else 'data'})" for part in content)
        rows = str(content or "(no output)").strip().splitlines() or ["(no output)"]
        more = len(rows) - 12
        rows = [row[:300] + (" ..." if len(row) > 300 else "") for row in rows[:12]] + ([f"... {more} more lines"] if more > 0 else [])
        if block.get("is_error"):
            rows[0] = "error: " + rows[0]
        asked = self.calls.pop(block.get("tool_use_id"), "")
        if asked and block.get("tool_use_id") != self.last:  # calls made together report back in their own order
            rows.insert(0, f"(from {asked})")
        self.last = None
        self.line("\n".join(f"{indent}  | {row}" for row in rows))

    def show(self, packet):
        kind, nested = packet.get("type"), bool(packet.get("parent_tool_use_id"))
        indent = "    " if nested else ""
        if kind == "system" and packet.get("subtype") == "init" and not self.started:  # it repeats on every turn
            self.started = True
            self.line(f"[claude] Session started · {packet.get('model', 'model not reported')}")
        elif kind == "stream_event" and not nested:
            event = packet.get("event") or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_start" and (event.get("content_block") or {}).get("type") == "text":
                self.line("")
            elif delta.get("type") == "text_delta" and delta.get("text"):
                self.write(delta["text"])
                self.streamed = True
        elif kind in ("assistant", "user"):
            content = (packet.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    self.call(block, indent)
                elif block.get("type") == "tool_result":
                    self.outcome(block, indent)
                elif block.get("type") == "text" and kind == "assistant":
                    if nested or not self.streamed:  # already on screen when it arrived as deltas
                        self.line("\n" + indent + str(block.get("text", "")).replace("\n", "\n" + indent))
                    self.streamed = False


def run_conversation(args, brief, timeout, env, inbox, reply_wait=120, keep=False):
    """Run one round as a live two-way session. Returns (exit code or reason, Claude's finished replies, statistics).
    Once the person has spoken in the round, a finished turn stays open `reply_wait` seconds for their reply."""
    proc = start(args, env)
    family, screen, lock, done = Family(proc), Screen(), threading.Lock(), threading.Event()
    state = {"open": True, "idle": 0.0, "spoke": False, "deadline": limit(timeout), "reason": None, "invited": False}
    stats, used = {"tools": 0, "messages": 0}, set()

    def close():  # once a finished turn's input ends, Claude exits 0; give it a short while to do so
        with lock:
            state.update(open=False, deadline=min(state["deadline"], time.time() + EXIT_WAIT))
            try:
                proc.stdin.close()
            except OSError:
                pass

    def send(text, source=None):
        payload = (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode("utf-8")
        with lock:
            if not state["open"]:
                return False
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except OSError:
                return False
        if env is not None:
            record_input(env, "cli", args, payload, *([source] if source else []))
        return True

    def attend():  # one thread delivers the person's messages, keeps time and ends the session when the talk is over
        while not done.wait(0.2):
            family.watch()
            now = time.time()
            if now > state["deadline"] and not (state["open"] and state["idle"]):  # busy, or not exiting after its input ended
                state["reason"] = f"timeout after {timeout / 60:g} min" if state["open"] else "did not exit after its session ended"
                return family.end()
            if not state["open"]:
                continue
            try:
                text = inbox.get_nowait()
            except queue.Empty:
                text = None
            if text is not None:
                if not send(text, "Message from the person during the round"):
                    inbox.put(text)  # this session has closed: it opens the next round
                    close()
                    continue
                state.update(idle=0.0, spoke=True, invited=False, deadline=limit(timeout))
                stats["messages"] += 1
                screen.line("\nYou: " + text.replace("\n", "\n     ") + "\n")
            elif state["idle"]:
                wait = reply_wait if state["spoke"] else 0
                if wait and not state["invited"]:
                    state["invited"] = True
                    screen.line(f"\n[loop] Claude finished this turn. Reply within {wait:g} s to continue this conversation.")
                # A finished turn waiting only for a reply ends cleanly at the time limit, rather than as a timeout.
                if (now - state["idle"] >= wait or now > state["deadline"]) and inbox.empty():
                    inbox.live = False  # from here on, a message waits for the next round, and the listener says so
                    close()

    inbox.live = True
    send(brief)
    threading.Thread(target=attend, daemon=True).start()
    replies, results, failed = [], 0, False
    try:
        for raw in lines(read_output(proc)):
            try:  # a stray invalid byte must not cost a whole packet
                packet = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                packet = None
            if not isinstance(packet, dict):  # the CLI's own plain messages, such as a sign-in error
                screen.line(raw.decode("utf-8", errors="replace").rstrip("\r"))
                continue
            kind = packet.get("type")
            if kind == "result":
                results += 1
                replies.append(str(packet.get("result") or ""))
                failed = bool(packet.get("is_error"))
                if failed and packet.get("result"):
                    screen.line(str(packet["result"]))
                # Cost and turns count the whole session so far, so the last value stands; usage is per turn, so it adds up.
                for field, key in (("total_cost_usd", "cost_usd"), ("num_turns", "turns")):
                    if isinstance(packet.get(field), (int, float)) and not isinstance(packet.get(field), bool):
                        stats[key] = packet[field]
                usage = packet.get("usage") if isinstance(packet.get("usage"), dict) else {}
                for key, fields in (("input_tokens", ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                                    ("output_tokens", ("output_tokens",))):
                    counts = [usage[f] for f in fields if isinstance(usage.get(f), int) and not isinstance(usage.get(f), bool)]
                    if counts:
                        stats[key] = stats.get(key, 0) + sum(counts)
                state["idle"] = time.time()
            else:
                if kind in ("assistant", "stream_event"):
                    state.update(idle=0.0, invited=False)  # a message that arrived as the turn ended has started another
                if kind == "assistant":
                    content = (packet.get("message") or {}).get("content")
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") not in used:
                            used.add(block.get("id"))
                            stats["tools"] += 1
                screen.show(packet)
    except BaseException:  # Ctrl-C or the dashboard's Stop: the harness's own group does not hear it, so end it here
        family.end()
        raise
    finally:
        inbox.live = False
        done.set()
        with lock:  # no message can be written into a session that has ended
            state["open"] = False
            try:
                proc.stdin.close()
            except OSError:
                pass
    code = proc.wait()
    finish_round(family, keep, bool(state["reason"]))
    screen.line("")
    if "cost_usd" in stats:
        stats["cost_usd"] = round(stats["cost_usd"], 6)
    outcome = state["reason"] or code or (1 if failed else 0 if results else "exited without a result")
    return outcome, "\n\n".join(r for r in replies if r) or screen.tail, stats


def fingerprint(patterns, limit=2000, large=20 << 20):
    """Hash the checker's files, named by glob patterns inside the working folder, so a round that edits them is noticed."""
    root, seen = Path.cwd().resolve(), {}
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip() or ".." in pattern or PurePath(pattern).anchor:
            continue  # nothing outside the working folder
        if pattern.endswith("**"):  # before Python 3.13 a trailing ** matches only folders
            pattern += "/*"
        try:
            for path in root.glob(pattern):
                name = path.relative_to(root).as_posix()
                if name in seen or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                if len(seen) >= limit:
                    return seen
                try:
                    info = path.stat()
                    if info.st_size > large:
                        seen[name] = f"{info.st_size}:{info.st_mtime_ns}"
                        continue
                    digest = hashlib.sha256()
                    with open(path, "rb") as f:
                        while block := f.read(1 << 20):
                            digest.update(block)
                    seen[name] = digest.hexdigest()
                except OSError:
                    seen[name] = "unreadable"
        except (OSError, ValueError, NotImplementedError):
            continue
    return seen


def main():
    try:
        run_loop()
    finally:  # however the loop ends, the processes kept between rounds end with it
        end_all(KEPT)
        KEPT.clear()


def run_loop():
    p = argparse.ArgumentParser(description="Run a harness against a Hades project, nonstop.",
                                usage="loop.py PROJECT [options] -- HARNESS COMMAND ...")
    p.add_argument("project")
    p.add_argument("--agent", default=os.environ.get("PROJECT_VIEW_AGENT") or socket.gethostname(), help="this agent's name on the page")
    p.add_argument("--harness", help="label shown on the Agents page (default: the command's name)")
    p.add_argument("--model", help="model name shown on the Agents page (default: taken from a --model in the harness command)")
    p.add_argument("--max-iterations", type=int, default=0, help="stop after this many (default: no limit)")
    p.add_argument("--max-hours", type=float, default=0, help="stop after this long (default: no limit)")
    p.add_argument("--timeout-min", type=float, default=0, help="end one iteration after this many minutes (default 0: no limit)")
    p.add_argument("--pause", type=float, default=5, help="seconds between iterations (default 5)")
    p.add_argument("--no-notes", action="store_true", help="do not save each round's output to the project's Round notes page")
    p.add_argument("--reply-wait", type=float, default=120, help="with --interactive: seconds a finished turn stays open for the person's reply once they have spoken in the round; 0 ends the round when Claude finishes (default 120)")
    p.add_argument("--keep-background", action="store_true", help="leave processes the agent started running between rounds; they still end when a round hits its time limit or the loop stops")
    p.add_argument("--interactive", action="store_true", help="with a direct `claude -p` command: show Claude's work as it happens and send each line of standard input to it as a message")
    # Split at the first "--" ourselves, so the harness command can contain any flags it likes.
    argv = sys.argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    a, command = p.parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        p.error("give the harness command after --, for example:  -- claude -p")
    if not sys.stdout.isatty():  # the dashboard reads this log as UTF-8, whatever the platform's own encoding
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a.session = os.environ.get("HADES_LAUNCH_SESSION") or uuid.uuid4().hex
    a.harness = a.harness or os.path.basename(command[0])
    if not a.model and "--model" in command[:-1]:  # most harnesses take the model this way; report it so the page can show it
        a.model = command[command.index("--model") + 1]
    try:
        command = resolve(command)
    except ValueError as error:
        p.error(str(error))
    inbox = None
    if a.interactive and can_converse(command):
        inbox = queue.Queue()
        inbox.live = False
        threading.Thread(target=listen, args=(inbox,), daemon=True).start()
    elif a.interactive:
        print("[loop] --interactive needs a direct `claude -p` command that reads its prompt on standard input; running rounds without it", flush=True)
    # The dashboard's Stop sends SIGTERM on POSIX; Ctrl-Break is its Windows console cousin. Either way the finally clauses
    # then end the round. (Killed outright on Windows, the loop's job handles close and take the round with them.)
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: sys.exit(143))
    print("[loop] This run: " + ", ".join(filter(None, [
        f"up to {a.max_iterations} rounds" if a.max_iterations else "rounds until stopped",
        f"for at most {a.max_hours:g} hours" if a.max_hours else "",
        f"{a.timeout_min:g} min limit per round" if a.timeout_min else "no time limit per round",
        (f"waits {a.reply_wait:g} s for your reply" if a.reply_wait else "reply wait off") if inbox else "",
        "keeps background processes between rounds" if a.keep_background else "stops background processes when a round ends",
        f"model {a.model}" if a.model else ""])), flush=True)
    query = urllib.parse.urlencode({"project": a.project, "agent": a.agent})
    started, n, failures = time.time(), 0, 0
    if not a.no_notes:
        try:
            call("/api/project", {"project": a.project, "section": {"id": "round-notes", "title": "Round notes", "view": "note"}})
        except OSError:
            pass

    def finish(text):  # the listener promised these to a next round that will not come
        left = []
        while inbox and not inbox.empty():
            left.append(inbox.get_nowait())
        if left:
            say(a, f"{len(left)} message(s) from the person arrived after the last round and were not delivered: " + " | ".join(left)[:1000])
        return say(a, text)

    while True:
        if os.path.exists("STOP"):
            return finish("Loop stopped: found a STOP file")
        if a.max_iterations and n >= a.max_iterations:
            return finish(f"Loop stopped: reached {a.max_iterations} iterations")
        if a.max_hours and time.time() - started > a.max_hours * 3600:
            return finish(f"Loop stopped: reached {a.max_hours:g} hours")
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
        brief += "\n\n" + HANDOFF
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="brief-", delete=False) as f:
            f.write(brief)
        say(a, f"Iteration {n} started")
        guarded = state.get("protected") if isinstance(state.get("protected"), list) else []
        before = fingerprint(guarded) if guarded else None
        began, stats = time.time(), {}
        try:
            argv = [part.replace("{brief_file}", f.name).replace("{brief}", brief) for part in command]
            if inbox:
                argv = argv[:1] + STREAM + argv[1:]
            with tempfile.TemporaryDirectory(prefix="hades-launch-") as directory:
                env = launch_env(a, n, argv, directory)
                if inbox:
                    code, output, stats = run_conversation(argv, brief, a.timeout_min * 60, env, inbox, a.reply_wait, a.keep_background)
                else:
                    code, output = run_harness(argv, brief, a.timeout_min * 60, env, a.keep_background)
        except OSError as e:
            return finish(f"Loop stopped: could not start the harness ({e})")
        finally:
            os.unlink(f.name)
        stats = {"seconds": round(time.time() - began, 1), **stats}
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
        if before is not None:
            after = fingerprint(guarded)
            changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            if changed:
                more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
                say(a, f"Checker changed during round {n}: " + ", ".join(changed[:5]) + more, tamper=changed[:20])
        if code == 0:
            say(a, f"Iteration {n} finished (exit 0)", stats=stats)
            failures = 0
            time.sleep(a.pause)
        else:  # back off, so a harness that fails instantly does not spin
            failures += 1
            wait = min(300, a.pause * 2 ** failures)
            say(a, f"Iteration {n} ended badly ({code}); waiting {wait:g} s before the next", stats=stats)
            time.sleep(wait)


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--capture-cli"]:
            sys.exit(forward_cli(sys.argv[2:]))
        main()
    except KeyboardInterrupt:
        print("\n[loop] stopped by Ctrl-C")
