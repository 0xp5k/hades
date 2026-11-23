#!/usr/bin/env python3
"""Hades server: the live page, and the one port a research loop reports to. It listens on this machine only.

  python3 server.py                  serve the page at http://127.0.0.1:8765 (PROJECT_VIEW_PORT changes the port)
  python3 server.py --dashboard      the same, creating one empty project first if there are none

Each project is a folder under projects/ holding:
  project.json   goal, target, rules, direction, loop settings and gates
  events.jsonl   append-only log of everything reported

What the loop, the page and a chat agent call:
  GET  /api/status?project=X                  plain text: where the project stands. Read this first.
  GET  /api/brief?project=X&agent=NAME        plain text: what one round needs to know, generated fresh
  GET  /api/project?project=X                 the project's config, and "hold" when it is paused
  GET  /api/projects                          every project, its launched loop and the launch recipes
  GET  /api/events?project=X&after=N          the log
  GET  /api/research?project=X&q=TEXT&area=A  every frame and claim, with where it stands
  GET  /api/gates?project=X                   the claims waiting for a gate, and what to run on each
  POST /api/frames   {"project", "frames"}    queue places to look, many at once
  POST /api/events   {"project", "type", ...} report: activity, note (a round's closing handoff), question or research
  POST /api/project  {"project", ...}         set goal, rules, target, direction, paused or loop
  POST /api/new      {"project", "goal"}      create a project
  POST /api/launch   {"project", ...}         start the project's loop from a recipe in launchers.json
  POST /api/stop     {"project"}              stop it

Trust: every caller is on this machine and is trusted. Run it somewhere you would run the agents themselves.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
PROJECTS = ROOT / "projects"
PORT = int(os.environ.get("PROJECT_VIEW_PORT", 8765))
LOCK = threading.RLock()
NAME = re.compile(r"^\w[\w.-]{0,79}$")
TYPES = ["activity", "note", "question", "research"]  # note: what a round printed at its end, from its Handoff marker on
STRINGS = {"id": 80, "key": 200, "area": 200, "status": 40, "agent": 80,
           "harness": 80, "model": 60, "source": 20, "body": 30000, "answer": 6000, "known": 400, "origin": 20}
EVENTS = {}  # project id -> list of events, loaded from disk on first use
IDS = {}  # project id -> {(type, id)} already in its log, so accepting a report never has to scan the log
KEYED = ("question", "research")
RESEARCH_STATES = ("open", "supported", "contradicted", "superseded")  # a frame; a claim; ruled out; dropped
GATE_VERDICTS = ("passed", "failed", "known", "error")  # known: a refuter cited where it was already reported; error: the gate could not run, or ran out of time
GATE_TRIES = 3  # how many times a gate that could not run is run again before the claim is left there
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
                for line in f.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "item" and event.get("section") == "round-notes":
                        event["type"] = "note"  # how a round's note was kept before it had a type of its own
                    if isinstance(event, dict) and event.get("type") in TYPES:  # older logs hold kinds of report that no longer exist
                        out.append(event)
            EVENTS[project] = out
            IDS[project] = {(e.get("type"), e.get("id")) for e in out if e.get("type") in KEYED}
        return EVENTS[project]


def config(project):
    """Read from disk every time, so hand edits to project.json show up on the page at once."""
    try:
        cfg = json.loads((PROJECTS / project / "project.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def config_problem(project):
    """Why this project's project.json cannot be used, or None. While it cannot, the loop holds: running on an empty
    config would drop the goal and swap the project's gates for the built-in one."""
    path = PROJECTS / project / "project.json"
    if not path.exists():
        return None
    try:
        whole = isinstance(json.loads(path.read_text(encoding="utf-8-sig")), dict)
    except (OSError, ValueError) as e:
        return f"project.json cannot be read ({e}); fix it by hand"
    return None if whole else "project.json must hold one JSON object; fix it by hand"


def save_config(project, cfg):
    path = PROJECTS / project / "project.json"
    draft = path.with_name("project.json.new")
    draft.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(draft, path)  # whole or not at all: a reader never sees half a file


DEFAULT_GATES = [{"name": "refutation", "timeout_min": 20, "refute":
                  "Try to make this claim fail. (1) Make it happen again yourself, from its artifact or its description, in this folder: if you cannot, it is "
                  "refuted. (2) Hold it against the goal and rules as stated above: if it only answers an easier reading of them, it is refuted. (3) Only if "
                  "the goal asks for something new: look for an earlier report of this same result. That counts only if you name a source you opened in this "
                  "session (a file, a fetched page) or an exact identifier (a CVE, a DOI, a commit), and say what in it matches; a source you remember but did "
                  "not open is not evidence."}]


def gates_of(cfg):
    """The project's gates, in the order a claim must pass them. A gate is a command (exit code 0 passes), a refuter (a
    separate fresh round that sees only the claim and tries to kill it), or a rater (a fresh round that says how much the
    claim matters, 1 to 5; it never stops a claim, it orders what is established). A refuter or rater may name another
    launch recipe to run on, so the check need not come from the model that made the claim. A project that sets none gets the
    built-in refuter, so that nothing is taken on a round's word and nobody has to set anything up first; "gates": []
    turns gating off. Gates are set only by editing project.json. A command is a list of arguments, or one string split
    the way a POSIX shell would."""
    if "gates" not in cfg:
        return [dict(g) for g in DEFAULT_GATES]
    out, given = [], cfg.get("gates")
    for g in given[:8] if isinstance(given, list) else []:
        name = str(g.get("name") or "").strip() if isinstance(g, dict) else ""
        if not re.fullmatch(r"\w[\w -]{0,39}", name) or any(x["name"] == name for x in out):
            continue
        command = g.get("command")
        try:
            command = shlex.split(command) if isinstance(command, str) else command
        except ValueError:
            command = None
        runs = isinstance(command, list) and bool(command) and all(isinstance(x, str) and x for x in command)
        refute = g["refute"].strip()[:2000] if isinstance(g.get("refute"), str) else ""
        rate = g["rate"].strip()[:2000] if isinstance(g.get("rate"), str) else ""
        if not runs and not refute and not rate:
            continue
        limit = g.get("timeout_min")
        other = g.get("recipe") if not runs and isinstance(g.get("recipe"), str) and NAME.match(g["recipe"]) else None
        out.append({"name": name, **({"command": command} if runs else {"refute": refute} if refute else {"rate": rate}), **({"recipe": other} if other else {}),
                    "timeout_min": limit if isinstance(limit, (int, float)) and not isinstance(limit, bool) and 0 < limit <= 1440 else 10})
    return sorted(out, key=lambda g: "rate" in g)  # raters last, wherever they were written: they rate what the others let through


HASHED = {}  # locked file -> (its modification time and size, its hash), so a poll does not re-read a large locked corpus


def fingerprint(project, cfg, gates=None):
    """What the question is bound to: the gates as configured, and the files in the project's folder that the person
    locked (the exact question, the gate's own script). A verdict is bound to its own gate and the locked files (see
    sealed): change either and it is stale, and counts for nothing until that gate has run again."""
    gates = gates_of(cfg) if gates is None else gates
    if not gates:
        return ""
    seal, root = hashlib.sha256(json.dumps(gates, sort_keys=True).encode()), (PROJECTS / project).resolve()
    for rel in sorted({str(x) for x in cfg.get("lock", []) if isinstance(x, str)} if isinstance(cfg.get("lock"), list) else [])[:50]:
        path = (root / rel).resolve()
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())[:2000] if path.is_dir() else []
        seal.update(rel.encode())
        for f in files:
            real = f.resolve()
            if root not in real.parents:  # nothing outside the project's own folder, directly or through a link
                continue
            try:
                info = real.stat()
                stamp = (info.st_mtime_ns, info.st_size)
                if HASHED.get(real, (None, None))[0] != stamp:
                    HASHED[real] = (stamp, hashlib.sha256(real.read_bytes()).digest())
                seal.update(f.relative_to(root).as_posix().encode() + HASHED[real][1])
            except (OSError, ValueError):
                seal.update(b"unreadable")
    return seal.hexdigest()[:16]


def sealed(project, cfg):
    """The gates, each with the fingerprint its verdicts carry: that gate alone and the locked files. So adding a gate,
    or rewording one, leaves the other gates' verdicts standing."""
    return [{**g, "fp": fingerprint(project, cfg, [{k: v for k, v in g.items() if k != "timeout_min"}])} for g in gates_of(cfg)]  # a longer time limit asks the same question


def public_config(project):
    cfg = config(project)
    cfg["direction"] = cfg["direction"] if isinstance(cfg.get("direction"), str) else ""
    cfg["loop"] = clean_loop(cfg.get("loop"), strict=False)
    cfg["problem"] = config_problem(project)
    cfg["default_gates"] = "gates" not in cfg  # the built-in refuter is in force: the project set no gates of its own
    cfg["gates"], cfg["fingerprint"] = sealed(project, cfg), fingerprint(project, cfg)
    cfg["artifact"] = str(cfg.get("artifact") or "").strip()[:300]
    cfg["lock"] = [str(x)[:200] for x in cfg["lock"][:50] if isinstance(x, str)] if isinstance(cfg.get("lock"), list) else []
    return cfg


def project_ids():
    on_disk = {p.name for p in PROJECTS.iterdir() if p.is_dir()} if PROJECTS.exists() else set()
    with LOCK:  # deleting a project's folder removes the project; forget what was cached for it
        for gone in set(EVENTS) - on_disk:
            del EVENTS[gone]
            IDS.pop(gone, None)
    return sorted(on_disk)


