#!/usr/bin/env python3
"""HAUS server: the live page, and the one port every harness reports to.

  python3 server.py                  this machine only
  python3 server.py --host 0.0.0.0   reachable from VMs; requires PROJECT_VIEW_TOKEN to be set
  python3 server.py --init my-project "Find every distinct way to ..."   create a starter project to edit

Each project is a folder under projects/ holding:
  project.json   goal, metric and the sections (pages) this project has
  events.jsonl   append-only log of everything reported

What a harness can call (from another machine, send "Authorization: Bearer <token>"):
  POST /api/events   {"project", "type", "text", "agent", ...}   report anything
  POST /api/claim    {"project", "agent"}                        take the oldest queued task, or null
  POST /api/slot     {"project", "agent"}                        ask to start a round; honours the project's limit on agents at once
  POST /api/project  {"project", "goal", "section": {...}}       set the goal or add a section
  GET  /api/project?project=X                                    read the project's config
  GET  /api/brief?project=X&agent=NAME                           plain-text instructions plus current state
  GET  /api/runs?project=X&agent=NAME                            that agent's runs with the verifier's decision
  GET  /api/found?project=X                                      what is confirmed and what is awaiting the verifier
  GET  /api/coordinator                                           plain-text guide for a chat agent that will run this server for you
  POST /api/coordinate {"tool", "args"}                            the coordinator's tools without MCP (this machine or coordinator)
  POST /api/launch   {"project", "recipe", "count", "rounds"}     start agents from a recipe in launchers.json (this machine or coordinator)
  POST /api/stop     {"project", "agent"}                         stop launched agents
  POST /mcp                                                     the same as MCP tools

Trust: requests from this machine are fully trusted. Remote writers need PROJECT_VIEW_TOKEN.
Only the verifier may post verdicts: from another machine it needs PROJECT_VIEW_VERIFIER_TOKEN.
The coordinator's tools (create projects, start and stop agents, change run settings) need PROJECT_VIEW_COORDINATOR_TOKEN remotely.
The scoring command lives in project.json and can only be changed by editing that file by hand.
Reading (the page and the GET endpoints) is open to anyone who can reach the port.
"""
import datetime as dt
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
PROJECTS = ROOT / "projects"
PORT = int(os.environ.get("PROJECT_VIEW_PORT", 8765))
TOKEN = os.environ.get("PROJECT_VIEW_TOKEN", "")
VERIFIER_TOKEN = os.environ.get("PROJECT_VIEW_VERIFIER_TOKEN", "")
COORDINATOR_TOKEN = os.environ.get("PROJECT_VIEW_COORDINATOR_TOKEN", "")
LOCAL = ("127.0.0.1", "::1")
LOCK = threading.RLock()
NAME = re.compile(r"^\w[\w.-]{0,79}$")
TYPES = ["activity", "run", "verdict", "finding", "question", "item", "task", "tool", "topic", "review", "proposal"]
STRINGS = {"id": 80, "run": 80, "section": 80, "key": 200, "area": 200, "status": 40, "agent": 80,
           "harness": 80, "host": 80, "model": 60, "source": 20, "body": 30000, "answer": 6000}
EVENTS = {}  # project id -> list of events, loaded from disk on first use
BOOT = time.time_ns()


def check_name(project):
    if not isinstance(project, str) or not NAME.match(project):
        raise ValueError("project must be 1-80 letters, digits, dots, dashes or underscores")
    return project


def load(project):
    with LOCK:
        if project not in EVENTS:
            out, f = [], PROJECTS / project / "events.jsonl"
            if f.exists():
                for line in f.read_text().splitlines():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
            EVENTS[project] = out
        return EVENTS[project]