def ingest(data):
    project, type = check_name(data.get("project")), data.get("type")
    if not (PROJECTS / project).is_dir():  # a mistyped name must not quietly start a second project
        raise ValueError("no such project")
    if type not in TYPES:
        raise ValueError("type must be one of: " + ", ".join(TYPES))
    event = {"type": type, "text": str(data.get("text") or "").strip()[:2000]}
    for k, limit in STRINGS.items():
        if data.get(k) is not None:
            event[k] = str(data[k])[:limit]
    if data.get("data") is not None:  # a table row's cells, or the loop's own details about a round
        try:
            if not isinstance(data["data"], dict) or len(json.dumps(data["data"], allow_nan=False)) > 4000:
                raise ValueError
        except ValueError:
            raise ValueError("data must be a JSON object under 4 kB")
        if not isinstance(data["data"].get("frame", ""), str) or not isinstance(data["data"].get("cwd", ""), str):
            raise ValueError("data.frame and data.cwd must be text")
        event["data"] = data["data"]

    if type == "note":
        if not event.get("key") or not event.get("body"):
            raise ValueError("a note needs a key (which round) and a body (what the round printed at its end)")
    elif type == "research" and data.get("source") == "gate":  # one gate's verdict on what a thread claims; nothing else about it changes
        cfg = config(project)
        if not event.get("id") or data.get("verdict") not in GATE_VERDICTS or not any(g["name"] == data.get("gate") for g in gates_of(cfg)):
            raise ValueError("a gate verdict needs the claim's id, one of the project's gates by name, and verdict one of: " + ", ".join(GATE_VERDICTS))
        # Stamped with what it ran under, so it stops counting when the gates or the locked files change. One that ran
        # under an earlier version (they changed while it ran) is refused: the gate is still due, and runs again.
        seal = next(g["fp"] for g in sealed(project, cfg) if g["name"] == data["gate"])
        if data.get("fp") is not None and data["fp"] != seal:
            raise ValueError("the gates or the locked files changed while this gate ran; it is still due and will run again")
        event = {"type": type, "id": event["id"], "gate": data["gate"], "verdict": data["verdict"], "detail": str(data.get("detail") or "")[-4000:],
                 "checked": str(data.get("checked") or "")[:300], "fp": seal, "source": "gate",
                 **({"rating": data["rating"]} if data.get("rating") in (1, 2, 3, 4, 5) and not isinstance(data.get("rating"), bool) else {})}
    elif type == "research" and data.get("source") == "you" and any(k in data for k in ("priority", "status", "score", "audited")) and "text" not in data:
        # the person's mark alone (first in the queue, dropped, reopened, a score, their audit): the claim and its history stay as they were
        if not event.get("id") or any(k in data and not isinstance(data[k], bool) for k in ("priority", "audited")) \
                or ("status" in data and data["status"] not in ("open", "superseded", "contradicted")) \
                or ("score" in data and (isinstance(data["score"], bool) or data["score"] not in (1, 2, 3, 4, 5))):
            raise ValueError("a change to a frame or claim needs its id, and priority or audited true or false, score 1 to 5, or status open (reopen), superseded (drop) or contradicted (rule out)")
        event = {"type": type, "id": event["id"], **{k: data[k] for k in ("priority", "status", "score", "audited") if k in data}, "source": "you"}
        if data.get("audited"):  # their audit is of this artifact under these gates; it lapses when either changes
            seen = next((n for n in research(project) if n["id"] == event["id"]), {})
            event.update(audit_fp=fingerprint(project, config(project)), audit_artifact=seen.get("artifact") or "")
    elif type == "research":
        if "verdict" in data or "gate" in data:
            raise ValueError("a gate's verdict is recorded by the loop when it runs the gate; it is not something to report")
        if data.get("source") == "you" and data.get("priority") is not None:  # an agent copying the mark back changes nothing
            if not isinstance(data["priority"], bool):
                raise ValueError("priority must be true or false")
            event["priority"] = data["priority"]
        if data.get("status") not in RESEARCH_STATES:
            raise ValueError("research status must be one of: " + ", ".join(RESEARCH_STATES))
        if not event["text"]:
            raise ValueError("research needs a short claim in text")
        if data.get("score") is not None:  # how many rounds a frame is worth
            if data["score"] not in (1, 2, 3, 4, 5) or isinstance(data["score"], bool):
                raise ValueError("score must be a whole number from 1 to 5")
            event["score"] = data["score"]
        if data.get("artifact") is not None:  # what the gates are run on; naming one asks for them again
            event["artifact"] = " ".join(str(data["artifact"]).split())[:300]
        for field, limit in (("found_in", 300), ("folder", 500)):  # the frame a claim came from; the folder its artifact is in
            if data.get(field) is not None:
                event[field] = str(data[field])[:limit]
        for field in ("next", "conditions"):
            if field in data:
                event[field] = str(data[field] or "")[:2000]
    elif type == "activity" and not event["text"]:
        raise ValueError("text is required")

    with LOCK:
        events = load(project)
        if type in KEYED:
            if "id" not in event:
                n = len(events) + 1
                while (type, f"{'k' if type == 'research' else 'q'}{n}") in IDS[project]:
                    n += 1
                event["id"] = f"{'k' if type == 'research' else 'q'}{n}"
            known = (type, event["id"]) in IDS[project]
            if not known and ("text" not in event or (not event["text"] and data.get("id") is not None)):
                raise ValueError(f'no {"frame or claim" if type == "research" else "assumption"} has the id {str(data.get("id"))[:80]}')
            if not known and not event["text"]:
                raise ValueError("text is required")
        event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **event}
        with open(PROJECTS / project / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        events.append(event)
        if type in KEYED:
            IDS[project].add((type, event["id"]))
    if type == "note":
        from_handoff(project, event)  # the agent only writes its handoff; the record is kept from it here
    return event


HANDOFF = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:#+[ \t]*)?(?:\*\*|__)?[ \t]*handoff[ \t]*(?:\*\*|__)?[ \t]*(?::|$)", re.I | re.M)
LABEL = re.compile(r"^\s*(?:[-*]\s+)?(?:#+\s*)?(?:\*\*|__)?\s*(handoff|results?|ruled[ -]out|decisions?(?:\s+needed)?|assum(?:ed|ptions?)|leads?|next)"
                   r"\s*(?:\*\*|__)?\s*(?::\s*(?:\*\*|__)?\s*(.*))?$", re.I)
BULLET = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
TAG = re.compile(r"\s*\[known(?::\s*([^\]]*))?\]\s*$", re.I)  # the round's own word that a result is already known, with where
CHECK_TAG = re.compile(r"\s*\[check:\s*([^\]]+)\]", re.I)
SCORE_TAG = re.compile(r"\s*\[score:\s*([1-5])\s*\]", re.I)


def plain(text):
    """Text as compared for duplicates: lower case, one space, no trailing punctuation."""
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(".!?,;: ")


def handoff_items(block):
    """{"results": [...], "ruled out": [...], "decision": [...], "leads": [...], "next": [...]} from a handoff, forgiving
    about bold, bullets, headings, plurals and "none". Lines come back whole, tags and all; the caller reads the tags."""
    found, label = {"results": [], "ruled out": [], "decision": [], "leads": [], "next": []}, None
    for line in block.splitlines():
        m = LABEL.match(line)
        if m:
            name = m.group(1).lower().replace("-", " ")
            label = "results" if name.startswith("result") else "decision" if name.startswith(("decision", "assum")) else "leads" if name.startswith("lead") \
                else name if name in ("ruled out", "next") else None
            line = m.group(2) or ""
        if label is None or not line.strip():
            continue
        items, bullet = found[label], BULLET.match(line)
        text = BULLET.sub("", line).replace("**", "").strip()
        if items and not bullet and not m and line[:1].isspace():  # an indented line carries on the item above it
            items[-1] += " " + text
        else:
            items.append(text)
    return {k: [x[:2000] for x in v if plain(x) not in ("", "none", "nothing", "n/a", "-")][:8] for k, v in found.items()}


def from_handoff(project, note):
    """Keep what a round reported in its handoff: a thread per result, ruled-out approach and lead, and each thing it assumed in the person's place. A round note without a Handoff marker records
    nothing, and says so in the log."""
    body = note.get("body") or note.get("text") or ""
    # A harness that echoes its prompt prints the template too ("Handoff: <what you did ...>"); that is not a handoff.
    marks = [m for m in HANDOFF.finditer(body) if not body[m.end():m.end() + 3].lstrip().startswith("<")]
    agent, data = note.get("agent"), note.get("data") if isinstance(note.get("data"), dict) else {}
    if not marks:
        ingest({"project": project, "type": "activity", "agent": agent, "text": "A round ended without a Handoff block: nothing was recorded from it"})
        return
    block = body[marks[-1].start():].strip()
    seen = {plain(n.get("text") or ""): n["id"] for n in research(project)}
    parts = handoff_items(block)
    asked = {plain(q.get("text") or "") for q in merged(load(project), "question").values() if not q.get("answer")}
    for line in parts["decision"]:  # what the round assumed in the person's place: in force until they say otherwise
        if plain(line) not in asked:
            asked.add(plain(line))
            ingest({"project": project, "type": "question", "text": line[:2000], "agent": agent, "origin": "handoff"})
    # Whatever is still work can be settled by name: an open frame, a claim a gate stopped (the round may claim it again
    # with a repaired artifact, or rule it out), and a claim the person marked.
    waiting = {n["id"]: n for n in research(project) if n["frame"] or n["state"] in ("error", "waiting", "unchecked") or (n["state"] == "failed" and n["gates"][n["at"]].get("verdict") != "known")}
    where = {"folder": data["cwd"]} if isinstance(data.get("cwd"), str) and data["cwd"] else {}  # where the round's artifacts are
    for section, status_ in (("results", "supported"), ("ruled out", "contradicted"), ("leads", "open")):
        for line in parts[section]:
            label, status = section, status_
            # Tags first, in any order, and only then the length limit, so a long line never loses what it named.
            named_artifact, worth = CHECK_TAG.search(line), SCORE_TAG.search(line)
            line = SCORE_TAG.sub("", CHECK_TAG.sub("", line)).strip()
            artifact = {"artifact": named_artifact.group(1).strip()} if label == "results" and named_artifact and named_artifact.group(1).strip() else {}
            tag = TAG.search(line) if section == "results" else None
            text = (line[:tag.start()].strip() if tag else line)[:400]
            if not text:
                continue
            extra = {"known": (tag.group(1) or "").strip() or "known"} if tag else {}
            extra.update({"score": min(4, int(worth.group(1)))} if worth and label == "leads" else {})  # 5 is the person's to give
            if "known" in extra:  # the round says it is already known: that is not a result, and no gate needs to say so
                label, status = "ruled out", "contradicted"
                text, artifact, extra = text, {}, {"conditions": "already known: " + extra["known"]}
            # A line that starts with a waiting thread's id, as [id], settles that thread in place, so it leaves the work
            # list: a result makes it a claim, a ruled-out line closes it with the reason. An id elsewhere in the line is prose.
            first = re.match(r"\s*\[([\w.-]+)\]\s*:?\s*", text) if label != "leads" else None
            named = waiting.pop(first.group(1), None) if first else None
            if named:
                said = text[first.end():].strip() or text
                if label == "ruled out":
                    ingest({"project": project, "type": "research", "id": named["id"], "text": named.get("text", ""), "agent": agent, "status": "contradicted", "conditions": (extra.get("conditions", "") + " " + said).strip()[:2000]})
                    continue
                # Claimed, or claimed again: every gate is still to come, on the artifact it names now (or the one it had).
                # A frame was a place to look, so the claim's own words replace it; a claim keeps its words and gains a note.
                was_frame = named.get("status") == "open"
                ingest({"project": project, "type": "research", "id": named["id"], "agent": agent, "status": "supported", **extra, **where,
                        "artifact": artifact.get("artifact") or named.get("artifact") or "",
                        **({"text": said, "found_in": named.get("text", "")[:300]} if was_frame else {"text": named.get("text", ""), "body": said})})
                continue
            if plain(text) in seen:
                same = next((n for n in research(project) if n["id"] == seen[plain(text)]), None) if artifact else None
                if same and same.get("status") not in ("contradicted", "superseded"):  # the same claim, now with something to check
                    ingest({"project": project, "type": "research", "id": same["id"], "text": same.get("text", ""), "agent": agent,
                            "status": same.get("status") or status, **artifact, **where})
                continue
            made = ingest({"project": project, "type": "research", "status": status, "text": text, "agent": agent, "origin": "handoff",
                           **extra, **artifact, **(where if label == "results" else {})})
            seen[plain(text)] = made["id"]
    # The round was given one frame. If it ends with the frame still open, its Next line is where the next round on that
    # frame starts, whoever runs it.
    given = data.get("frame") if isinstance(data.get("frame"), str) else None
    still = next((n for n in research(project) if n["id"] == given and n["frame"]), None) if given and parts["next"] else None
    if still and plain(parts["next"][0]) != plain(still.get("next") or ""):
        ingest({"project": project, "type": "research", "id": still["id"], "text": still.get("text", ""), "agent": agent, "status": still.get("status") or "open", "next": parts["next"][0][:2000]})


def add_frames(data):
    """Queue places to look, many at once: {"project", "frames": ["text", {"text", "score", "area", "next"}, ...]}.
    Each becomes an open thread the loop will take in turn. One that repeats an existing thread is skipped."""
    project, given = check_name(data.get("project")), data.get("frames")
    if not isinstance(given, list) or not 0 < len(given) <= 500:
        raise ValueError("frames must be a list of 1 to 500 entries")
    seen, added = {plain(n.get("text") or "") for n in research(project)}, []
    for one in given:
        one = {"text": one} if isinstance(one, str) else one
        if not isinstance(one, dict) or not str(one.get("text") or "").strip():
            raise ValueError("each frame needs text: the place to look, in a line")
        if plain(str(one["text"])) in seen:
            continue
        seen.add(plain(str(one["text"])))
        added.append(ingest({"project": project, "type": "research", "status": "open", "origin": "queue", "agent": data.get("agent"),
                             **{k: one[k] for k in ("text", "score", "area", "next") if one.get(k) is not None}})["id"])
    return {"added": added, "skipped": len(given) - len(added)}


def working(project):
    """The agents with a loop running on this project now."""
    with LOCK:
        mine = {a for (p, a), r in RUNNING.items() if p == project and r["proc"].poll() is None}
    return mine | {a for a in worked(project) if a not in mine and orphan(project, a)}


def gates_pending(project, agent=""):
    """What the loop should run next: for each live claim, the first gate it has no current verdict from, the person's
    marks first, then the claim with the most gates behind it, then the oldest. A claim a gate stopped waits there until it is claimed again.
    With several loops side by side, each runs the gates on its own agent's claims, so no gate is run twice."""
    live = working(project)
    others, first = live - {agent}, min(live | {agent})  # a claim whose own loop is gone falls to one loop, not to all of them
    cfg = config(check_name(project))
    gates, root = {g["name"]: g for g in sealed(project, cfg)}, str((PROJECTS / project).resolve())
    threads = research(project)
    settled = [n for n in threads if n["state"] == "established"]
    due = [n for n in threads if (n["state"] == "waiting" or n.get("rate")) and n.get("status") not in ("contradicted", "superseded") and n.get("agent") not in others
           and (n.get("agent") == agent or n.get("agent") in live or agent == first)]
    out = []
    # Nearest to established first: finishing one claim is worth more than starting on three. Rating comes after all of it.
    for n in sorted(due, key=lambda n: (bool(n.get("rate")), -n["priority"], -n["standing"], n["born"])):
        gate = gates[n["at"] or n["rate"]]
        out.append({"id": n["id"], "artifact": n.get("artifact") or "", "text": n.get("text", ""), "gate": gate["name"], "timeout_min": gate["timeout_min"],
                    "fp": gate["fp"], "cwd": n.get("folder") or "",  # the question it runs under, and the folder its artifact was left in
                    **({"command": [part.replace("{project}", root) for part in gate["command"]]} if "command" in gate else
                       {"brief": refuter_brief(cfg, n, gate, [x for x in settled if x is not n]) if "refute" in gate else rater_brief(cfg, n, gate),
                        "kind": "refute" if "refute" in gate else "rate", **other_harness(gate)})})
    return {"pending": out}


def refuter_brief(cfg, n, gate, settled=()):
    """What a refuter round is given: the claim, the question it answers, what already stands, and nothing about how it was reached."""
    return "\n".join([
        "You are checking one claim from another round of this research. You have not seen how it was reached. Do not trust it.",
        *[f"{k.upper()}: {cfg[k]}" for k in ("target", "goal", "rules") if isinstance(cfg.get(k), str) and cfg[k].strip()],
        "",
        f'THE CLAIM [{n["id"]}]: {n.get("text", "")}',
        *([n["body"][:1500]] if n.get("body") else []),
        *([f'Its artifact, in your working folder: {n["artifact"]}'] if n.get("artifact") else ["It named no artifact. Work from the claim itself."]),
        *(["", "ALREADY ESTABLISHED (if the claim only repeats one of these in other words, it is refuted as a duplicate)",
           *[f'- [{x["id"]}] {x.get("text", "")[:200]}' for x in settled[:20]]] if settled else []),
        "",
        f'YOUR JOB ({gate["name"]}): {gate["refute"]}',
        "Try to kill the claim with something concrete: a counterexample, a run that fails, an earlier source you opened and can cite.",
        "An opinion is not a refutation, and neither is a reason it might be wrong. If you cannot kill it, say what you tried.",
        "Do not read loop.log or any notes in this folder about how the claim was reached, and change nothing here.",
        "Put the decisive evidence (the command, its output, the citation) in the last six lines before your verdict: they are all the next round will see.",
        "End with exactly one of these lines:",
        "Verdict: refuted - <the concrete evidence>",
        "Verdict: known - <the earlier source you opened, and what in it matches>",
        "Verdict: stands - <what you tried and where you looked>"]) + "\n"


def other_harness(gate):
    """The command of the recipe a gate asks to run on, or why it cannot: {"harness": [...]}, {"problem": "..."} or {}."""
    if "recipe" not in gate:
        return {}
    recipe = recipes().get(gate["recipe"])
    return {"harness": resolve_program(recipe["command"])} if recipe else {"problem": f'the gate names the recipe "{gate["recipe"]}", which is not in launchers.json'}


def rater_brief(cfg, n, gate):
    """What a rater round is given: an established claim, and the job of saying how much it matters."""
    return "\n".join([
        "You are rating one result from this research. It has already been checked and stands; do not re-check it. Say how much it matters.",
        *[f"{k.upper()}: {cfg[k]}" for k in ("target", "goal", "rules") if isinstance(cfg.get(k), str) and cfg[k].strip()],
        "",
        f'THE RESULT [{n["id"]}]: {n.get("text", "")}',
        *([n["body"][:1500]] if n.get("body") else []),
        *([f'Its artifact, in your working folder: {n["artifact"]}'] if n.get("artifact") else []),
        "",
        f'YOUR JOB ({gate["name"]}): {gate["rate"]}',
        "Whoever found a result overrates it, so start from the case against: who would be affected, how often, and what they would do differently for knowing it.",
        "Rate what was shown, not what it might lead to. 5: it changes what people in the field do or believe. 4: a clear advance that others will use. "
        "3: a real result worth reporting, of modest consequence. 2: correct and minor. 1: correct and of no practical consequence.",
        "Do not read loop.log or any notes in this folder, and change nothing here. End with exactly one line:",
        "Rating: <1 to 5> - <the reason, in a sentence>"]) + "\n"


def assess(n, gates):
    """Where a claim stands against the gates: how many it is through in order ("standing"), and what is stopping it.
    A rater stops nothing: a claim through every other gate is established, and "rate" names the rater still due on it."""
    if not gates or n.get("status") != "supported":  # a frame or a dead end is not a claim
        return {"standing": 0, "state": None, "at": None, "rate": None}
    for count, gate in enumerate(gates):
        got = (n.get("gates") or {}).get(gate["name"])
        again = not got or got.get("fp") != gate["fp"] or (got.get("checked") or "") != (n.get("artifact") or "")  # never run, or run on a different question or artifact
        again = again or got.get("verdict") == "error" and (got.get("tries") or 1) < GATE_TRIES  # a timeout or a harness hiccup is not the claim's fault: it runs again
        if "rate" in gate:
            if again:
                return {"standing": count, "state": "established", "at": None, "rate": gate["name"]}
            continue
        if "command" in gate and not n.get("artifact"):  # a command has to be run on something; a refuter can work from the claim
            return {"standing": count, "state": "unchecked", "at": gate["name"], "rate": None}
        if again:
            return {"standing": count, "state": "waiting", "at": gate["name"], "rate": None}
        if got.get("verdict") != "passed":
            return {"standing": count, "state": "failed" if got.get("verdict") in ("failed", "known") else "error", "at": gate["name"], "rate": None}
    return {"standing": len(gates), "state": "established", "at": None, "rate": None}


def queue(claims):
    """The frames the loop will take, in order: the person's marks first, then the highest score, then the oldest.
    A frame whose rounds are spent is not in it."""
    return sorted((n for n in claims if n["frame"] and not n["stalled"]), key=lambda n: (-n["priority"], -n["budget"], n["born"]))


def merged(events, type):
    """Latest state of each thing of this type, keyed by id (or by key). "_i" is where it last changed."""
    out = {}
    for i, e in enumerate(events):
        if e["type"] == type:
            k = e.get("id") or e.get("key") or f"_{i}"
            # "asker" is whoever opened it: a later answer from the other side must not change whose question it is
            out[k] = {**out.get(k, {}), **e, "_i": i, "asker": out.get(k, {}).get("asker") or ("you" if e.get("source") == "you" else "agent")}
    return out


THREAD_KEYS = ("ts", "agent", "source", "text", "status", "area", "body", "conditions", "next", "priority",
               "known", "origin", "artifact", "score", "audited", "gate", "verdict", "detail", "checked", "found_in")
VERDICT_KEYS = ("verdict", "detail", "checked", "fp", "ts", "rating")
MARK_KEYS = ("priority", "status", "score", "audited", "audit_fp", "audit_artifact")
STARTED = re.compile(r"(?:Round|Iteration) \d+ started")


def research(project, query="", area=""):
    """Current research threads with their attributed update history. A thread is a frame (a place to look) until a round
    settles it; then it is a claim, or ruled out. Each comes back with where it stands: against the gates ("standing",
    "state", "at"), as work ("frame", "attempts", "budget", "stalled"), and whether the person's audit still holds."""
    cfg = config(check_name(project))
    gates, seal = sealed(project, cfg), fingerprint(project, cfg)
    nodes, attempts = {}, {}
    for i, e in enumerate(load(project)):
        if e["type"] == "activity" and isinstance(e.get("data"), dict) and isinstance(e["data"].get("frame"), str) and STARTED.match(e.get("text") or ""):
            # A round is spent on its frame when it starts, so one that crashes, times out or prints nothing still counts.
            attempts[e["data"]["frame"]] = attempts.get(e["data"]["frame"], 0) + 1
        if e["type"] == "research" and e.get("id"):
            old = nodes.get(e["id"], {})
            update = {k: e[k] for k in THREAD_KEYS if k in e}
            if e.get("source") == "gate":  # a gate's verdict changes only that
                if old:
                    before = old.get("gates", {}).get(e.get("gate")) or {}
                    again = before.get("verdict") == "error" and before.get("fp") == e.get("fp") and before.get("checked") == e.get("checked")
                    nodes[e["id"]] = {**old, "history": [*old["history"], update], "gates": {**old.get("gates", {}), e.get("gate"): {
                        **{k: e.get(k) for k in VERDICT_KEYS}, "tries": (before.get("tries", 1) if again else 0) + 1 if e.get("verdict") == "error" else 0}}}
                continue
            if e.get("source") == "you" and "text" not in e:  # the person's mark or status change alone: it changes only that, and does not make the thread look recently worked on
                if old:
                    nodes[e["id"]] = {**old, **{k: e[k] for k in MARK_KEYS if k in e}, "history": [*old["history"], update]}
                    if e.get("priority") is True:
                        attempts[e["id"]] = 0  # the person's mark puts it at the front with its rounds to spend again
                continue
            # Claimed again with another artifact, reworded, or claimed again after a gate said no: a different claim, so every gate
            # and the audit are still to come. Saying the same thing about the same artifact changes nothing.
            fresh = ("artifact" in e and ((e["artifact"] or "") != (old.get("artifact") or "") or any(g.get("verdict") != "passed" for g in old.get("gates", {}).values()))
                     or bool(e.get("text") and old.get("text") and plain(e["text"]) != plain(old["text"])))
            kept = {k: v for k, v in old.items() if k not in ("gates", "audited", "audit_fp", "audit_artifact")} if fresh else old
            nodes[e["id"]] = {**kept, **e, "_i": i, "born": old.get("born", i), "history": [*old.get("history", []), update]}
    out = list(nodes.values())
    for n in out:
        n["priority"] = bool(n.get("priority"))
        n.update(assess(n, gates))
        n["audited"] = bool(n.get("audited")) and n.get("audit_fp") == seal and n.get("audit_artifact", "") == (n.get("artifact") or "")
        n["attempts"], n["budget"] = attempts.get(n["id"], 0), n["score"] if isinstance(n.get("score"), int) else 3
        rated = [{**((n.get("gates") or {}).get(g["name"]) or {}), "of": g["fp"]} for g in gates if "rate" in g]  # how much an established claim matters, by the rater's word
        n["rating"] = next((r["rating"] for r in rated if r.get("fp") == r["of"] and (r.get("checked") or "") == (n.get("artifact") or "") and r.get("rating")), None) if n["state"] == "established" else None
        settled = n.get("status") in ("contradicted", "superseded")
        known = n["state"] == "failed" and ((n.get("gates") or {}).get(n["at"]) or {}).get("verdict") == "known"  # nothing a round can repair
        # A claim a gate stopped is not queued: in the real runs every round sent back to one only agreed with the gate. It stays
        # in the brief, where any round may repair it; the person's mark is what gives it a round of its own.
        n["frame"] = not settled and (n.get("status") == "open" or (n["priority"] and (n["state"] == "unchecked" or (n["state"] == "failed" and not known))))
        n["stalled"] = n["frame"] and n["attempts"] >= n["budget"]
        for g in gates:  # whether each verdict still counts, so nothing that shows the record has to compare fingerprints
            got = (n.get("gates") or {}).get(g["name"])
            if got:
                got["live"] = got.get("fp") == g["fp"] and (got.get("checked") or "") == (n.get("artifact") or "")
        # The one group each thread is in, said once, here.
        n["bucket"] = ({"established": "established", "failed": "stopped", "error": "stopped", "waiting": "waiting"}.get(n["state"], "unchecked") if n.get("status") == "supported"
                       else "ruled" if n.get("status") == "contradicted" else "dropped" if settled else "aside" if n["stalled"] else "queued")
    order = {n["id"]: i + 1 for i, n in enumerate(queue(out))}
    on = rounds_on(project)
    for n in out:
        n["place"], n["working"] = order.get(n["id"]), on.get(n["id"])
    if area:
        out = [n for n in out if n.get("area") == area]
    if query:
        q = query.lower()
        out = [n for n in out if q in " ".join(str(n.get(k) or "") for k in ("id", "text", "body", "conditions", "next", "area")).lower()]
    out.sort(key=lambda n: (n["priority"], n["_i"]), reverse=True)  # what the person marked first, then the most recently worked on
    for n in out:
        n.pop("_i", None)
    return out


def thread_lines(n):
    """One frame as a fresh round should see it: what it is, and its next test."""
    return [f'- [{n["id"]}] {n.get("text", "")}' + (f' (area: {n["area"]})' if n.get("area") else ""), *([f'  Next test: {n["next"][:300]}'] if n.get("next") else [])]


def steering(cfg, events):
    """What the person has said that every round must follow: their standing direction."""
    out, direction = [], cfg.get("direction")
    if isinstance(direction, str) and direction.strip():
        out += ["THE PERSON'S CURRENT DIRECTION (it outranks your own choice of next step; follow it until they change it)", direction.strip(), ""]
    return out


def hold(project):
    """Why the loop should not work on this project right now, or None if it should. loop.py asks before every round."""
    return config_problem(project) or ("paused by the person" if config(project).get("paused") else None)


# ---------- launching agents from the server ----------
# Off unless launchers.json exists. That file lists named recipes, each an exact harness command, and is only ever
# written by hand. The page can start and stop those recipes; it can never supply a command.

LAUNCH_FILE = ROOT / "launchers.json"
RUNNING = {}  # (project, agent) -> {"proc", "recipe", "started", "ended"}: one live loop per project
MAX_RUNNING = 8  # across every project


def launcher_file():
    """(usable recipes, a plain sentence on what in launchers.json could not be used or ""). Read on every call, so an edit to
    the harness takes effect at the next launch without a restart."""
    try:
        data = json.loads(LAUNCH_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as e:
        return {}, f"launchers.json could not be read: {e}"
    if not isinstance(data, dict):
        return {}, "launchers.json must be a JSON object that maps recipe names to recipes."
    ok = lambda v: isinstance(v, dict) and isinstance(v.get("command"), list) and v["command"] and all(isinstance(x, str) for x in v["command"])
    good = {k: v for k, v in data.items() if NAME.match(str(k)) and ok(v)}
    dropped = [str(k)[:40] for k in data if k not in good]
    return good, ("Skipped in launchers.json (each recipe needs a simple name and a non-empty \"command\" list of strings): " + ", ".join(dropped) + ".") if dropped else ""


def recipes():
    return launcher_file()[0]


MODEL = re.compile(r"^[\w.:\-\[\]/@]{1,80}$")
is_claude = lambda command: Path(command[0]).name.lower() in ("claude", "claude.exe", "claude.cmd")


def model_flag(command):
    """Where the model is named in a command: (index of the value, "--model=" if it is joined, else ""), or None."""
    for i, x in enumerate(command):
        if x.startswith("--model="):
            return i, "--model="
        # "python3 -m module" names a module, not a model
        if (x == "--model" or (x == "-m" and not (i == 1 and Path(command[0]).name.lower().startswith("python")))) and i + 1 < len(command):
            return i + 1, ""
    return None


LOOP_DEFAULTS = {"rounds": 4, "timeout_min": 0, "keep_background": False, "workers": 1, "spend": 0}  # research: no time limit
LOOP_RULES = {  # what each loop setting may be, and how to say so when it is not
    "recipe": (lambda v: isinstance(v, str) and bool(NAME.match(v)), "a recipe name from launchers.json"),
    "model": (lambda v: isinstance(v, str) and bool(MODEL.match(v)), "1 to 80 letters, digits or . : - [ ] / @"),
    "rounds": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 500, "a whole number from 0 (until stopped) to 500"),
    "timeout_min": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1440, "minutes from 0 (no limit) to 1440"),
    "keep_background": (lambda v: isinstance(v, bool), "true or false"),
    "workers": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 4, "a whole number from 1 to 4: how many rounds run side by side, each on its own frame and in its own folder"),
    "spend": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000, "a whole number from 0 (no limit) to 10000: the most model rounds one launch may use, research and checking together"),
}