def config(project):
    """Read from disk every time, so hand edits to project.json show up on the page at once."""
    try:
        cfg = json.loads((PROJECTS / project / "project.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def public_config(project):
    cfg = config(project)
    m = cfg.get("metric")
    if isinstance(m, dict):
        cfg["metric"] = {**{k: v for k, v in m.items() if k != "verify"}, "verified": bool(m.get("verify"))}
    return cfg


def project_ids():
    on_disk = {p.name for p in PROJECTS.iterdir() if p.is_dir()} if PROJECTS.exists() else set()
    with LOCK:  # deleting a project's folder removes the project; forget what was cached for it
        for gone in set(EVENTS) - on_disk:
            del EVENTS[gone]
    return sorted(on_disk)


def ingest(data, ip="127.0.0.1", verifier=True, internal=False):
    project, type = check_name(data.get("project")), data.get("type")
    if type not in TYPES:
        raise ValueError("type must be one of: " + ", ".join(TYPES))
    event = {"type": type, "text": str(data.get("text") or "").strip()[:2000]}
    for k, limit in STRINGS.items():
        if data.get(k) is not None:
            event[k] = str(data[k])[:limit]
    for k in ("score", "ms"):
        v = data.get(k)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
                raise ValueError(k + " must be a finite number")
            event[k] = v
    if data.get("ok") is not None:
        event["ok"] = bool(data["ok"])
    if data.get("artifact") is not None:
        try:
            size = len(json.dumps(data["artifact"], allow_nan=False))
        except ValueError:
            raise ValueError("artifact must not contain NaN or Infinity")
        if size > 64000:
            raise ValueError("artifact is larger than 64 kB; split it into smaller runs or probes")
        event["artifact"] = data["artifact"]
    v = data.get("value")
    if v is not None:  # what a chart point or a key number shows
        if isinstance(v, bool) or not isinstance(v, (int, float, str)) or (isinstance(v, float) and (v != v or abs(v) == float("inf"))):
            raise ValueError("value must be a finite number or a short string")
        event["value"] = v[:200] if isinstance(v, str) else v
    if data.get("data") is not None:  # a table row's cells
        try:
            if not isinstance(data["data"], dict) or len(json.dumps(data["data"], allow_nan=False)) > 4000:
                raise ValueError
        except ValueError:
            raise ValueError("data must be a JSON object under 4 kB")
        event["data"] = data["data"]

    if type == "run" and data.get("probe"):
        event["probe"] = True  # sent only to be measured; does not count toward the metric
    if type == "verdict":
        if not verifier:
            raise PermissionError("only the verifier may post verdicts")
        if "run" not in event:
            raise ValueError("a verdict needs the run id it is about")
        covers = data.get("covers")  # what the verifier saw this run reach: {"name": count} or ["name", ...]
        if covers is not None:
            covers = {str(k): 1 for k in covers} if isinstance(covers, list) else covers
            if not isinstance(covers, dict) or len(covers) > 200 or not all(
                    isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n < 1e12 for n in covers.values()):
                raise ValueError("covers must map up to 200 names to counts")
            event["covers"] = {str(k)[:200]: n for k, n in covers.items()}
    elif type == "item":
        if not event.get("section") or not event.get("key"):
            raise ValueError("an item needs a section and a key")
    elif type == "topic":  # one entry on the Understanding page; sending the same key again replaces it
        if data.get("diagram") is not None:
            event["diagram"] = clean_diagram(data["diagram"])
        if not event.get("key") or not (event.get("body") or event["text"] or event.get("diagram")):
            raise ValueError("a topic needs a key (its title) and a body, a diagram, or both")
    elif type == "review":  # the person's response to a topic
        if not event.get("key") or event.get("status") not in ("confirmed", "unclear", "simpler"):
            raise ValueError("a review needs the topic's key and a status: confirmed, unclear or simpler")
        event["source"] = "you"
    elif type == "proposal":  # a survey agent's proposed project setup, for the person to approve
        for k, limit in (("config", 20000), ("scorer", 40000)):
            if data.get(k) is not None:
                try:
                    fits = isinstance(data[k], dict) and len(json.dumps(data[k], allow_nan=False)) <= limit
                except ValueError:
                    fits = False
                if not fits:
                    raise ValueError(f"{k} must be a JSON object under {limit // 1000} kB")
                event[k] = data[k]
        if event.get("status") == "approved" and not internal:
            raise PermissionError("a proposal is approved from the page, not by reporting it as approved")
    elif type in ("activity", "finding", "tool", "run") and not event["text"]:
        raise ValueError("text is required")
    if event.get("source") == "you" and ip not in LOCAL:
        raise PermissionError("only the page on the server's own machine may act as the person")
    if event.get("agent"):
        event.setdefault("host", ip)

    with LOCK:
        events = load(project)
        if type in ("run", "task", "question", "proposal"):
            event.setdefault("id", f"{type[0]}{len(events) + 1}")
            known = any(e["type"] == type and e.get("id") == event["id"] for e in events)
            if not known and not event["text"]:
                raise ValueError("text is required")
            if not known and "status" not in event and type in ("task", "proposal"):
                event["status"] = "queued" if type == "task" else "proposed"
        event ={"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **event}
        (PROJECTS / project).mkdir(parents=True, exist_ok=True)
        with open(PROJECTS / project / "events.jsonl", "a") as f:
            f.write(json.dumps(event) + "\n")
        events.append(event)
    return event


def claim(data, ip="127.0.0.1"):
    """Hand the oldest queued task to one agent. The lock means two agents never get the same one."""
    project, agent = check_name(data.get("project")), data.get("agent")
    if not agent:
        raise ValueError("agent is required")
    with LOCK:
        tasks = {}
        for e in load(project):
            if e["type"] == "task" and e.get("id"):
                tasks[e["id"]] = {**tasks.get(e["id"], {}), **e}
        for t in tasks.values():
            if t.get("status") == "queued":
                return ingest({"project": project, "type": "task", "text": t["text"], "id": t["id"], "status": "running",
                               "agent": agent, "harness": data.get("harness"), "host": data.get("host"),
                               "area": t.get("area"), "source": t.get("source")}, ip)
    return None


def pending(project):
    """Runs that no verdict has been posted for yet. The verifier polls this."""
    with LOCK:
        events = load(check_name(project))
        judged = {e.get("run") for e in events if e["type"] == "verdict"}
        return [e for e in events if e["type"] == "run" and e.get("id") not in judged][:50]


def runs_for(project, agent=""):
    """An agent's own runs with the verifier's decision on each, newest last."""
    events = list(load(check_name(project)))
    verdicts = {e.get("run"): e for e in events if e["type"] == "verdict"}
    out = []
    for e in events:
        if e["type"] == "run" and (not agent or e.get("agent") == agent):
            v = verdicts.get(e.get("id"))
            out.append({"id": e.get("id"), "text": e.get("text"), "claimed": e.get("score"),
                        # a probe is only measured for coverage, so "confirmed or not" does not apply to it
                        "state": "pending" if not v else "measured (probe)" if e.get("probe") else "confirmed" if v.get("ok") else "not confirmed",
                        "covers": v.get("covers") if v else None,
                        "measured": v.get("score") if v else None, "detail": v.get("text") if v else None})
    return out[-50:]


def merged(events, type):
    """Latest state of each thing of this type, keyed by id (or by key). "_i" is where it last changed."""
    out = {}
    for i, e in enumerate(events):
        if e["type"] == type:
            k = e.get("id") or e.get("key") or f"_{i}"
            # "asker" is whoever opened it: a later answer from the other side must not change whose question it is
            out[k] = {**out.get(k, {}), **e, "_i": i, "asker": out.get(k, {}).get("asker") or ("you" if e.get("source") == "you" else "agent")}
    return out


def understanding(events):
    """The Understanding page as agents should treat it: each topic with how far the person has vouched for it."""
    topics, reviews, out = merged(events, "topic"), merged(events, "review"), []
    for key, t in topics.items():
        r = reviews.get(key)
        fresh = r and r["_i"] > t["_i"]
        if t.get("source") == "you":
            label = "written by the person; treat as fact"
        elif fresh and r.get("status") == "confirmed":
            label = "confirmed by the person; treat as fact"
        elif fresh:
            label = ("the person could not follow this" if r.get("status") == "unclear" else "the person asked for a simpler explanation") \
                + (f': "{r["text"]}"' if r.get("text") else "") + "; rewrite it"
        else:
            label = "an agent's reading; the person has not checked it"
        out.append({"key": key, "body": t.get("body") or t.get("text") or "", "label": label})
    return out


def hold(project):
    """Why agents should not work on this project right now, or None if they should. One place decides it, so loop.py, the
    brief and the page all agree. The reasons, in order: the person paused it; a setup proposal is with them; the run window
    they set has ended; or they asked for work to stop whenever something needs them, and something does."""
    cfg = config(project)
    if cfg.get("paused"):
        return "paused by the person"
    events = list(load(project))
    tasks, questions = merged(events, "task"), merged(events, "question")
    proposed = any(p.get("status") == "proposed" for p in merged(events, "proposal").values())
    busy = any(t.get("status") in ("queued", "running") for t in tasks.values())
    asked = any(q["asker"] == "you" and not q.get("answer") for q in questions.values())
    if cfg.get("phase") == "survey" and proposed and not busy and not asked:
        return "waiting for the person to review the setup proposal"
    away = cfg.get("away") if isinstance(cfg.get("away"), dict) else {}
    try:
        if away.get("until") and dt.datetime.now(dt.timezone.utc) > dt.datetime.fromisoformat(str(away["until"]).replace("Z", "+00:00")):
            return "the time the person allowed for this run has ended"
    except (ValueError, TypeError):
        pass
    if away.get("when_blocked") == "pause" and not asked:
        needs = sum(1 for q in questions.values() if q["asker"] != "you" and not q.get("answer")) \
            + sum(1 for t in tasks.values() if t.get("status") == "review") + (1 if proposed else 0)
        if needs:
            return f"waiting for the person: {needs} thing{'s' if needs != 1 else ''} need{'' if needs != 1 else 's'} them"
    return None


SLOTS = {}  # project -> {agent: when its slot expires}. In memory only: a restart frees every slot.
SLOT_SECONDS = 15 * 60  # a slot not renewed or released for this long is taken to be a dead agent


def slot(data):
    """An agent asks to start a round (or says it has finished one). With a limit set, only that many hold a slot at once.
    Returns {"granted": bool, "working": [...], "limit": n}. loop.py calls this around every round."""
    project, agent = check_name(data.get("project")), str(data.get("agent") or "")[:80]
    if not agent:
        raise ValueError("agent is required")
    away = config(project).get("away")
    limit = away.get("max_agents") if isinstance(away, dict) else None
    with LOCK:
        held, now = SLOTS.setdefault(project, {}), time.time()
        for name in [a for a, until in held.items() if until < now]:
            del held[name]
        if data.get("release"):
            held.pop(agent, None)
            return {"granted": False, "working": sorted(held), "limit": limit}
        granted = agent in held or not limit or len(held) < limit
        if granted:
            held[agent] = now + SLOT_SECONDS
        return {"granted": granted, "working": sorted(held), "limit": limit}


def active_agents(project, events, within=600):
    """Agents that hold a slot or reported in the last ten minutes, sorted by name. The verifier is not one of them."""
    now, names = dt.datetime.now(dt.timezone.utc), {a for a, t in SLOTS.get(project, {}).items() if t > time.time()}
    for e in reversed(events):
        try:
            if (now - dt.datetime.fromisoformat(e["ts"])).total_seconds() > within:
                break
        except (KeyError, ValueError):
            continue
        if e.get("agent") and e.get("harness") != "verifier.py":
            names.add(e["agent"])
    return sorted(names)


def found(project):
    """What is already confirmed and what is waiting for the verifier, so an agent can check before reporting a repeat."""
    events = list(load(check_name(project)))
    verdicts = {e.get("run"): e for e in events if e["type"] == "verdict"}
    runs = [e for e in events if e["type"] == "run" and not e.get("probe")]
    return {"confirmed": sorted({verdicts[r["id"]].get("text") or r.get("text", "") for r in runs if r.get("id") in verdicts and verdicts[r["id"]].get("ok")}),
            "awaiting_the_verifier": [{"agent": r.get("agent"), "text": r.get("text", "")} for r in runs if r.get("id") not in verdicts][-40:]}


def notable(project):
    """The short list of things worth telling a person who stepped away: what was found, what went wrong, what needs them.
    "open" marks the ones still waiting on them. General on purpose: it knows about results, questions, reviews and setup,
    not about any particular kind of project."""
    cfg, events, out = config(project), list(load(project)), []
    m = cfg.get("metric") if isinstance(cfg.get("metric"), dict) else {}
    higher = m.get("direction") == "higher" or m.get("aggregate") == "max"
    tolerance = m["tolerance"] if isinstance(m.get("tolerance"), (int, float)) else 0.0005
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    add = lambda e, kind, text, page, open=False: out.append({"ts": e.get("ts", ""), "kind": kind, "text": str(text)[:220], "page": page, "open": open})
    runs, seen, best = {}, set(), None
    for e in events:
        if e["type"] == "run":
            runs[e.get("id")] = e
        elif e["type"] == "verdict":
            r = runs.get(e.get("run"))
            if not r or r.get("probe") or not e.get("ok"):
                continue
            if number(r.get("score")) and number(e.get("score")) and abs(r["score"] - e["score"]) > tolerance:
                add(e, "mismatch", f'{r.get("agent") or "An agent"} claimed {r["score"]:g}, but the verifier measured {e["score"]:g}', "runs")
            if m.get("aggregate") == "unique":
                label = e.get("text") or r.get("text", "")
                if label not in seen:
                    seen.add(label)
                    add(e, "found", f"New confirmed result: {label}", "runs")
            elif number(e.get("score")) and (best is None or (e["score"] > best if higher else e["score"] < best)):
                if best is not None:  # the first run is a starting point, not news
                    add(e, "found", f'New best {m.get("name", "score")}: {e["score"]:g} ({r.get("text", "")})', "runs")
                best = e["score"]
        elif e["type"] == "activity" and e.get("text", "").startswith("Loop stopped"):
            add(e, "stopped", f'{e.get("agent") or "An agent"}: {e["text"]}', "agents")
    for q in merged(events, "question").values():
        if q["asker"] != "you":
            if not q.get("answer"):
                add(q, "question", f'{q.get("agent") or "An agent"} asks: {q.get("text", "")}', "understand", True)
        elif q.get("answer"):
            add(q, "answered", f'Your question was answered: {q.get("text", "")}', "understand")
    for p in merged(events, "proposal").values():
        if p.get("status") == "proposed":
            add(p, "setup", f'A setup proposal is ready for you to review: {p.get("text", "")}', "understand", True)
    for t in merged(events, "task").values():
        if t.get("status") == "review":
            add(t, "review", f'Ready for your review: {t.get("text", "")}', "tasks", True)
    out.sort(key=lambda n: n["ts"])
    return [n for n in out if n["open"]][-20:] + [n for n in out if not n["open"]][-30:]


def clean_diagram(d):
    """A diagram is data: boxes and arrows. The page decides how to draw it."""
    if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
        raise ValueError('a diagram needs "nodes": [{"id", "label", "note"}] and "edges": [{"from", "to", "label"}]')
    nodes = [{"id": str(n["id"])[:60], "label": str(n.get("label") or n["id"])[:60], **({"note": str(n["note"])[:300]} if n.get("note") else {})}
             for n in d["nodes"][:40] if isinstance(n, dict) and n.get("id") is not None]
    ids = {n["id"] for n in nodes}
    edges = [{"from": str(e.get("from"))[:60], "to": str(e.get("to"))[:60], **({"label": str(e["label"])[:40]} if e.get("label") else {})}
             for e in (d.get("edges") if isinstance(d.get("edges"), list) else [])[:80]
             if isinstance(e, dict) and str(e.get("from")) in ids and str(e.get("to")) in ids]
    if not nodes:
        raise ValueError("a diagram needs at least one node with an id")
    return {"nodes": nodes, "edges": edges}


# ---------- launching agents from the server ----------
# Off unless launchers.json exists. That file lists named recipes, each an exact harness command, and is only ever
# written by hand. The page and the coordinator can start and stop those recipes; they can never supply a command.

LAUNCH_FILE = ROOT / "launchers.json"
RUNNING = {}  # (project, agent) -> {"proc", "recipe", "started", "ended"}
MAX_RUNNING = 8


def recipes():
    try:
        data = json.loads(LAUNCH_FILE.read_text())
    except (OSError, ValueError):
        return {}
    ok = lambda v: isinstance(v, dict) and isinstance(v.get("command"), list) and v["command"] and all(isinstance(x, str) for x in v["command"])
    return {k: v for k, v in data.items() if NAME.match(str(k)) and ok(v)} if isinstance(data, dict) else {}


def launched(project=None):
    """Agents this server started: still running, or finished in the last ten minutes."""
    out, now = [], time.time()
    with LOCK:
        for key, r in list(RUNNING.items()):
            alive = r["proc"].poll() is None
            if not alive:
                r.setdefault("ended", now)
                if now - r["ended"] > 600:
                    del RUNNING[key]
                    continue
            if project in (None, key[0]):
                out.append({"project": key[0], "agent": key[1], "recipe": r["recipe"], "alive": alive, "started": r["started"]})
    return out


def launch(data):
    project, wanted = check_name(data.get("project")), recipes()
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    if not wanted:
        raise ValueError("launching is off: create launchers.json next to server.py (see launchers.example.json)")
    name = data.get("recipe")
    if name not in wanted:
        raise ValueError("recipe must be one of: " + ", ".join(wanted))
    recipe = wanted[name]
    count = data.get("count", 1)
    rounds = data.get("rounds", recipe.get("rounds", 4))
    if not (isinstance(count, int) and 1 <= count <= 5 and isinstance(rounds, int) and 1 <= rounds <= 500):
        raise ValueError("count must be 1 to 5 and rounds 1 to 500")
    started = []
    with LOCK:
        alive = [k for k, r in RUNNING.items() if r["proc"].poll() is None]
        if len(alive) + count > MAX_RUNNING:
            raise ValueError(f"at most {MAX_RUNNING} launched agents may run at once; {len(alive)} are running")
        taken, n = {a for (p, a) in alive if p == project}, 1
        for _ in range(count):
            while f"{name}-{n}" in taken:
                n += 1
            agent = f"{name}-{n}"
            taken.add(agent)
            work = ROOT / "agent-work" / project / agent
            fresh = not work.exists()
            work.mkdir(parents=True, exist_ok=True)
            seed = config(project).get("agent_files")
            if fresh and seed and Path(seed).is_dir():  # what each agent starts with: a copy of the target, a spec
                shutil.copytree(seed, work, dirs_exist_ok=True)
            env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
            env["PROJECT_VIEW_URL"] = f"http://127.0.0.1:{PORT}"
            command = [sys.executable, str(ROOT / "loop.py"), project, "--agent", agent, "--harness", str(recipe.get("label") or name),
                       "--max-iterations", str(rounds), "--timeout-min", str(recipe.get("timeout_min", 10)), "--"] + recipe["command"]
            log = open(work / "loop.log", "a")
            proc = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            RUNNING[(project, agent)] = {"proc": proc, "recipe": name, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            started.append(agent)
    return {"started": started, "rounds": rounds, "recipe": name}


def stop(data):
    """Stop one launched agent, or every launched agent on the project. Ends the loop and whatever harness it is running."""
    project, agent, stopped = check_name(data.get("project")), data.get("agent"), []
    with LOCK:
        for (p, a), r in list(RUNNING.items()):
            if p == project and agent in (None, "", a) and r["proc"].poll() is None:
                try:
                    os.killpg(r["proc"].pid, signal.SIGTERM)
                except OSError:
                    pass
                stopped.append(a)
    for a in stopped:
        SLOTS.get(project, {}).pop(a, None)
        ingest({"project": project, "type": "activity", "text": "Loop stopped: stopped by the person", "agent": a})
    return {"stopped": stopped}


def status(project):
    """A plain-language summary of one project, for the coordinator to read out."""
    cfg, events = config(check_name(project)), list(load(project))
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    f, tasks, questions = found(project), merged(events, "task"), merged(events, "question")
    count = lambda st: sum(1 for t in tasks.values() if t.get("status") == st)
    lines = [f'Project "{project}" ({"being set up" if cfg.get("phase") == "survey" else "running"})',
             "Goal: " + (cfg.get("goal") or "not set yet"),
             "On hold: " + (hold(project) or "no"),
             f'Confirmed results: {len(f["confirmed"])}' + (" (" + ", ".join(f["confirmed"][:12]) + ")" if f["confirmed"] else ""),
             f'Awaiting the verifier: {len(f["awaiting_the_verifier"])}',
             f'Tasks: {count("queued")} queued, {count("running")} running, {count("review")} waiting for the person, {count("done")} done',
             "Agents active in the last ten minutes: " + (", ".join(active_agents(project, events)) or "none"),
             "Agents launched from this server: " + (", ".join(f'{x["agent"]} ({"running" if x["alive"] else "finished"})' for x in launched(project)) or "none")]
    needs = [n for n in notable(project) if n["open"]]
    lines += ["Needs the person:"] + [f'  - {n["text"]}' for n in needs] if needs else ["Nothing is waiting on the person."]
    asks = [q for q in questions.values() if q["asker"] != "you" and not q.get("answer")]
    if asks:
        lines += ["Open questions from agents (id: text):"] + [f'  - {q["id"]}: {q.get("text", "")}' for q in asks]
    finds = [e for e in events if e["type"] == "finding"][-6:]
    if finds:
        lines += ["Latest findings:"] + [f'  - {e.get("agent") or "?"}: {e.get("text", "")}' for e in finds]
    return "\n".join(lines)


def new_project(data):
    """Start a project in its setup phase: a survey agent studies the target and proposes the rest."""
    project = check_name(data.get("project"))
    target = str(data.get("target") or "").strip()[:4000]
    if not target:
        raise ValueError("say what the target is and where it lives")
    with LOCK:
        if (PROJECTS / project).exists():
            raise ValueError("a project with that name already exists")
        (PROJECTS / project).mkdir(parents=True)
        (PROJECTS / project / "project.json").write_text(json.dumps(
            {"phase": "survey", "target": target, "goal": str(data.get("goal") or "").strip()[:2000], "sections": [],
             **({"agent_files": str(data["agent_files"])[:500]} if data.get("agent_files") else {})}, indent=2) + "\n")
        load(project)
    return public_config(project)


def approve(data):
    """The person accepts a setup proposal: its goal, metric, map and scoring script become the project, and the run phase starts.
    Only reachable from the server's own machine. It writes the proposed scoring script to disk, where the verifier will run it,
    so the page shows that script in full before offering this."""
    project = check_name(data.get("project"))
    with LOCK:
        prop = merged(list(load(project)), "proposal").get(str(data.get("id")))
        if not prop or prop.get("status") != "proposed":
            raise ValueError("no proposal with that id is waiting for approval")
        c, cfg = prop.get("config") or {}, config(project)
        for k in ("goal", "summary", "rules"):
            if isinstance(c.get(k), str) and c[k].strip():
                cfg[k] = c[k][:2000]
        metric = {k: c["metric"][k] for k in ("name", "direction", "aggregate", "tolerance", "artifact") if k in c.get("metric", {})} \
            if isinstance(c.get("metric"), dict) else {}
        source = (prop.get("scorer") or {}).get("source")
        if isinstance(source, str) and source.strip():
            (PROJECTS / project / "score.py").write_text(source)
            metric["verify"] = "python3 score.py"
        if metric:
            cfg["metric"] = metric
        sections = []
        for s in c.get("sections", [])[:12] if isinstance(c.get("sections"), list) else []:
            one = clean_section(s)
            if isinstance(s.get("measured"), dict):  # approving is the one place thresholds can be set without editing the file
                one["measured"] = {str(k)[:40]: n for k, n in s["measured"].items() if isinstance(n, (int, float)) and not isinstance(n, bool)}
            sections.append(one)
        if sections:
            cfg["sections"] = sections
        cfg["phase"] = "run"
        (PROJECTS / project / "project.json").write_text(json.dumps(cfg, indent=2) + "\n")
        for entry in c.get("entries", [])[:300] if isinstance(c.get("entries"), list) else []:
            if isinstance(entry, dict) and entry.get("section") and entry.get("key"):
                ingest({"project": project, "type": "item", "section": entry["section"], "key": entry["key"], "text": entry.get("text") or ""})
        ingest({"project": project, "type": "proposal", "id": prop["id"], "status": "approved", "source": "you"}, internal=True)
    return public_config(project)


def clean_section(s):
    if not isinstance(s, dict) or not NAME.match(str(s.get("id") or "")):
        raise ValueError("a section needs an id made of letters, digits, dots, dashes or underscores")
    out = {"id": str(s["id"]), "title": str(s.get("title") or s["id"])[:40], "view": s.get("view") if s.get("view") in VIEWS else "grid"}
    if s.get("icon"):
        out["icon"] = str(s["icon"])[:20]
    if isinstance(s.get("columns"), list):
        out["columns"] = [str(x)[:40] for x in s["columns"][:12]]
    if s.get("unit"):
        out["unit"] = str(s["unit"])[:20]
    if isinstance(s.get("statuses"), list):
        out["statuses"] = [str(x)[:40] for x in s["statuses"][:8]]
    if s.get("progress"):
        out["progress"] = True
    return out


def set_project(data):
    """Set the goal, summary, metric labels or sections. The scoring command cannot be set from here."""
    project = check_name(data.get("project"))
    with LOCK:
        cfg = config(project)
        if cfg.get("phase") == "survey" and any(k in data for k in ("goal", "summary", "rules", "metric", "sections", "section")):
            raise ValueError("this project is still being set up: send a proposal for the person to approve instead")
        for k in ("goal", "summary", "rules", "target"):
            if isinstance(data.get(k), str):
                cfg[k] = data[k][:4000 if k == "target" else 2000]
        if isinstance(data.get("away"), dict):  # what agents do while the person is away
            away = {"when_blocked": "pause" if data["away"].get("when_blocked") == "pause" else "continue"}
            limit = data["away"].get("max_agents")
            if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit <= 100:
                away["max_agents"] = limit  # at most this many agents working at once; absent means no limit
            if data["away"].get("until"):
                try:
                    dt.datetime.fromisoformat(str(data["away"]["until"]).replace("Z", "+00:00"))
                    away["until"] = str(data["away"]["until"])[:40]
                except ValueError:
                    raise ValueError("away.until must be a date and time in ISO form")
            cfg["away"] = away
        if isinstance(data.get("paused"), bool):
            cfg["paused"] = data["paused"]
        if isinstance(data.get("metric"), dict):
            old = cfg.get("metric") if isinstance(cfg.get("metric"), dict) else {}
            new = {k: data["metric"][k] for k in ("name", "direction", "aggregate", "tolerance", "artifact") if k in data["metric"]}
            cfg["metric"] = {**new, **({"verify": old["verify"]} if old.get("verify") else {})}
        # "measured" (which statuses the verifier's counts earn) is like the scoring command: only settable by hand,
        # and it survives an agent redefining the section.
        kept = {s["id"]: {k: s[k] for k in ("measured", "unit", "statuses") if k in s}
                for s in cfg.get("sections", []) if isinstance(s, dict) and s.get("id") and s.get("measured")}
        keep = lambda s: {**s, **kept[s["id"]]} if s["id"] in kept else s
        if isinstance(data.get("sections"), list):
            cfg["sections"] = [keep(clean_section(s)) for s in data["sections"][:12]]
        if data.get("section") is not None:
            one = keep(clean_section(data["section"]))
            rest = [s for s in cfg.get("sections", []) if isinstance(s, dict) and s.get("id") != one["id"]]
            was = next((i for i, s in enumerate(cfg.get("sections", [])) if isinstance(s, dict) and s.get("id") == one["id"]), len(rest))
            cfg["sections"] = (rest[:was] + [one] + rest[was:])[:12]
        (PROJECTS / project).mkdir(parents=True, exist_ok=True)
        (PROJECTS / project / "project.json").write_text(json.dumps(cfg, indent=2) + "\n")
        load(project)
    return public_config(project)


# ---------- the brief: everything an agent with no memory needs, generated from the project ----------

VIEWS = {
    "grid": 'one card per key. Send {"section", "key", "status", "text", "body"}',
    "board": 'one card per key, placed in the column named by its status. Send {"section", "key", "status", "text"}',
    "table": 'one row per key. Send {"section", "key", "data": {"<column>": value, ...}}',
    "chart": 'a line chart: every report adds one point, one line per key. Send {"section", "key", "value": <number>}',
    "stats": 'one big number per key. Send {"section", "key", "value": <number or short string>, "text": "<caption>"}',
    "note": 'one block of prose per key, replaced each time. Send {"section", "key", "body": "<text>"}',
}


def measured_status(section, count):
    """The highest status whose threshold the verifier's count has reached."""
    statuses, best = section.get("statuses") or [""], None
    for name in statuses:
        need = (section.get("measured") or {}).get(name)
        if isinstance(need, (int, float)) and count >= need:
            best = name
    return best or statuses[0]


def survey_brief(cfg, events, post, who, known, writing, person):
    """The brief for a project still being set up: study the target, teach the person, propose the project. No attempts yet."""
    proposals = merged(events, "proposal")
    waiting_one = next((p for p in proposals.values() if p.get("status") == "proposed"), None)
    returned = [p for p in proposals.values() if p.get("status") == "returned"]
    tasks = [t for t in merged(events, "task").values() if t.get("status") == "queued"]
    out = ["THIS PROJECT IS BEING SET UP. You are its survey agent. There is no goal to chase yet.",
           "Your job is to understand the target, teach the person what you find, and propose how the project should run.",
           "Do NOT start attempting anything against the target beyond what you need to understand it.",
           "",
           "THE TARGET (in the person's words)", cfg.get("target") or "(not described; ask what it is)",
           "", "WHAT THE PERSON HOPES TO DO", cfg.get("goal") or "(not said; propose a sensible goal from what you find)",
           "", *known]
    out += ["", *person] if person else []
    if tasks:
        out += ["", "THE PERSON SENT WORK BACK (do these first; take each with the claim command, close it when done)"]
        out += [f'- [{t["id"]}] {t.get("text", "")}' for t in tasks[:8]]
        out += ["  " + post % ("claim", "{" + who + "}"),
                "  " + post % ("events", "{" + who + ', "type": "task", "id": "<task id>", "text": "<task text>", "status": "done"}')]
    if waiting_one and not tasks and not any(l.startswith("THE PERSON ASKED") for l in person):
        return out + ["", f'YOUR PROPOSAL "{waiting_one.get("text", "")}" IS WAITING FOR THE PERSON TO REVIEW. There is nothing to do until they respond. Stop now.']
    out += ["", "WHAT TO DO, IN ORDER",
            "1. Say what you are doing as you go, one short line each time it changes.",
            "   " + post % ("events", "{" + who + ', "type": "activity", "text": "..."}'),
            "2. Study the target until you could explain it to someone else.",
            "3. Write the Understanding page. Cover at least: what this is and what it is for; how it works, step by step; its parts,",
            "   one line each; where problems are most likely and why; and the words a newcomer needs, each defined.",
            "4. Ask the person anything only they can answer (what they care about most, what is out of bounds). Then keep going;",
            "   do not wait for the answer.",
            "   " + post % ("events", "{" + who + ', "type": "question", "text": "..."}'),
            "5. Propose the setup, as ONE proposal (below). If an earlier proposal was sent back, send a new one that deals with",
            "   the person's comments.",
            "6. Stop. The person reads the page and the proposal, and either approves it or sends it back with comments.",
            "From another machine, add to every command:  -H \"Authorization: Bearer $PROJECT_VIEW_TOKEN\"",
            "", *writing,
            "", "THE PROPOSAL",
            "Send one JSON object. It is long, so build it in a script or a file rather than on one command line, for example:",
            "  write it to proposal.json, then:  " + (post % ("events", "@")).replace("-d '@'", "--data-binary @proposal.json"),
            "{",
            "  " + who + ', "type": "proposal",',
            '  "text": "<one line: what you propose>",',
            '  "body": "<why this setup, in a few short paragraphs the person can follow; Markdown allowed>",',
            '  "config": {',
            '    "goal": "<one sentence: what the agents will try to achieve>",',
            '    "summary": "<one or two sentences on how the work will go>",',
            '    "rules": "<what agents must and must not do; where the target is; how to try something locally>",',
            '    "metric": {"name": "<what is counted>", "aggregate": "unique | max | min", "direction": "higher | lower",',
            '               "artifact": "<exactly what an agent attaches to a run so it can be re-checked>"},',
            '    "sections": [',
            '      {"id": "<short-id>", "title": "<page title>", "view": "grid", "statuses": ["untouched", "probed", "covered"], "progress": true,',
            '       "unit": "<what is counted, e.g. inputs>", "measured": {"probed": 1, "covered": 300}},',
            '      {"id": "<short-id>", "title": "<page title>", "view": "board", "statuses": ["suspected", "confirmed", "rejected"]}',
            "    ],",
            '    "entries": [{"section": "<the map section id>", "key": "<one part of the target>", "text": "<one line on it>"}]',
            "  },",
            '  "scorer": {"source": "<the full text of a Python 3 scoring script>"}',
            "}",
            "- The map (the first kind of section) lists the parts of the target worth covering, one entry each. Give it \"measured\"",
            "  thresholds only if your scoring script can report which parts a run reached.",
            "- aggregate \"unique\" suits goals of the form \"find every distinct ...\"; \"max\" or \"min\" suit a single number to push.",
            "- The scoring script is what makes results trustworthy, so the person will read all of it. Keep it short and plain:",
            "  Python 3, standard library only. It reads one run's artifact as JSON on standard input and prints ONE line of JSON:",
            '    {"ok": true or false, "score": <number>, "detail": "<short label; equal labels count once under unique>",',
            '     "covers": {"<map entry key>": <count>}}        (covers is optional)',
            "  \"ok\" means \"this run counts as a result\". Print ok false for anything that should not: an attempt that did not",
            "  succeed, a malformed artifact, a batch sent where one result was expected. Under \"unique\", every distinct detail",
            "  among the ok results is counted once, so an ok result labelled \"nothing found\" would wrongly count as a finding.",
            "  It runs with the project's folder as its working directory, on the machine where the verifier runs, so refer to the",
            "  target by absolute path. It must give the same answer every time, must not trust anything inside the artifact, and",
            "  must not change any file.",
            "- Test the script yourself on a result that should count and one that should not, before you propose it."]
    if returned:
        out += ["", "EARLIER PROPOSALS THE PERSON SENT BACK"] + [f'- "{p.get("text", "")}": {p.get("answer") or "see the tasks above for their comments"}' for p in returned[-3:]]
    return out


def brief(project, agent, base):
    cfg, events = config(check_name(project)), list(load(project))
    m = cfg.get("metric") if isinstance(cfg.get("metric"), dict) else {}
    agent = re.sub(r"[^\w./@:-]", "_", agent or "")[:80] or "<your-name>"  # it is pasted into shell examples below
    # "round-notes" is written by loop.py from each round's closing message, so agents are not asked to maintain it
    sections = [s for s in cfg.get("sections", []) if isinstance(s, dict) and s.get("id") and s["id"] != "round-notes"]
    verdicts = {e.get("run"): e for e in events if e["type"] == "verdict"}
    runs, tasks, items = {}, {}, {}
    for e in events:
        if e["type"] == "run":
            runs[e.get("id")] = e
        elif e["type"] == "task":
            tasks[e.get("id")] = {**tasks.get(e.get("id"), {}), **e}
        elif e["type"] == "item":
            items.setdefault(e.get("section"), {})[e.get("key")] = {**items.get(e.get("section"), {}).get(e.get("key"), {}), **e}
    reach = {}  # what the verifier has measured, summed over every run and probe it replayed
    for v in verdicts.values():
        for k, n in (v.get("covers") or {}).items():
            reach[k] = reach.get(k, 0) + n
    probes = sum(1 for r in runs.values() if r.get("probe"))
    runs = {i: r for i, r in runs.items() if not r.get("probe")}
    good = [(r, verdicts[i]) for i, r in runs.items() if i in verdicts and verdicts[i].get("ok")]
    bad = [(r, verdicts[i]) for i, r in runs.items() if i in verdicts and not verdicts[i].get("ok")]
    post = f"curl -s -X POST {base}/api/%s -H 'Content-Type: application/json' -d '%s'"
    who = f'"project": "{project}", "agent": "{agent}"'

    out = [f'You are an agent working on the project "{project}". You report to HAUS at {base}.',
           f'Your name here is "{agent}". Send it as "agent" in every request.', ""]
    reason = hold(project)
    if reason:
        return "\n".join(out + [f"THE PROJECT IS ON HOLD: {reason}. Do no work. Stop now; you will be started again when that changes."]) + "\n"

    # ----- what the person and the agents share: the Understanding page, and the questions going both ways -----
    topic_cmd = post % ("events", "{" + who + ', "type": "topic", "key": "<short title>", "body": "<the explanation, Markdown allowed>"}')
    known = ["WHAT IS UNDERSTOOD SO FAR (the Understanding page: the person reads it to learn this target, and so do you)"]
    budget = 9000
    for u in understanding(events):
        body = u["body"][:max(300, min(2500, budget))]
        budget -= len(body)
        known += [f'## {u["key"]}   [{u["label"]}]', body + (" ..." if len(body) < len(u["body"]) else ""), ""]
    known += ["(nothing has been written yet)"] if len(known) == 1 else []
    writing = ["HOW TO WRITE FOR THE UNDERSTANDING PAGE",
               "The reader is sharp but new to this, and wants to actually learn it, not just be told the result. You may know more",
               "than they do here; your job is to bring them with you.",
               "- Open each topic with one or two plain sentences a newcomer could repeat back to someone else.",
               "- Then the detail. Define each term the first time you use it. Prefer one small concrete example to a general description.",
               "- Say why it matters for the goal, and what you are unsure of. Never present a guess as a fact.",
               "- One idea per topic, with a short title. To change a topic, send the same key again; do not pile up near-duplicates.",
               "- Leave topics the person wrote or confirmed alone, unless you have found one to be wrong. Then fix it, and say in its",
               "  first line what changed and why.",
               "- Show it as well as say it. A picture of how the parts fit together teaches faster than paragraphs. Give at least",
               "  one topic a diagram, the system at a glance, by adding this to the topic (you describe boxes and arrows; the page",
               "  draws them):",
               '    "diagram": {"nodes": [{"id": "a", "label": "<short name>", "note": "<one line on what it does>"}, ...],',
               '                "edges": [{"from": "a", "to": "b", "label": "<what passes between them, optional>"}, ...]}',
               "  Keep it to what a newcomer needs: the main parts and what flows between them, at most a dozen or so boxes.",
               "  " + topic_cmd]
    questions = merged(events, "question")
    asked = [q for q in questions.values() if q["asker"] == "you" and not q.get("answer")]
    answered = [q for q in questions.values() if q["asker"] != "you" and q.get("answer")][-8:]
    pending = [q for q in questions.values() if q["asker"] != "you" and not q.get("answer")][-8:]
    person = []
    if asked:
        person += ["THE PERSON ASKED (answering these comes before any other work; answer so that they learn something)"]
        for q in asked[:8]:
            person += [f'- [{q["id"]}]' + (f' about "{q["key"]}"' if q.get("key") else "") + f': {q.get("text", "")}',
                       "  " + post % ("events", "{" + who + f', "type": "question", "id": "{q["id"]}", "answer": "<your answer, Markdown allowed>"}}')]
    if answered:
        person += ["THE PERSON ANSWERED (treat as fact)"] + [f'- Q: {q.get("text", "")}\n  A: {q["answer"]}' for q in answered]
    if pending:
        person += ["ALREADY ASKED, NOT YET ANSWERED (do not ask again)"] + [f'- {q.get("text", "")}' for q in pending]

    # A fresh round remembers nothing. Hand it what it left half-done and what it last told the person.
    carry = []
    left = [t for t in merged(events, "task").values() if t.get("status") == "running" and t.get("agent") == agent]
    if left:
        carry += ["TASKS YOU TOOK IN AN EARLIER ROUND AND NEVER CLOSED (finish or close these before taking anything new)"]
        carry += [f'- [{t["id"]}] {t.get("text", "")}' for t in left[:5]]
    notes = [e for e in events if e["type"] == "item" and e.get("section") == "round-notes" and e.get("agent") == agent and e.get("body")]
    if notes:
        carry += ["YOUR CLOSING NOTE FROM YOUR LAST ROUND (it is your only memory of that round)", notes[-1]["body"][-2000:]]
    person = carry + ([""] if carry and person else []) + person

    # Several agents with the same brief will chase the same things unless each knows what the others are doing.
    others = [a for a in active_agents(project, events) if a != agent]
    team = []
    if others:
        all_tasks, last = merged(events, "task"), {}
        for e in events:
            if e["type"] == "activity" and e.get("agent") in others:
                last[e["agent"]] = e["text"]
        team += [f"YOU ARE NOT ALONE: {len(others)} other agent{'s are' if len(others) != 1 else ' is'} working on this project right now. Do not redo their work."]
        for name in others:
            doing = [t.get("text", "") for t in all_tasks.values() if t.get("status") == "running" and t.get("agent") == name]
            team.append(f'- {name}: ' + ("; ".join(doing[:2]) if doing else last.get(name, "just started")))
        in_flight = [r for r in found(project)["awaiting_the_verifier"] if r["agent"] != agent]
        if in_flight:
            team += ["Results the others have reported that the verifier has not decided yet (treat as taken):"] + [f'- {r["agent"]}: {r["text"]}' for r in in_flight[-15:]]
        # Give each agent a different part of the map to start from, least-covered first, dealt out in turn by name.
        section = next((s for s in sections if s.get("measured") or s.get("progress")), None)
        if section:
            reach = {}
            for e in events:
                if e["type"] == "verdict":
                    for k, n in (e.get("covers") or {}).items():
                        reach[k] = reach.get(k, 0) + n
            keys = [k for k in merged([e for e in events if e["type"] == "item" and e.get("section") == section["id"]], "item")]
            keys.sort(key=lambda k: reach.get(k, 0))
            everyone = sorted(others + [agent])
            mine = keys[everyone.index(agent)::len(everyone)]
            if mine:
                team += [f'YOUR SHARE of {section.get("title", section["id"])} this round (the others have the rest; start here, and only go wider once yours are done):',
                         "  " + ", ".join(mine[:40])]
        team += ["When you create a task, name the specific part or idea it covers, so the others can see what is taken.",
                 "Just before you report a run, check it is not already found or in flight:",
                 f"  curl -s '{base}/api/found?project={project}'"]
        person = team + ([""] if person else []) + person

    if cfg.get("phase") == "survey":
        return "\n".join(out + survey_brief(cfg, events, post, who, known, writing, person)) + "\n"

    out += ["GOAL", cfg.get("goal") or "(no goal has been set; ask for one with a question)"]
    out += [cfg["summary"]] if cfg.get("summary") else []
    out += ["", "RULES", cfg["rules"]] if cfg.get("rules") else []

    out += ["", "HOW PROGRESS IS MEASURED"]
    if m:
        how = {"unique": "the number of distinct confirmed results", "max": "the highest confirmed score", "min": "the lowest confirmed score"}
        out.append(f'The metric is "{m.get("name", "score")}": {how.get(m.get("aggregate") or ("max" if m.get("direction") == "higher" else "min"))}.')
        if m.get("verify"):
            out.append("You do not score yourself. A separate verifier re-scores every run from its artifact, and only what it confirms counts. "
                       "Reporting a number or result that does not hold up is recorded against you.")
        else:
            out.append("No verifier is set up, so your own reported scores are shown as unverified claims. Be exact.")
        if m.get("artifact"):
            out.append("A run's artifact must be: " + m["artifact"])
        out.append("An artifact can be at most 64 kB of JSON. Split anything larger into several runs or probes.")
    else:
        out.append("No metric is set. Report findings; do not report runs.")

    out += ["", *known]
    out += ["", *person] if person else []
    out += ["", "HOW YOU WORK",
            "The page is the only thing the person running this project sees. Work that is not reported there did not happen, as far",
            "as they can tell. Reporting is part of the job, not an extra. Do ONE solid piece of work this round (one task), report it",
            "fully, then stop. You will be started again with fresh state, so there is no need to do everything at once.",
            "",
            "Do these in order. Each is one command.",
            "1. FIRST, before any other work, take a task. If the reply is null, decide the most useful next step yourself and create",
            "   a task for it (second command; the reply contains its id).",
            "   " + post % ("claim", "{" + who + "}"),
            "   " + post % ("events", "{" + who + ', "type": "task", "text": "<what you will do>", "status": "running"}'),
            "2. Before each distinct piece of work, post one short line saying what you are about to do.",
            "   " + post % ("events", "{" + who + ', "type": "activity", "text": "..."}'),
            "3. Report each attempt worth scoring as a run. The reply contains its id.",
            "   " + post % ("events", "{" + who + ', "type": "run", "text": "<one-line description>", "score": <your number, optional>, "artifact": <see above>}'),
            "4. After reporting runs, wait a few seconds and read what the verifier decided about them.",
            f"   curl -s '{base}/api/runs?project={project}&agent={agent}'",
            "5. Update every page below that your work changed. The exact command for each page is listed under THIS PROJECT'S PAGES.",
            "   That includes the Understanding page: if what you learned changes how the target should be explained, or a topic is",
            "   missing, flagged by the person, or wrong, write or rewrite that topic.",
            "6. Post what you learned that the next agent needs, as a finding (\"body\" is optional longer text).",
            "   " + post % ("events", "{" + who + ', "type": "finding", "text": "...", "body": "..."}'),
            "7. LAST, close your task: status \"done\", or \"review\" if a person should look before it counts as finished.",
            "   " + post % ("events", "{" + who + ', "type": "task", "id": "<task id>", "text": "<task text>", "status": "done"}'),
            "If only a person can unblock you, ask with type \"question\", then carry on with something else.",
            "Keep your files in your own working folder, not in shared places such as /tmp. A script you find that you did not",
            "write this round may be stale, or written for a different project: read it before you run it, and never report",
            f'to any project other than "{project}".',
            "From another machine, add to every command:  -H \"Authorization: Bearer $PROJECT_VIEW_TOKEN\"",
            f"To re-read this brief with fresh state:  curl -s '{base}/api/brief?project={project}&agent={agent}'",
            "", *writing]

    def example(s):
        fields = {"section": s["id"], "key": "<name>"}
        if s.get("view") == "table":
            fields["data"] = {c: "<value>" for c in s.get("columns") or ["<column>"]}
        elif s.get("view") in ("chart", "stats"):
            fields["value"] = "<number>"
        elif s.get("view") == "note":
            fields["body"] = "<text>"
        else:
            if s.get("statuses"):
                fields["status"] = "<" + " | ".join(s["statuses"]) + ">"
            fields["text"] = "<one line>"
        return post % ("events", "{" + who + ', "type": "item", ' + json.dumps(fields)[1:])

    out += ["", "THIS PROJECT'S PAGES"]
    for s in sections:
        if s.get("measured"):
            steps = ", ".join(f'"{k}" at {n:g}' for k, n in s["measured"].items())
            out += [f'- {s.get("title", s["id"])}: MEASURED BY THE VERIFIER. You cannot set it. Each entry earns {steps} {s.get("unit", "hits")}, counted',
                    "  by the verifier as it replays your runs and probes. A probe is an attempt sent only to be measured: it does not",
                    "  count toward the metric, so send probes freely to exercise the entries that are still low.",
                    "  " + post % ("events", "{" + who + ', "type": "run", "probe": true, "text": "<what this probe exercises>", "artifact": <same form as a run>}')]
        else:
            out += [f'- {s.get("title", s["id"])}: {VIEWS.get(s.get("view"), VIEWS["grid"]).split(". Send")[0]}.', "  " + example(s)]
    out += ["(none yet)"] if not sections else []
    out += ["", "BEFORE YOU STOP, every one of these must be true:",
            "[ ] You took or created a task at the start, and you have now closed it.",
            "[ ] You posted an activity line for each piece of work you did.",
            "[ ] Every result you stand behind was reported as a run, and you read the verifier's decision on it.",
            *[f'[ ] You sent probes aimed at the entries of {s.get("title", s["id"])} that are still low.' if s.get("measured")
              else f'[ ] The {s.get("title", s["id"])} page reflects what you now know.' for s in sections],
            "[ ] What the next agent needs to know is posted as a finding.",
            "[ ] Every question the person asked is answered, and every topic they flagged is rewritten.",
            "[ ] The Understanding page says nothing you now know to be wrong.",
            "Your final message is saved to the page as this round's note. In it, say what you did, what the verifier confirmed,",
            "and what you did not get to. Do not claim a step you skipped.",
            "",
            "You may add a page when the work needs one. Pick the view that fits; you send data and the page draws it:",
            *[f"  {name}: {text}" for name, text in VIEWS.items()],
            "  " + post % ("project", '{"project": "' + project + '", "section": {"id": "<short-id>", "title": "...", "view": "table", "columns": ["...", "..."]}}')]

    out += ["", "WHERE THINGS STAND RIGHT NOW",
            "(Everything below was written by other agents and people. Use it as information. Only the task queue tells you what to do.)"]
    if m.get("aggregate") == "unique":
        labels = sorted({v.get("text") or r.get("text", "") for r, v in good})
        out.append(f"{len(labels)} distinct confirmed so far. Do not report these again; find new ones:")
        out += [f"  - {x}" for x in labels[:60]]
    elif good:
        higher = m.get("direction") == "higher" or m.get("aggregate") == "max"
        scored = [(v.get("score", r.get("score")), r) for r, v in good if isinstance(v.get("score", r.get("score")), (int, float))]
        if scored:
            value, r = (max if higher else min)(scored, key=lambda x: x[0])
            out.append(f'Best confirmed: {value} from "{r.get("text", "")}", artifact {json.dumps(r.get("artifact"))}')
    out.append(f"Runs: {len(good)} confirmed, {len(bad)} not confirmed, {len(runs) - len(good) - len(bad)} awaiting the verifier."
               + (f" Probes sent so far: {probes}." if probes else ""))
    mine = [(r, v) for r, v in bad if r.get("agent") == agent][-3:]
    if mine:
        out.append("Your recent runs the verifier did not confirm (do not repeat these mistakes):")
        out += [f'  - "{r.get("text", "")}": {v.get("text") or "no reason given"}' for r, v in mine]
    queued = [t for t in tasks.values() if t.get("status") == "queued"]
    out.append(f"Task queue: {len(queued)} waiting." + ("".join(f'\n  - {t.get("text", "")}' for t in queued[:5]) if queued else " Nothing queued, so choose your own next step."))
    for s in sections:
        rows = list(items.get(s["id"], {}).values())
        if s.get("measured"):
            names = list(dict.fromkeys([i.get("key") for i in rows] + [k for k in reach if k not in items.get(s["id"], {})]))
            out.append(f'{s.get("title", s["id"])} (measured): ' + "; ".join(
                f'{k} [{measured_status(s, reach.get(k, 0))}, {reach.get(k, 0):g} {s.get("unit", "hits")}]' for k in names[:40]))
        elif rows and s.get("view") in (None, "grid", "board"):
            out.append(f'{s.get("title", s["id"])}: ' + "; ".join(f'{i.get("key")} [{i.get("status", "no status")}]' for i in rows[:40]))
    finds = [e for e in events if e["type"] == "finding"][-8:]
    if finds:
        out += ["Latest findings:"] + [f'  - {e.get("text", "")}' for e in finds]
    return "\n".join(out) + "\n"


# ---------- MCP: the same calls as tools, one JSON-RPC message in, one JSON reply out ----------

WHO = {"project": {"type": "string"}, "agent": {"type": "string", "description": "Who is calling, e.g. vm-2/codex"}, "harness": {"type": "string"}}
MCP_TOOLS = [
    {"name": "report", "description": "Report to the project view. Types: activity (what you are doing now), run (an attempt, with the artifact the "
     "verifier needs to re-score it), finding, question (ask the person, or answer one of theirs), item (an entry in one of the project's sections), "
     "task (create or update), tool (one tool call), topic (an entry on the Understanding page: key is its title, body the explanation), "
     "proposal (a survey agent's proposed setup).",
     "inputSchema": {"type": "object", "required": ["project", "type"], "properties": {
         **WHO, "type": {"type": "string", "enum": [t for t in TYPES if t != "verdict"]}, "text": {"type": "string"},
         "id": {"type": "string", "description": "run or task id; omit to have one assigned"},
         "score": {"type": "number", "description": "run: the score you claim"}, "artifact": {"description": "run: what the verifier scores"},
         "probe": {"type": "boolean", "description": "run: true to send it only for the verifier to measure coverage; it does not count toward the metric"},
         "section": {"type": "string"}, "key": {"type": "string"}, "status": {"type": "string"}, "area": {"type": "string"},
         "body": {"type": "string", "description": "longer note shown on demand"}, "ok": {"type": "boolean"}, "ms": {"type": "number"},
         "value": {"description": "item: a chart point or key number"}, "data": {"type": "object", "description": "item: a table row's cells"},
         "answer": {"type": "string", "description": "question: your answer to a question the person asked (send its id)"},
         "diagram": {"type": "object", "description": "topic: boxes and arrows the page draws: {nodes: [{id, label, note}], edges: [{from, to, label}]}"},
         "config": {"type": "object", "description": "proposal: the proposed goal, metric, sections and entries"},
         "scorer": {"type": "object", "description": "proposal: {\"source\": \"<Python scoring script>\"}"}}}},
    {"name": "next_task", "description": "Take the oldest queued task for this project and mark it running. Returns null when the queue is empty.",
     "inputSchema": {"type": "object", "required": ["project", "agent"], "properties": WHO}},
    {"name": "get_project", "description": "Read the project's goal, metric and sections, so you know what to report and where.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": {"project": {"type": "string"}}}},
    {"name": "define_section", "description": "Add or replace a section (a page) in the project. Views: " + " | ".join(f"{k}: {v}" for k, v in VIEWS.items()),
     "inputSchema": {"type": "object", "required": ["project", "id"], "properties": {
         "project": {"type": "string"}, "id": {"type": "string"}, "title": {"type": "string"}, "view": {"type": "string", "enum": list(VIEWS)},
         "statuses": {"type": "array", "items": {"type": "string"}}, "columns": {"type": "array", "items": {"type": "string"}},
         "icon": {"type": "string"}, "progress": {"type": "boolean"}}}},
    {"name": "get_runs", "description": "Your runs and what the verifier decided about each: pending, confirmed or not confirmed.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": {"project": {"type": "string"}, "agent": {"type": "string"}}}},
    {"name": "get_brief", "description": "Your full instructions for this project, with the current state. Read this first, and again whenever you lose track.",
     "inputSchema": {"type": "object", "required": ["project", "agent"], "properties": {"project": {"type": "string"}, "agent": {"type": "string"}}}},
]


P = {"project": {"type": "string"}}
COORDINATOR_TOOLS = [
    {"name": "list_projects", "description": "Every project on this HAUS server with its phase, goal and whether it is on hold. Start here.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "project_status", "description": "Where one project stands: confirmed results, tasks, live agents, what needs the person, open questions, latest findings.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": P}},
    {"name": "read_understanding", "description": "The project's Understanding page: what the target is and how it works, as written by agents and checked by the person.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": P}},
    {"name": "create_project", "description": "Start a new project in its setup phase. target is the person's own description of what to study and where it lives. "
     "A survey agent must then be started; its proposal has to be approved by the person on the page, which you cannot do.",
     "inputSchema": {"type": "object", "required": ["project", "target"], "properties": {
         **P, "target": {"type": "string"}, "goal": {"type": "string"}, "agent_files": {"type": "string", "description": "A folder whose contents every launched agent starts with"}}}},
    {"name": "add_task", "description": "Queue a task for the agents, in the person's words. The next free agent takes it.",
     "inputSchema": {"type": "object", "required": ["project", "text"], "properties": {**P, "text": {"type": "string"}}}},
    {"name": "relay_answer", "description": "Pass on the person's answer to a question an agent asked. Use only words the person actually gave you; never answer for them.",
     "inputSchema": {"type": "object", "required": ["project", "question_id", "answer"], "properties": {**P, "question_id": {"type": "string"}, "answer": {"type": "string"}}}},
    {"name": "set_run_settings", "description": "Pause or resume a project, cap how many agents work at once (0 for no limit), choose whether agents stop when "
     "something needs the person, and set how many hours they may keep running (0 for no limit).",
     "inputSchema": {"type": "object", "required": ["project"], "properties": {
         **P, "paused": {"type": "boolean"}, "max_agents": {"type": "integer"}, "stop_when_blocked": {"type": "boolean"}, "hours": {"type": "number"}}}},
    {"name": "list_launchers", "description": "The launch recipes this server allows, and the agents it has started.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "start_agents", "description": "Start agents on a project from a named launch recipe. This spends the person's model usage, so confirm the count and rounds with them first.",
     "inputSchema": {"type": "object", "required": ["project", "recipe"], "properties": {**P, "recipe": {"type": "string"}, "count": {"type": "integer"}, "rounds": {"type": "integer"}}}},
    {"name": "stop_agents", "description": "Stop one launched agent, or all launched agents on the project if no agent is named.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": {**P, "agent": {"type": "string"}}}},
]


def coordinate(name, args):
    """The coordinator's tools: what the person could do on the page, except approving a setup proposal."""
    if not any(t["name"] == name for t in COORDINATOR_TOOLS):
        raise ValueError(f"there is no tool named {name!r}. Approving a setup proposal is deliberately not available: the person does that on the page.")
    if name == "list_projects":
        return [{"project": p, "phase": config(p).get("phase", "run"), "goal": config(p).get("goal", ""), "on_hold": hold(p),
                 "needs_the_person": sum(1 for n in notable(p) if n["open"])} for p in project_ids()]
    if name == "list_launchers":
        return {"recipes": {k: v.get("label", k) for k, v in recipes().items()}, "launched": launched()}
    project = check_name(args.get("project"))
    if name == "create_project":
        return new_project(args)
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project; use list_projects")
    if name == "project_status":
        return status(project)
    if name == "read_understanding":
        return "\n\n".join(f'## {u["key"]}  [{u["label"]}]\n{u["body"]}' for u in understanding(list(load(project)))) or "Nothing has been written yet."
    if name == "add_task":
        return ingest({"project": project, "type": "task", "text": args.get("text"), "status": "queued", "source": "you"})
    if name == "relay_answer":
        if not str(args.get("answer") or "").strip():
            raise ValueError("answer is required")
        return ingest({"project": project, "type": "question", "id": args.get("question_id"), "answer": args["answer"], "source": "you"})
    if name == "set_run_settings":
        away = dict(config(project).get("away") or {})
        if "max_agents" in args:
            away.pop("max_agents", None) if not args["max_agents"] else away.update(max_agents=args["max_agents"])
        if "stop_when_blocked" in args:
            away["when_blocked"] = "pause" if args["stop_when_blocked"] else "continue"
        if "hours" in args:
            away.pop("until", None)
            if args["hours"]:
                away["until"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=float(args["hours"]))).isoformat(timespec="seconds")
        return set_project({"project": project, "away": away, **({"paused": bool(args["paused"])} if "paused" in args else {})})
    if name == "start_agents":
        return launch({"project": project, "recipe": args.get("recipe"), "count": args.get("count", 1), **({"rounds": args["rounds"]} if "rounds" in args else {})})
    if name == "stop_agents":
        return stop({"project": project, "agent": args.get("agent")})
    raise ValueError("unknown tool")


def coordinator_guide(base):
    """Everything a chat agent needs to run this server for the person, with nothing to install: it only needs to make HTTP
    requests. The person says "read <this address> and help me with my projects"."""
    call = f"curl -s -X POST {base}/api/coordinate -H 'Content-Type: application/json' -d"
    out = [f"You are the person's coordinator for HAUS, a server at {base} that runs and tracks AI agent research projects.",
           f"The person watches everything on the page at {base} ; you act for them by calling the tools below.",
           "",
           "HOW TO CALL A TOOL",
           f"  {call} '{{\"tool\": \"<name>\", \"args\": {{...}}}}'",
           "From another machine, add:  -H \"Authorization: Bearer $PROJECT_VIEW_COORDINATOR_TOKEN\"",
           "",
           "TOOLS"]
    for t in COORDINATOR_TOOLS:
        props = t["inputSchema"].get("properties", {})
        need = set(t["inputSchema"].get("required", []))
        args = ", ".join(f'{k}{"" if k in need else "?"}' for k in props) or "no arguments"
        out += [f'- {t["name"]} ({args})', f'    {t["description"]}']
    out += ["",
            "RULES",
            "- Start with list_projects, then project_status for the one the person means. Tell them what you see in plain words.",
            "- Starting agents spends the person's model usage. Say how many agents and rounds you are about to start, and wait for a yes.",
            "- You cannot approve a setup proposal. That saves and runs a script an agent wrote, so the person must read it and",
            f"  approve it themselves on the page ({base}). Tell them when one is waiting.",
            "- Never answer an agent's question for the person. Read it out, and use relay_answer only with the answer they give you.",
            "- A new project starts in setup: create_project, then start ONE agent to survey it, then wait for the person to approve.",
            "- After you change something, check project_status again and report what actually happened.",
            "",
            "RIGHT NOW"]
    out += [f'- {p["project"]}: {p["phase"]}' + (f', on hold ({p["on_hold"]})' if p["on_hold"] else "") + (f', {p["needs_the_person"]} waiting on the person' if p["needs_the_person"] else "")
            for p in coordinate("list_projects", {})] or ["- no projects yet"]
    r = recipes()
    out += ["Launch recipes: " + (", ".join(f"{k} ({v.get('label', k)})" for k, v in r.items()) if r else "none; launching is off until the person creates launchers.json")]
    return "\n".join(out) + "\n"