def clean_loop(given, strict=True, keys=LOOP_RULES):
    """The loop settings that are valid. Unknown keys and nulls are dropped; an invalid value is an error when strict
    (what the person just sent) and skipped otherwise (a hand-edited file or a recipe's own fields)."""
    if given is None:
        return {}
    if not isinstance(given, dict):
        if strict:
            raise ValueError("loop must be an object")
        return {}
    out = {}
    for k in keys:
        if given.get(k) is None:
            continue
        ok, need = LOOP_RULES[k]
        if ok(given[k]):
            out[k] = given[k]
        elif strict:
            raise ValueError(f"loop.{k} must be {need}")
    return out


def recipe_defaults(recipe):
    """A recipe's own rounds, time limit and background setting, with Hades's defaults where it sets none."""
    return {**LOOP_DEFAULTS, **clean_loop(recipe, strict=False, keys=tuple(LOOP_DEFAULTS))}


def recipe_model(command):
    at = model_flag(command)
    return command[at[0]][len(at[1]):] if at else ""


def with_model(command, model):
    """The recipe's command with the person's choice of model for this launch only."""
    if not isinstance(model, str) or not MODEL.match(model):
        raise ValueError("model must be 1 to 80 letters, digits or . : - [ ] / @")
    out, at = list(command), model_flag(command)
    if at:
        out[at[0]] = at[1] + model
        return out
    if is_claude(out):
        return out[:1] + ["--model", model] + out[1:]
    raise ValueError("This recipe does not name a model. Add a model flag (for example --model) to its command in launchers.json, then choose the model again.")


def launchers():
    """Each recipe as the page offers it: label, its own model, the models to choose from, and the command."""
    out = {}
    for k, v in recipes().items():
        models = v.get("models") if isinstance(v.get("models"), list) else None
        out[k] = {"label": str(v.get("label") or k), "model": recipe_model(v["command"]),
                  "models": [str(x) for x in models if isinstance(x, str)] if models is not None else ["fable", "opus", "sonnet", "haiku"] if is_claude(v["command"]) else [],
                  "command": v["command"], **recipe_defaults(v)}
    return out


def launched(project=None):
    """Agents this server started: still running, or finished in the last ten minutes."""
    out, now = [], time.time()
    with LOCK:
        for key, r in list(RUNNING.items()):
            alive = r["proc"].poll() is None
            if not alive:
                if "ended" not in r:
                    forget_pid(*key, r["proc"].pid)
                r.setdefault("ended", now)
                if now - r["ended"] > 600:
                    r["console"].close()
                    del RUNNING[key]
                    continue
            if project in (None, key[0]):
                out.append({"project": key[0], "agent": key[1], "recipe": r["recipe"], "alive": alive, "started": r["started"], "session": r["session"],
                            "model": r.get("model", ""), "settings": r.get("settings", {})})
    return out


def console_tail(project, agent, session):
    """Read only a launcher-held descriptor and only bytes from this launch."""
    with LOCK:
        record = RUNNING.get((project, agent))
        if not record or record["session"] != session:
            return None
        stream = record["console"]
        end = stream.seek(0, os.SEEK_END)
        start = max(record["start_offset"], end - 65536)
        stream.seek(start)
        data = stream.read(max(0, end - start))
        return {"text": data.decode("utf-8", errors="replace"), "clipped": start > record["start_offset"]}