def mcp(msg, ip, verifier, base="", coordinator=False):
    method, params = msg.get("method"), msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "haus", "version": "0.3"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS + (COORDINATOR_TOOLS if coordinator else [])}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "report":
                out = ingest(args, ip, verifier)
            elif name == "next_task":
                out = claim(args, ip)
            elif name == "get_project":
                out = public_config(check_name(args.get("project")))
            elif name == "define_section":
                out = set_project({"project": args.get("project"), "section": {k: v for k, v in args.items() if k != "project"}})
            elif name == "get_runs":
                out = runs_for(args.get("project"), args.get("agent") or "")
            elif name == "get_brief":
                out = brief(args.get("project"), args.get("agent"), base)
            elif coordinator and any(t["name"] == name for t in COORDINATOR_TOOLS):
                out = coordinate(name, args)
            else:
                raise ValueError("unknown tool")
            result = {"content": [{"type": "text", "text": out if isinstance(out, str) else json.dumps(out)}]}
        except (ValueError, PermissionError) as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body=None, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def base(self):
        host = self.headers.get("Host", "")
        return "http://" + (host if re.fullmatch(r"[\w.\-\[\]:]{1,100}", host) else f"127.0.0.1:{PORT}")

    def role(self):
        if self.client_address[0] in LOCAL:
            return "verifier"
        given = self.headers.get("Authorization", "").encode()
        if VERIFIER_TOKEN and hmac.compare_digest(given, b"Bearer " + VERIFIER_TOKEN.encode()):
            return "verifier"
        if COORDINATOR_TOKEN and hmac.compare_digest(given, b"Bearer " + COORDINATOR_TOKEN.encode()):
            return "coordinator"
        if TOKEN and hmac.compare_digest(given, b"Bearer " + TOKEN.encode()):
            return "agent"
        return None

    def do_GET(self):
        url = urlparse(self.path)
        arg = lambda k: (parse_qs(url.query).get(k) or [""])[0]
        try:
            if url.path == "/":
                self.reply(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/projects":
                # epoch changes on every server start, so an open page knows to refetch instead of mixing two logs
                self.reply(200, [{"id": p, "config": public_config(p), "count": len(load(p)), "epoch": BOOT, "hold": hold(p), "notable": notable(p), "working": sorted(a for a, t in SLOTS.get(p, {}).items() if t > time.time()),
                                  "launched": launched(p), "recipes": {k: v.get("label", k) for k, v in recipes().items()}} for p in project_ids()])
            elif url.path == "/api/events":
                events, after = load(check_name(arg("project"))), max(0, int(arg("after") or 0))
                self.reply(200, {"events": events[after:after + 5000]})
            elif url.path == "/api/pending":
                self.reply(200, pending(arg("project")))
            elif url.path == "/api/project":
                self.reply(200, {**public_config(check_name(arg("project"))), "hold": hold(arg("project"))})
            elif url.path == "/mcp":
                self.reply(405, {"error": "this endpoint takes POST only"})
            elif url.path == "/api/coordinator":
                self.reply(200, coordinator_guide(self.base()).encode(), "text/plain; charset=utf-8")
            elif url.path == "/api/found":
                self.reply(200, found(arg("project")))
            elif url.path == "/api/runs":
                self.reply(200, runs_for(arg("project"), arg("agent")))
            elif url.path == "/api/brief":
                self.reply(200, brief(arg("project"), arg("agent")[:80], self.base()).encode(), "text/plain; charset=utf-8")
            else:
                self.reply(404, {"error": "not found"})
        except ValueError as e:
            self.reply(400, {"error": str(e)})
        except Exception as e:  # our bug, not the caller's: say so plainly instead of hanging up
            traceback.print_exc()
            self.reply(500, {"error": f"server error: {type(e).__name__}: {e}"})

    def do_POST(self):
        role, ip = self.role(), self.client_address[0]
        if not role:
            return self.reply(401, {"error": "missing or wrong token"})
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 200000:
                return self.reply(413, {"error": "request is larger than 200 kB"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            path = urlparse(self.path).path
            if path == "/api/events":
                out = ingest(data, ip, role == "verifier")
            elif path == "/api/claim":
                out = claim(data, ip)
            elif path == "/api/project":
                out = set_project(data)
            elif path == "/api/slot":
                out = slot(data)
            elif path == "/api/coordinate":  # the coordinator's tools for callers that do not speak MCP
                if ip not in LOCAL and role != "coordinator":
                    raise PermissionError("only this machine, or a caller with the coordinator token, may do this")
                out = coordinate(str(data.get("tool")), data.get("args") if isinstance(data.get("args"), dict) else {})
                out = {"result": out}
            elif path in ("/api/launch", "/api/stop"):  # starting and stopping agents: this machine, or the coordinator
                if ip not in LOCAL and role != "coordinator":
                    raise PermissionError("only the page on the server's own machine, or the coordinator, may do this")
                out = launch(data) if path == "/api/launch" else stop(data)
            elif path in ("/api/new", "/api/approve"):  # the person's own decisions: this machine only, whatever token is sent
                if ip not in LOCAL:
                    raise PermissionError("only the page on the server's own machine may do this")
                out = new_project(data) if path == "/api/new" else approve(data)
            elif path == "/mcp":
                out = mcp(data, ip, role == "verifier", self.base(), coordinator=ip in LOCAL or role == "coordinator")
                if out is None:
                    return self.reply(202, b"")
            else:
                return self.reply(404, {"error": "not found"})
        except PermissionError as e:
            return self.reply(403, {"error": str(e)})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self.reply(500, {"error": f"server error: {type(e).__name__}: {e}"})
        self.reply(200, out)

    def log_message(self, *args):
        pass


def serve(host="127.0.0.1"):
    if host not in LOCAL and not TOKEN:
        sys.exit("Refusing to listen beyond this machine without a token. Set PROJECT_VIEW_TOKEN first.")
    print(f"HAUS on http://{host}:{PORT}", flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


def init(project, goal):
    """Create a starter project.json to edit by hand."""
    path = PROJECTS / check_name(project) / "project.json"
    if path.exists():
        sys.exit(f"{path} already exists; edit it directly.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "goal": goal,
        "summary": "",
        "rules": "",
        "metric": {"name": "score", "direction": "higher", "aggregate": "max",
                   "artifact": "Describe here what an agent must attach to a run so it can be re-scored.",
                   "verify": ""},
        "sections": [{"id": "coverage", "title": "Coverage", "view": "grid", "statuses": ["untouched", "partial", "explored"], "progress": True}],
    }, indent=2) + "\n")
    print(f"Created {path}\n"
          "Next: set metric.aggregate to min, max or unique, and set metric.verify to a command that reads a run's artifact\n"
          "as JSON on stdin and prints {\"ok\": true, \"score\": 1.0, \"detail\": \"...\"}. Leave verify empty to run unverified.")


if __name__ == "__main__":
    if "--init" in sys.argv:
        i = sys.argv.index("--init")
        if len(sys.argv) < i + 3:
            sys.exit('Usage: python3 server.py --init <project> "<goal>"')
        init(sys.argv[i + 1], sys.argv[i + 2])
    else:
        serve(sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1")