def run_details(project, agent, session, number):
    path = ROOT / "agent-work" / ".hades" / "invocations.sqlite3"
    if not path.exists():
        return []
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='invocation'").fetchone():
            return []
        rows = conn.execute("""SELECT stage, argv, command, stdin, source FROM invocation
            WHERE project=? AND agent=? AND session=? AND round=? ORDER BY rowid""",
            (project, agent, session, number))
        return [{"stage": stage, "argv": json.loads(argv), "command": command,
                 "stdin": payload.decode("utf-8", errors="backslashreplace"), "source": source}
                for stage, argv, command, payload, source in rows]
    finally:
        conn.close()


def launch(data):
    project, wanted = check_name(data.get("project")), recipes()
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    if not wanted:
        raise ValueError("launching is off: create launchers.json next to server.py (see launchers.example.json)")
    # Each setting comes from this request, else the project's saved loop settings, else the recipe, else the default
    asked, saved = clean_loop(data), clean_loop(config(project).get("loop"), strict=False)
    name = asked.get("recipe") or saved.get("recipe") or (next(iter(wanted)) if len(wanted) == 1 else None)
    if name not in wanted:
        raise ValueError("recipe must be one of: " + ", ".join(wanted))
    recipe = wanted[name]
    settings = {k: asked.get(k, saved.get(k, v)) for k, v in recipe_defaults(recipe).items()}
    model = asked.get("model") or saved.get("model")
    base = with_model(recipe["command"], model) if model else recipe["command"]
    settings["model"], rounds = recipe_model(base), settings["rounds"]
    program = resolve_program(base)  # "claude" may be claude.cmd on Windows; the loop gets the path that will start
    share = max(1, settings["spend"] // settings["workers"]) if settings["spend"] else 0  # the launch's limit, split between its loops
    # Each worker is a loop of its own, named for the notes and the folder it keeps, so a later launch of the same recipe remembers
    # them. Side by side they share the record and nothing else: each takes a different frame and runs the gates on its own claims.
    agents = [f"{name}-{i + 1}" for i in range(settings["workers"])]
    with LOCK:
        alive = [k for k, r in RUNNING.items() if r["proc"].poll() is None]
        working = [a for (p, a) in alive if p == project] or [a for a in worked(project) if orphan(project, a)]
        if working:
            raise ValueError(f"a loop is already running on this project ({working[0]}); stop it first")
        if len(alive) + len(agents) > MAX_RUNNING:
            raise ValueError(f"at most {MAX_RUNNING} loops may run at once; {len(alive)} are running")
        for agent in agents:
            work = ROOT / "agent-work" / project / agent
            fresh = not work.exists()
            work.mkdir(parents=True, exist_ok=True)
            seed = config(project).get("agent_files")
            if fresh and seed and Path(seed).is_dir():  # what the agent starts with: a copy of the target, a spec
                shutil.copytree(seed, work, dirs_exist_ok=True)
            env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
            env["PROJECT_VIEW_URL"] = f"http://127.0.0.1:{PORT}"
            command = [sys.executable, str(ROOT / "scripts" / "loop.py"), project, "--agent", agent, "--harness", str(recipe.get("label") or name),
                       "--max-iterations", str(rounds), "--timeout-min", f'{settings["timeout_min"]:g}', "--max-spend", str(share),
                       *(["--keep-background"] if settings["keep_background"] else []), "--"] + program
            session = uuid.uuid4().hex
            env["HADES_LAUNCH_SESSION"] = session
            env["HADES_DETAILS_DB"] = str((ROOT / "agent-work" / ".hades" / "invocations.sqlite3").resolve())
            with open(work / "loop.log", "ab") as log:
                console = open(work / "loop.log", "rb")
                try:
                    if not os.path.samestat(os.fstat(log.fileno()), os.fstat(console.fileno())):
                        raise OSError("terminal log changed while opening it")
                    start_offset = console.seek(0, os.SEEK_END)
                    proc = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT, **own_group())
                except BaseException:
                    console.close()
                    raise
            (work / "loop.pid").write_text(f"{proc.pid}\n")  # survives a restart of this server, so the loop can still be found
            previous = RUNNING.get((project, agent))
            if previous:
                previous["console"].close()
            RUNNING[(project, agent)] = {"proc": proc, "recipe": name, "session": session, "console": console,
                                         "model": settings["model"], "settings": settings,
                                         "start_offset": start_offset, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    return {"started": agents, "rounds": rounds, "recipe": name, "model": settings["model"], "settings": settings}


# ---------- the few things processes do differently on Windows, kept small so each can be checked on its own ----------

WINDOWS = os.name == "nt"


def own_group(windows=WINDOWS):
    """Start a loop in a process group of its own, so stopping it reaches what it started and nothing else."""
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)} if windows else {"start_new_session": True}


def quiet(windows=WINDOWS):
    """Run a helper command (tasklist, taskkill) without flashing a console window on Windows."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if windows else {}


def resolve_program(command):
    """A bare program name resolved on PATH, so a claude.cmd or codex.exe shim starts without a shell."""
    found = shutil.which(command[0]) if command and not os.path.dirname(command[0]) else None
    return [found, *command[1:]] if found else list(command)


def end_command(pid, windows=WINDOWS):
    """The command that ends a loop and everything under it on Windows, or None where a signal to its group does it."""
    return ["taskkill", "/PID", str(pid), "/T", "/F"] if windows else None


def end_tree(pid, windows=WINDOWS):
    """End a loop's whole process tree. True when the request was made."""
    try:
        if windows:
            subprocess.run(end_command(pid, True), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **quiet(True))
        else:
            os.killpg(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def process_query(pid, windows=WINDOWS):
    """The command that describes a running process: its command line from ps, or only its image name from tasklist."""
    return ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"] if windows else ["ps", "-p", str(pid), "-o", "command="]


def is_loop(shown, project, agent, windows=WINDOWS):
    """Whether process_query's output is this agent's loop. tasklist gives no command line, so on Windows a running Python
    process with the recorded id is taken to be it."""
    if windows:
        image = shown.strip().split(",")[0].strip('"').lower()
        return image.startswith(("python", "py.exe", "pythonw"))
    return "loop.py" in shown and f" {project} --agent {agent} " in shown


def worked(project):
    """The names of every agent that has a working folder on this project."""
    folder = ROOT / "agent-work" / project
    return sorted(d.name for d in folder.iterdir() if d.is_dir()) if folder.is_dir() else []


def orphan(project, agent):
    """The process id of a loop for this agent that an earlier run of this server started and that is still running, or None."""
    if not NAME.match(agent):
        return None
    try:
        pid = int((ROOT / "agent-work" / project / agent / "loop.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid != os.getpid() and running_loop(pid, project, agent) else None


def running_loop(pid, project, agent):
    """Whether the process with this id is, right now, this agent's loop."""
    try:
        shown = subprocess.run(process_query(pid), capture_output=True, text=True, timeout=5, **quiet()).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return is_loop(shown, project, agent)


def forget_pid(project, agent, pid):
    """Remove a finished loop's pid file, so a later process that happens to get the same id is never taken for it."""
    path = ROOT / "agent-work" / project / agent / "loop.pid"
    try:
        if path.read_text().split()[0] == str(pid):
            path.unlink()
    except (OSError, IndexError):
        pass


def stop(data):
    """Stop the project's launched loop (or the one named). Ends the loop and whatever harness it is running, and waits a
    few seconds for it to go, so a Start that follows does not find it still winding down."""
    project, agent, stopped, ending = check_name(data.get("project")), data.get("agent"), [], []
    with LOCK:
        for (p, a), r in list(RUNNING.items()):
            if p == project and agent in (None, "", a) and r["proc"].poll() is None:
                end_tree(r["proc"].pid)
                stopped.append(a)
                ending.append(r["proc"])
    # loops started before this server last restarted are not in RUNNING, but their folders still name them
    for a in [agent] if isinstance(agent, str) and agent else worked(project):
        pid = orphan(project, a) if a not in stopped else None
        if pid and end_tree(pid):
            forget_pid(project, a, pid)
            stopped.append(a)
            ending.append((a, pid))
    deadline = time.time() + 8
    for one in ending:  # outside the lock: the page keeps updating while a round's processes end
        try:
            if isinstance(one, tuple):
                while time.time() < deadline and running_loop(one[1], project, one[0]):
                    time.sleep(0.25)
            else:
                one.wait(max(0.1, deadline - time.time()))
        except subprocess.SubprocessError:
            pass
    for a in stopped:
        ingest({"project": project, "type": "activity", "text": "Loop stopped: stopped by the person", "agent": a})
    return {"stopped": stopped}


def new_project(data):
    """Create a project that is ready to run: what to research, and optionally where the target lives."""
    project = check_name(data.get("project"))
    with LOCK:
        if (PROJECTS / project).exists():
            raise ValueError("a project with that name already exists")
        (PROJECTS / project).mkdir(parents=True)
        (PROJECTS / project / "project.json").write_text(json.dumps(
            {"goal": str(data.get("goal") or "").strip()[:2000],
             **({"target": str(data["target"]).strip()[:4000]} if str(data.get("target") or "").strip() else {}),
             **({"agent_files": str(data["agent_files"])[:500]} if data.get("agent_files") else {})}, indent=2) + "\n")
        load(project)
    return public_config(project)


def set_project(data, create=False):
    """Set the goal, rules, target, direction, pause or loop settings."""
    project = check_name(data.get("project"))
    if not create and not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    problem = config_problem(project)
    if problem:  # writing now would replace the person's file with an empty one plus this change
        raise ValueError(problem)
    loop = clean_loop(data["loop"]) if "loop" in data else None
    offered = recipes()
    if loop and loop.get("recipe") and offered and loop["recipe"] not in offered:
        raise ValueError("loop.recipe must be one of: " + ", ".join(offered))
    if "direction" in data:
        if not isinstance(data["direction"], str) or len(data["direction"]) > 4000:
            raise ValueError("direction must be text of up to 4,000 characters")
    with LOCK:
        cfg = config(project)
        for k in ("goal", "summary", "rules", "target"):
            if isinstance(data.get(k), str):
                cfg[k] = data[k][:4000 if k == "target" else 2000]
        if isinstance(data.get("paused"), bool):
            cfg["paused"] = data["paused"]
        if loop is not None:  # the object sent replaces the stored one
            cfg["loop"] = loop
        if "direction" in data:
            cfg["direction"] = data["direction"].strip()
            if not cfg["direction"]:
                del cfg["direction"]
        (PROJECTS / project).mkdir(parents=True, exist_ok=True)
        save_config(project, cfg)
        load(project)
    return public_config(project)


# ---------- the brief: everything a round with no memory needs, generated from the project ----------

def brief(project, agent):
    """What one round of the loop is given, as text."""
    return plan(project, agent)[0]


def shown(command):
    """A command as someone would type it in this machine's shell."""
    return subprocess.list2cmdline(command) if WINDOWS else shlex.join(command)


def rounds_on(project):
    """frame id -> the agent whose round is on it right now."""
    on, live = {}, working(project)
    for e in load(project):
        if e["type"] == "activity" and e.get("agent"):
            if STARTED.match(e.get("text") or ""):
                on[e["agent"]] = (e.get("data") or {}).get("frame") if isinstance(e.get("data"), dict) else None
            elif re.match(r"(?:Round|Iteration) \d+ (finished|ended badly)|Loop stopped", e.get("text") or ""):
                on.pop(e["agent"], None)
    return {f: a for a, f in on.items() if f and a in live}


HELD = {}  # (project, frame id) -> (agent, when): a frame just handed to a loop, until its round is seen to start


def in_flight(project, events, agent):
    """The frames other loops are on right now, so two rounds side by side never take the same one."""
    busy = {f for f, a in rounds_on(project).items() if a != agent}
    return busy | {f for (p, f), (a, when) in list(HELD.items()) if p == project and a != agent and time.time() - when < 120}


def plan(project, agent, hold=False):
    """(the brief, the id of the frame it gives the round or None). The model does the research; the harness keeps the
    record from its closing handoff, so every line here is about the work."""
    cfg, events = config(check_name(project)), list(load(project))
    update = next((e for e in reversed(events) if e["type"] == "note" and e.get("agent") != agent and HANDOFF.search(e.get("body") or "")), {})  # what another loop last said
    notes = [e for e in events if e["type"] == "note" and e.get("agent") == agent and e.get("body")]
    claims, gates = research(project), gates_of(cfg)
    ruled = [f'- {n.get("text", "")[:300]}' + (f' (why: {n["conditions"][:200]})' if n.get("conditions") else "")
             for n in claims if n.get("status") == "contradicted"][:20]
    live = [n for n in claims if n.get("status") not in ("contradicted", "superseded")]
    line = queue(claims)
    busy = in_flight(project, events, agent)
    frame = next((n for n in line if n["id"] not in busy), None)  # while there is a free frame, the round settles it; when there is none, the round widens
    if frame and hold:
        HELD[(project, frame["id"])] = (agent, time.time())
    # The closing note is the round's memory of its last round. On a different frame it would pull this round off its own.
    before = (notes[-1].get("data") or {}).get("frame") if notes and isinstance(notes[-1].get("data"), dict) else None
    recall = notes and (before is None or before == (frame["id"] if frame else None))  # a note about a different frame would pull this round off its own
    if gates:
        root, hint = str((PROJECTS / project).resolve()), str(cfg.get("artifact") or "").strip()[:300]
        commands, stops, rounds = [g for g in gates if "command" in g], [g for g in gates if "rate" not in g], [g for g in gates if "command" not in g]
        rule = [f"A RESULT COUNTS ONLY WHEN IT PASSES {'THE GATES, in this order' if len(stops) > 1 else 'THE GATE'}: " + ", ".join(g["name"] for g in stops) + ".",
                "- They run after the round, on every Results line. End a line with [check: <artifact>] to name what should be checked"
                + (f": {hint}" if hint else "") + "." + (" A command gate cannot run without one." if commands else ""),
                *[f'- {g["name"]}: ' + (shown([part.replace("{project}", root) for part in g["command"]]) + ("" if any("{artifact}" in part for part in g["command"]) else " <artifact>")
                                        if "command" in g else "a separate round that sees only your claim and tries to refute it. It is told: " + g["refute"] if "refute" in g
                                        else "a separate round that says how much the result matters, from 1 to 5. It is told: " + g["rate"][:300]) for g in gates],
                "- A checker sees the Results line and its artifact, and nothing else from this round."
                + (" Each line costs " + ("a full checking round." if len(rounds) == 1 else f"up to {len(rounds)} full checking rounds.") if rounds else ""),
                "- The gates and what they read are not yours to edit. If one looks wrong, say so in your Handoff."]
    else:
        rule = ["Nothing re-checks your results, so every result you report is a claim. Say what you observed and how to see it again."]
    out = [*steering(cfg, events),
           *(["YOUR CLOSING NOTE FROM YOUR LAST ROUND (your only memory of it)", notes[-1]["body"][-2000:], ""] if recall else []),
           *[f"{k.upper()}: {cfg[k]}" for k in ("target", "goal", "rules") if isinstance(cfg.get(k), str) and cfg[k].strip()],
           *([] if str(cfg.get("goal") or "").strip() else
             ["GOAL: none yet. Work from the direction above."] if str(cfg.get("direction") or "").strip() else
             ["GOAL: none yet. Do not invent work: say in your handoff that the project has no goal, and stop."]),
           "",
           # What the round is for, and nothing about how: the model does the research.
           "This round has one frame to settle, named at the end of this brief." if frame else
           "Nothing is queued, so this round is yours: find what is worth finding, however you see fit. What you do not get to goes on Leads lines.",
           *(["A Results or Ruled out line that settles a frame or claim listed below starts with its id in brackets, like [k12]."] if live else []),
           "Nobody is watching. Where only the person could decide, take the cautious option and say so on the Assumed line.",
           "",
           *rule]
    questions = [q for q in merged(events, "question").values() if q["asker"] != "you"]
    words = {}  # the answer arrives as a later event without the question's text, so take the text from where it was asked
    for e in events:
        if e["type"] == "question" and e.get("text"):
            words.setdefault(e.get("id"), e["text"])
    answered, waiting = [q for q in questions if q.get("answer")][-6:], [q for q in questions if not q.get("answer")][-6:]
    if answered:
        out += ["", "THE PERSON DECIDED (treat as fact)", *[f'- {words.get(q["id"], "")[:300]}\n  Answer: {q["answer"][:600]}' for q in answered]]
    if waiting:
        out += ["", "ASSUMPTIONS IN FORCE (earlier rounds made them in the person's place; keep to them, and do not raise them again, "
                "unless one contradicts the goal or the rules: then follow those and say so on the Assumed line)",
                *[f'- {words.get(q["id"], "")[:300]}' for q in waiting]]
    if update:
        out += ["", f'THE LATEST HANDOFF FROM ANOTHER LOOP ({update.get("agent")})', update["body"][HANDOFF.search(update["body"]).start():][:2000]]
    title = lambda n: f'- [{n["id"]}] {n.get("text", "")[:200]}' + (" (the person's priority)" if n["priority"] else "")
    by = {g["name"]: g for g in gates}

    def halted(n, indent="  "):  # what the gate that stopped a claim said, for whoever has to answer it
        got = n["gates"][n["at"]]
        said = "\n".join(indent + "  " + row for row in (got.get("detail") or "").strip().splitlines()[-8:])[-700:]
        through = [g["name"] for g in gates[:n["standing"]]]
        word = "could not run" if n["state"] == "error" else "says it is already known" if got.get("verdict") == "known" else "refuted" if "command" not in by[n["at"]] else "failed"
        return [*([f'{indent}Passed: {", ".join(through)}.'] if through else []),
                f'{indent}Stopped at {n["at"]} ({word}) on {got.get("checked") or n.get("artifact") or "the claim alone"}' + (":\n" + said if said.strip() else ".")]

    established = sorted((n for n in live if n["state"] == "established"), key=lambda n: (-n["audited"], -(n["rating"] or 0)))
    if established:
        out += ["", f"ESTABLISHED (passed {'every gate' if len(gates) > 1 else 'the gate'}: build on these, do not redo them)",
                *[f'- [{n["id"]}] {n.get("text", "")[:300]}' + (f' (artifact: {n["artifact"]})' if n.get("artifact") else "") + (f' (rated {n["rating"]} of 5)' if n["rating"] else "") for n in established[:20]]]
    stopped = sorted((n for n in live if n["state"] in ("failed", "error") and n is not frame), key=lambda n: -n["standing"])
    old = [n for n in stopped if n["gates"][n["at"]].get("verdict") == "known"]  # nothing to repair: one line each, so no round finds them a second time
    stopped = [n for n in stopped if n not in old]
    if old:
        out += ["", "ALREADY KNOWN (a gate found each reported elsewhere: do not claim them again, in these or other words)",
                *[f'- [{n["id"]}] {n.get("text", "")[:200]}' for n in old[:30]]]
    if stopped:
        out += ["", "CLAIMED, BUT STOPPED AT A GATE" + (" (for context: not this round's work)" if frame else " (to repair one, claim it again starting the Results line with its id; or rule it out)")]
        for n in stopped[:8]:
            out += [f'- [{n["id"]}] {n.get("text", "")[:300]}', *halted(n)]
    claimed = [n for n in live if n.get("status") != "open" and n["state"] in (None, "unchecked", "waiting") and n is not frame]
    out += ["", "CLAIMED, NOT YET THROUGH THE GATES (another round's word, nothing more)" if gates else "CLAIMED (another round's word, nothing more)", *map(title, claimed[:12])] if claimed else []
    later = [n for n in line if n is not frame and n["state"] != "failed"]
    out += ["", "ALSO QUEUED (for later rounds, in this order; not this round's work, and not to be proposed again as leads)", *map(title, later[:20]),
            *([f"  and {len(later) - 20} more"] if len(later) > 20 else [])] if later else []
    spent = [n for n in live if n["stalled"]]
    out += ["", "SET ASIDE (their rounds are spent; come back to one only as a new lead with a new angle, and say what is new)", *map(title, spent[:8])] if spent else []
    out += ["", "RULED OUT (do not retry without a new reason, and say what is different if you do)", *ruled] if ruled else []
    if frame:
        mark = f'[{frame["id"]}]'
        out += ["", f'THIS ROUND\'S FRAME (round {frame["attempts"] + 1} of {frame["budget"]} on it' + ("; the person marked it" if frame["priority"] else "") + ")",
                *thread_lines(frame),
                *([f"It is a claim that a gate stopped. Answer what the gate said:", *halted(frame, ""),
                   "If the gate showed it is already known or that it cannot be made to happen, rule it out; do not reword it and claim it again."]
                  if frame["state"] == "failed" else []),
                "Settle it one way or the other:",
                f"- a Results line that starts with {mark}" + (" and ends with [check: <artifact>], or" if gates else ", or"),
                f"- a Ruled out line that starts with {mark} and says what you examined and why it is not there.",
                "If you cannot, say what blocked you and make Next the quickest test for this same frame. Other places to look go on Leads lines."]
    return "\n".join(out) + "\n", frame["id"] if frame else None


def status(project, base):
    """Where a project stands, as plain text: is it running and on what, how far each claim got, what is next. This is
    what an assistant reads first when the person asks it what is going on."""
    cfg, events = config(check_name(project)), list(load(project))
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    claims, gates = research(project), gates_of(cfg)
    live = [n for n in claims if n.get("status") not in ("contradicted", "superseded")]
    by, gate_by = (lambda state: [n for n in live if n["state"] == state]), {g["name"]: g for g in gates}
    line, ago = queue(claims), _ago
    runs = launched(project)
    alive = next((r for r in runs if r["alive"]), None)
    marks = [(i, e) for i, e in enumerate(events) if e["type"] == "activity" and re.match(r"((?:Round|Iteration) \d+ (started|finished|ended badly)|Loop stopped|A round ended without)", e.get("text") or "")]
    began = next(((i, e) for i, e in reversed(marks) if STARTED.match(e["text"])), None)
    stopped = next((e for _, e in reversed(marks) if e["text"].startswith("Loop stopped")), None)
    silent = next((i for i, e in reversed(marks) if e["text"].startswith("A round ended without")), -1)
    doing = ""
    if began:
        i, last = began
        over = any(j > i and not e["text"].startswith("A round ended without") for j, e in marks)  # finished, ended badly, or the loop stopped
        noted = any(j > i and e["type"] == "note" and e.get("agent") == last.get("agent") for j, e in enumerate(events))
        given = (last.get("data") or {}).get("frame")
        on = next((n for n in claims if n["id"] == given), None)
        doing = ("Last round: " if over or not alive else "Round ended, its gates are running now (a refuter is a full round of the model): " if noted else "Round in progress: ") \
            + f'round {last["text"].split()[1]} of this run, by {last.get("agent") or "unknown"}, started {ago(last.get("ts"))}, ' \
            + (f'settling [{on["id"]}] {on.get("text", "")[:120]}' if on else "widening (nothing was queued, so it is looking for places to look)")
    title = lambda n: f'- [{n["id"]}] {n.get("text", "")[:200]}'
    out = [f'Hades project "{project}"   page: {base}/#/{project}   record: {base}/api/research?project={project}',
           "Goal: " + (str(cfg.get("goal") or "").strip() or "none set"),
           *(["Direction: " + cfg["direction"].strip()[:400]] if isinstance(cfg.get("direction"), str) and cfg["direction"].strip() else []),
           "Loop: " + ("paused by the person; " if cfg.get("paused") else "")
           + (f'running as {", ".join(r["agent"] for r in runs if r["alive"])} ({alive["recipe"]}' + (f', {alive["model"]}' if alive.get("model") else "") + f'), up to {alive["settings"].get("rounds") or "unlimited"} rounds' if alive else
              "not running" + (f' ({stopped["text"]}, {ago(stopped.get("ts"))})' if stopped else "")),
           *([doing] if doing else []),
           *(["PROBLEM: " + config_problem(project) + ". The loop holds until it is fixed."] if config_problem(project) else []),
           "Gates: " + (" -> ".join(g["name"] + (" (refuter" if "refute" in g else " (rater" if "rate" in g else "") + (f', on {g["recipe"]}' if g.get("recipe") else "") + (")" if "command" not in g else "") for g in gates) + (" [built in; the project set none]" if "gates" not in cfg else "")
                        if gates else "none: every result is only a claim"),
           f'Spent: {sum(1 for _, e in marks if STARTED.match(e["text"]))} research rounds and '
           f'{sum(1 for e in events if e["type"] == "research" and e.get("source") == "gate" and "command" not in gate_by.get(e.get("gate"), {"command": 1}))} checking rounds (refuters and raters), for {len(by("established"))} established.',
           f'Counts: {len(by("established"))} established, {len(by("failed")) + len(by("error"))} stopped at a gate, {len(by("waiting"))} waiting for a gate, '
           f'{len([n for n in live if n.get("status") == "supported" and n["state"] in (None, "unchecked")])} claimed and unchecked, '
           f'{len([n for n in claims if n.get("status") == "contradicted"])} ruled out, {len(line)} queued, {len([n for n in live if n["stalled"]])} set aside']
    if by("established"):
        out += ["", "ESTABLISHED (through every gate; the most important first, by the rater's word where there is one)",
                *[title(n) + (f' (rated {n["rating"]} of 5)' if n["rating"] else "") + (f' (artifact: {n["artifact"]})' if n.get("artifact") else "") + (" [audited by the person]" if n["audited"] else "")
                  for n in sorted(by("established"), key=lambda n: (-n["audited"], -(n["rating"] or 0)))[:20]]]
    halted = sorted(by("failed") + by("error"), key=lambda n: -n["standing"])
    if halted:
        out += ["", "STOPPED AT A GATE"]
        for n in halted[:10]:
            got = n["gates"][n["at"]]
            said = (got.get("detail") or "").strip().splitlines()
            word = "could not run" if n["state"] == "error" else "says it is already known (it is not queued again)" if got.get("verdict") == "known" else "said no"
            out += [title(n), f'  passed {n["standing"]} of {len([g for g in gates if "rate" not in g])}; {n["at"]} {word}' + (f': {said[-1][:240]}' if said else "")]
    for heading, some in (("WAITING FOR A GATE (it runs after the next round)", by("waiting")),
                          ("CLAIMED AND UNCHECKED (a round's word; no gate could run on it)", [n for n in live if n.get("status") == "supported" and n["state"] in (None, "unchecked")])):
        out += ["", heading, *[title(n) + (f' (at {n["at"]})' if n.get("at") else "") + (" (marked by the person)" if n["priority"] else "") for n in some[:8]],
                *([f"  and {len(some) - 8} more"] if len(some) > 8 else [])] if some else []
    if line:
        out += ["", "QUEUE (the next round takes the first)", *[title(n) + f' ({n["attempts"]} of {n["budget"]} rounds spent' + ("; marked by the person" if n["priority"] else "") + ")" for n in line[:8]],
                *([f"  and {len(line) - 8} more"] if len(line) > 8 else [])]
    aside = [n for n in live if n["stalled"]]
    out += ["", "SET ASIDE (rounds spent, not settled)", *map(title, aside[:8])] if aside else []
    held = [q for q in merged(events, "question").values() if q["asker"] != "you" and not q.get("answer")]
    words = {}
    for e in events:
        if e["type"] == "question" and e.get("text"):
            words.setdefault(e.get("id"), e["text"])
    out += ["", "ASSUMPTIONS IN FORCE (a round made each in the person's place; answer one to overrule it)", *[f'- [{q["id"]}] {words.get(q["id"], "")[:240]}' for q in held[-8:]]] if held else []
    at, note = next(((i, e) for i, e in reversed(list(enumerate(events))) if e["type"] == "note" and HANDOFF.search(e.get("body") or "")), (-1, None))
    if note:  # its results, dead ends, assumptions and leads are all above as threads; what is left is what it did and what it would do next
        body, told = note.get("body") or note.get("text") or "", []
        for row in body.splitlines():
            m = LABEL.match(row)
            if m:
                keep = m.group(1).lower() in ("handoff", "next")
            if not m and not told:
                keep = True  # a handoff in no known shape: show its opening lines
            if keep and row.strip():
                told.append(row)
        out += ["", f'LATEST HANDOFF ({note.get("agent") or "unknown"}, {ago(note.get("ts"))})',
                *(["(A later round ended without a Handoff block, so nothing was recorded from it; this is the one before.)"] if silent > at else []),
                "\n".join(told)[:1200]]
    elif silent >= 0:
        out += ["", "No handoff yet: the last round ended without a Handoff block, so nothing was recorded from it."]
    p = f'"project": "{project}"'
    out += ["", f"WHAT YOU CAN DO (each is JSON POSTed to {base}; the loop never waits for any of it)",
            f'- Queue places to look: /api/frames {{{p}, "frames": ["..."]}}',
            f'- Put a frame first, with its rounds to spend again: /api/events {{{p}, "type": "research", "id": "k1", "priority": true, "source": "you"}}',
            '- Drop a frame or a claim, or rule it out: the same, with "status": "superseded" or "contradicted" in place of priority',
            f'- Tell every round something: /api/project {{{p}, "direction": "..."}}',
            f'- Overrule an assumption: /api/events {{{p}, "type": "question", "id": "q1", "answer": "...", "source": "you"}}',
            f'- Hold or release the loop: /api/project {{{p}, "paused": true}}. Start or stop it: /api/launch or /api/stop {{{p}}} (starting spends the person\'s model usage: ask first)',
            f"- Gates, loop settings, new projects: {base}/AGENTS.md"]
    return "\n".join(out) + "\n"


def _ago(ts):
    try:
        seconds = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(ts))).total_seconds()
    except (TypeError, ValueError):
        return "at an unknown time"
    return "just now" if seconds < 90 else f"{seconds / 60:.0f} min ago" if seconds < 5400 else f"{seconds / 3600:.0f} h ago" if seconds < 172800 else f"{seconds / 86400:.0f} days ago"


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body=None, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def local(self):
        """True when the request names this server by a local address. A page on another site can be made to resolve
        here, but it then arrives under that site's name, and is refused."""
        host = self.headers.get("Host", "")
        return not host or (re.fullmatch(r"[\w.\-\[\]:]{1,100}", host) is not None and urlparse("http://" + host).hostname in ("127.0.0.1", "::1", "localhost"))

    def do_GET(self):
        if not self.local():
            return self.reply(403, {"error": "Hades answers only on this machine's own address."})
        url = urlparse(self.path)
        arg = lambda k: (parse_qs(url.query).get(k) or [""])[0]
        try:
            if url.path == "/":
                self.reply(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/AGENTS.md":
                self.reply(200, (ROOT / "AGENTS.md").read_bytes(), "text/markdown; charset=utf-8")
            elif url.path == "/api/projects":
                offered, problem = launchers(), launcher_file()[1]
                # epoch changes on every server start, so an open page knows to refetch instead of mixing two logs
                self.reply(200, [{"id": p, "config": public_config(p), "count": len(load(p)), "epoch": BOOT, "hold": hold(p),
                                  "launched": launched(p), "launchers": offered, "launcherError": problem} for p in project_ids()])
            elif url.path in ("/api/terminal", "/api/run-details"):
                host = self.headers.get("Host", "")  # a page on another site that resolves here must not read the terminal
                if re.fullmatch(r"[\w.\-\[\]:]{1,100}", host) and urlparse("http://" + host).hostname not in ("127.0.0.1", "::1", "localhost"):
                    return self.reply(403, {"error": "Terminal output is available on localhost."})
                if url.path == "/api/run-details":
                    return self.reply(200, run_details(arg("project"), arg("agent"), arg("session"), int(arg("round") or 0)))
                tail = console_tail(arg("project"), arg("agent"), arg("session"))
                self.reply(200 if tail is not None else 404, tail or {"error": "No attached terminal."})
            elif url.path == "/api/events":
                events, after = load(check_name(arg("project"))), max(0, int(arg("after") or 0))
                self.reply(200, {"events": events[after:after + 5000]})
            elif url.path == "/api/project":
                self.reply(200, {**public_config(check_name(arg("project"))), "hold": hold(arg("project"))})
            elif url.path == "/api/status":
                host = self.headers.get("Host", "")
                base = "http://" + (host if re.fullmatch(r"[\w.\-\[\]:]{1,100}", host) else f"127.0.0.1:{PORT}")
                text = status(arg("project"), base) if arg("project") else "".join(
                    f'{p}: {(config(p).get("goal") or "no goal")[:100]}   {base}/api/status?project={p}\n' for p in project_ids()) or "No projects yet.\n"
                self.reply(200, text.encode(), "text/plain; charset=utf-8")
            elif url.path == "/api/gates":
                self.reply(200, gates_pending(arg("project"), arg("agent")[:80]))
            elif url.path == "/api/research":
                self.reply(200, research(arg("project"), arg("q"), arg("area")))
            elif url.path == "/api/brief":
                text, frame = plan(arg("project"), arg("agent")[:80], hold=arg("as") == "json")
                if arg("as") == "json":  # the loop asks this way, to learn which frame its round was given
                    return self.reply(200, {"brief": text, "frame": frame})
                self.reply(200, text.encode(), "text/plain; charset=utf-8")
            else:
                self.reply(404, {"error": "not found"})
        except ValueError as e:
            self.reply(400, {"error": str(e)})
        except Exception as e:  # our bug, not the caller's: say so plainly instead of hanging up
            traceback.print_exc()
            self.reply(500, {"error": f"server error: {type(e).__name__}: {e}"})

    def do_POST(self):
        # Every write starts or steers an agent that can run commands, so a web page open in the person's browser must not
        # be able to make one. A browser sends such a page's request with its Origin, and cannot send JSON across sites
        # without asking first; the loop, curl and this server's own page all pass.
        origin = self.headers.get("Origin")
        if not self.local() or (origin is not None and origin != "http://" + self.headers.get("Host", "")) or self.headers.get_content_type() != "application/json":
            return self.reply(403, {"error": "Send JSON (Content-Type: application/json) from this machine; requests from other sites are refused."})
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 200000:
                return self.reply(413, {"error": "request is larger than 200 kB"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            path = urlparse(self.path).path
            if path == "/api/events":
                out = ingest(data)
            elif path == "/api/project":
                out = set_project(data)
            elif path == "/api/new":
                out = new_project(data)
            elif path == "/api/frames":
                out = add_frames(data)
            elif path == "/api/launch":
                out = launch(data)
            elif path == "/api/stop":
                out = stop(data)
            else:
                return self.reply(404, {"error": "not found"})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self.reply(500, {"error": f"server error: {type(e).__name__}: {e}"})
        self.reply(200, out)

    def log_message(self, *args):
        pass


def starter():
    """The first project on a fresh install, with no goal yet. The person types what to research on the page."""
    return set_project({"project": "workspace", "goal": ""}, create=True)


def serve():
    print(f"Hades on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--dashboard" in sys.argv and not project_ids():
        starter()
    serve()
