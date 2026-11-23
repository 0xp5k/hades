#!/usr/bin/env python3
"""Hades server: the live page, and the one port a research loop reports to. It listens on this machine only.

  python3 server.py                  serve the page at http://127.0.0.1:8765 (PROJECT_VIEW_PORT changes the port)
  python3 server.py --dashboard      the same, creating one empty project first if there are none

Each project is a folder under projects/ holding:
  project.json   goal, direction, loop settings and the pages this project has
  events.jsonl   append-only log of everything reported

What the loop, the page and a chat agent call:
  GET  /api/status?project=X                  plain text: where the project stands. Read this first.
  GET  /api/brief?project=X&agent=NAME        plain text: what one round needs to know, generated fresh
  GET  /api/project?project=X                 the project's config, and "hold" when it is paused
  GET  /api/projects                          every project, its launched loop and the launch recipes
  GET  /api/events?project=X&after=N          the log
  GET  /api/research?project=X&q=TEXT&area=A  current research threads
  GET  /api/gates?project=X                   the claims waiting for a gate, and what to run on each
  POST /api/frames   {"project", "frames"}    queue places to look, many at once
  POST /api/events   {"project", "type", ...} report: activity, item, question, research or topic
  POST /api/project  {"project", ...}         set goal, summary, rules, target, direction, paused, loop or a section
  POST /api/new      {"project", "goal"}      create a project
  POST /api/launch   {"project", ...}         start the project's loop from a recipe in launchers.json
  POST /api/stop     {"project"}              stop it
  POST /api/terminal/send {"project", "agent", "session", "text"}   message a live Claude round (this server's own page only)

Trust: every caller is on this machine and is trusted. Run it somewhere you would run the agents themselves.
"""
import datetime as dt
import hashlib
import json
import os
import re
import select
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
TYPES = ["activity", "item", "question", "research", "topic"]
STRINGS = {"id": 80, "section": 80, "key": 200, "area": 200, "scope": 500, "snapshot": 200, "status": 40, "agent": 80,
           "harness": 80, "model": 60, "source": 20, "body": 30000, "answer": 6000, "known": 400, "origin": 20}
EVENTS = {}  # project id -> list of events, loaded from disk on first use
IDS = {}  # project id -> {(type, id)} already in its log, so accepting a report never has to scan the log
KEYED = ("question", "research")
RESEARCH_KINDS = ("hypothesis", "observation", "experiment", "conclusion")
RESEARCH_STATES = ("open", "supported", "contradicted", "reproduced", "superseded")
RESEARCH_RELATIONS = ("supports", "contradicts", "depends on", "variant of", "same cause as", "follows from")
GATE_VERDICTS = ("passed", "failed", "error")  # error: the gate could not be run, or ran out of time
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
                  "Try to make this claim fail, three ways. (1) Make it happen again yourself, from its artifact or its description, in this folder: "
                  "if you cannot, it is refuted. (2) Hold it against the goal as stated above: if it only answers an easier reading of the goal, it is "
                  "refuted. (3) Look for where it has already been reported or published: if you can cite that, it is refuted as not new."}]


def gates_of(cfg):
    """The project's gates, in the order a claim must pass them. A gate is either a command (exit code 0 passes) or a
    refuter: a separate fresh round that sees only the claim and tries to kill it. A project that sets none gets the
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
        if not runs and not refute:
            continue
        limit = g.get("timeout_min")
        out.append({"name": name, **({"command": command} if runs else {"refute": refute}),
                    "timeout_min": limit if isinstance(limit, (int, float)) and not isinstance(limit, bool) and 0 < limit <= 1440 else 10})
    return out


HASHED = {}  # locked file -> (its modification time and size, its hash), so a poll does not re-read a large locked corpus


def fingerprint(project, cfg):
    """What every verdict is bound to: the gates as configured, and the files in the project's folder that the person
    locked (the exact question, the gate's own script). Change either and earlier verdicts are stale: they count for
    nothing until the gates have run again."""
    gates = gates_of(cfg)
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


def public_config(project):
    cfg = config(project)
    cfg["direction"] = cfg["direction"] if isinstance(cfg.get("direction"), str) else ""
    cfg["loop"] = clean_loop(cfg.get("loop"), strict=False)
    cfg["problem"] = config_problem(project)
    cfg["default_gates"] = "gates" not in cfg  # the built-in refuter is in force: the project set no gates of its own
    cfg["gates"], cfg["fingerprint"] = gates_of(cfg), fingerprint(project, cfg)
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

    if type == "item":
        if not event.get("section") or not event.get("key"):
            raise ValueError("an item needs a section and a key")
    elif type == "topic":  # one page in the Library; sending the same key again replaces it
        if not event.get("key") or not (event.get("body") or event["text"]):
            raise ValueError("a topic needs a key (its title) and a body")
    elif type == "research" and data.get("source") == "gate":  # one gate's verdict on what a thread claims; nothing else about it changes
        cfg = config(project)
        if not event.get("id") or data.get("verdict") not in GATE_VERDICTS or not any(g["name"] == data.get("gate") for g in gates_of(cfg)):
            raise ValueError("a gate verdict needs the thread's id, one of the project's gates by name, and verdict one of: " + ", ".join(GATE_VERDICTS))
        # Stamped with what it ran under, so it stops counting when the gates or the locked files change. One that ran
        # under an earlier version (they changed while it ran) is refused: the gate is still due, and runs again.
        seal = fingerprint(project, cfg)
        if data.get("fp") is not None and data["fp"] != seal:
            raise ValueError("the gates or the locked files changed while this gate ran; it is still due and will run again")
        event = {"type": type, "id": event["id"], "gate": data["gate"], "verdict": data["verdict"], "detail": str(data.get("detail") or "")[-4000:],
                 "checked": str(data.get("checked") or "")[:300], "fp": seal, "source": "gate"}
    elif type == "research" and data.get("source") == "you" and any(k in data for k in ("priority", "status", "score", "audited")) and not any(k in data for k in ("kind", "text")):
        # the person's mark alone (Pursue, Drop, Reopen, a score, their audit): the thread's claim, evidence and history stay as they were
        if not event.get("id") or any(k in data and not isinstance(data[k], bool) for k in ("priority", "audited")) \
                or ("status" in data and data["status"] not in ("open", "superseded", "contradicted")) \
                or ("score" in data and (isinstance(data["score"], bool) or data["score"] not in (1, 2, 3, 4, 5))):
            raise ValueError("a change to a thread needs its id, and priority or audited true or false, score 1 to 5, or status open (reopen), superseded (drop) or contradicted (rule out)")
        event = {"type": type, "id": event["id"], **{k: data[k] for k in ("priority", "status", "score", "audited") if k in data}, "source": "you"}
        if data.get("audited"):  # their audit is of this artifact under these gates; it lapses when either changes
            seen = next((n for n in research(project) if n["id"] == event["id"]), {})
            event.update(audit_fp=fingerprint(project, config(project)), audit_artifact=seen.get("artifact") or "")
    elif type == "research":
        if ("verdict" in data or "gate" in data) and "kind" not in data:
            raise ValueError("a gate's verdict is recorded by the loop when it runs the gate; it is not something to report")
        if data.get("source") == "you" and data.get("priority") is not None:  # an agent copying the mark back changes nothing
            if not isinstance(data["priority"], bool):
                raise ValueError("priority must be true or false")
            event["priority"] = data["priority"]
        if data.get("kind") not in RESEARCH_KINDS:
            raise ValueError("research kind must be one of: " + ", ".join(RESEARCH_KINDS))
        if data.get("status") not in RESEARCH_STATES:
            raise ValueError("research status must be one of: " + ", ".join(RESEARCH_STATES))
        if not event["text"]:
            raise ValueError("research needs a short claim in text")
        event["kind"] = data["kind"]
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
        links = data.get("links", [])
        evidence = data.get("evidence", [])
        if not isinstance(links, list) or len(links) > 20 or not isinstance(evidence, list) or len(evidence) > 20:
            raise ValueError("research links and evidence must be lists of at most 20 entries")
        if not all(isinstance(x, dict) and isinstance(x.get("to"), str) and x.get("relation") in RESEARCH_RELATIONS for x in links):
            raise ValueError("each research link needs a target id and a known relation")
        if not all(isinstance(x, dict) and isinstance(x.get("ref"), str) for x in evidence):
            raise ValueError("each evidence entry needs a ref")
        if "links" in data:
            event["links"] = [{"to": x["to"][:80], "relation": x["relation"]} for x in links]
        if "evidence" in data:
            event["evidence"] = [{"ref": x["ref"][:500], "note": str(x.get("note") or "")[:1000]} for x in evidence]
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
                raise ValueError(f'no {"research thread" if type == "research" else "assumption or question"} has the id {str(data.get("id"))[:80]}')
            if not known and not event["text"]:
                raise ValueError("text is required")
        event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **event}
        with open(PROJECTS / project / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        events.append(event)
        if type in KEYED:
            IDS[project].add((type, event["id"]))
    if type == "item" and event["section"] == "round-notes":
        from_handoff(project, event)  # the agent only writes its handoff; the record is kept from it here
    return event


HANDOFF = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:#+[ \t]*)?(?:\*\*|__)?[ \t]*handoff[ \t]*(?:\*\*|__)?[ \t]*(?::|$)", re.I | re.M)
LABEL = re.compile(r"^\s*(?:[-*]\s+)?(?:#+\s*)?(?:\*\*|__)?\s*(handoff|results?|ruled[ -]out|decisions?(?:\s+needed)?|assum(?:ed|ptions?)|leads?|next)"
                   r"\s*(?:\*\*|__)?\s*(?::\s*(?:\*\*|__)?\s*(.*))?$", re.I)
BULLET = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
TAG = re.compile(r"\s*\[(known|new)(?::\s*([^\]]*))?\]\s*$", re.I)
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
    """Keep what a round reported in its handoff: the Project brief's current update, a thread per result, ruled-out
    approach and lead, and each thing it assumed in the person's place. A round note without a Handoff marker records
    nothing, and says so in the log."""
    body = note.get("body") or note.get("text") or ""
    marks = list(HANDOFF.finditer(body))
    agent, data = note.get("agent"), note.get("data") if isinstance(note.get("data"), dict) else {}
    if not marks:
        ingest({"project": project, "type": "activity", "agent": agent, "text": "A round ended without a Handoff block: nothing was recorded from it"})
        return
    block = body[marks[-1].start():].strip()
    ingest({"project": project, "type": "item", "section": "now", "key": "Current update", "body": block, "agent": agent, "origin": "handoff"})
    seen = {plain(n.get("text") or ""): n["id"] for n in research(project)}
    parts = handoff_items(block)
    asked = {plain(q.get("text") or "") for q in merged(load(project), "question").values() if not q.get("answer")}
    for line in parts["decision"]:  # what the round assumed in the person's place: in force until they say otherwise
        if plain(line) not in asked:
            asked.add(plain(line))
            ingest({"project": project, "type": "question", "text": line[:2000], "agent": agent, "origin": "handoff"})
    # Whatever is still work can be settled by name: an open frame, a claim a gate stopped (the round may claim it again
    # with a repaired artifact, or rule it out), and a claim the person marked.
    waiting = {n["id"]: n for n in research(project) if n["frame"] or n["state"] in ("failed", "error")}
    where = {"folder": data["cwd"]} if isinstance(data.get("cwd"), str) and data["cwd"] else {}  # where the round's artifacts are
    for label, kind, status in (("results", "conclusion", "supported"), ("ruled out", "observation", "contradicted"), ("leads", "hypothesis", "open")):
        for line in parts[label]:
            # Tags first, in any order, and only then the length limit, so a long line never loses what it named.
            named_artifact, worth = CHECK_TAG.search(line), SCORE_TAG.search(line)
            line = SCORE_TAG.sub("", CHECK_TAG.sub("", line)).strip()
            artifact = {"artifact": named_artifact.group(1).strip()} if label == "results" and named_artifact and named_artifact.group(1).strip() else {}
            tag = TAG.search(line) if label == "results" else None
            text = (line[:tag.start()].strip() if tag else line)[:400]
            if not text:
                continue
            extra = {"known": "new" if tag.group(1).lower() == "new" else (tag.group(2) or "").strip() or "known"} if tag else {}
            extra.update({"score": int(worth.group(1))} if worth and label == "leads" else {})
            # A line that starts with a waiting thread's id, as [id], settles that thread in place, so it leaves the work
            # list: a result makes it a claim, a ruled-out line closes it with the reason. An id elsewhere in the line is prose.
            first = re.match(r"\s*\[([\w.-]+)\]\s*:?\s*", text) if label != "leads" else None
            named = waiting.pop(first.group(1), None) if first else None
            if named:
                said = text[first.end():].strip() or text
                if label == "ruled out":
                    ingest({"project": project, "type": "research", "id": named["id"], "text": named.get("text", ""), "agent": agent,
                            "kind": named.get("kind") or "hypothesis", "status": "contradicted", "conditions": said[:2000]})
                    continue
                # Claimed, or claimed again: every gate is still to come, on the artifact it names now (or the one it had).
                # A frame was a place to look, so the claim's own words replace it; a claim keeps its words and gains a note.
                was_frame = named.get("status") == "open"
                ingest({"project": project, "type": "research", "id": named["id"], "agent": agent, "kind": "conclusion", "status": "supported", **extra, **where,
                        "artifact": artifact.get("artifact") or named.get("artifact") or "",
                        **({"text": said, "found_in": named.get("text", "")[:300]} if was_frame else {"text": named.get("text", ""), "body": said})})
                continue
            if plain(text) in seen:
                same = next((n for n in research(project) if n["id"] == seen[plain(text)]), None) if artifact else None
                if same and same.get("status") not in ("contradicted", "superseded"):  # the same claim, now with something to check
                    ingest({"project": project, "type": "research", "id": same["id"], "text": same.get("text", ""), "agent": agent,
                            "kind": same.get("kind") or kind, "status": same.get("status") or status, **artifact, **where})
                continue
            made = ingest({"project": project, "type": "research", "kind": kind, "status": status, "text": text, "agent": agent, "origin": "handoff",
                           **extra, **artifact, **(where if label == "results" else {})})
            seen[plain(text)] = made["id"]
    # The round was given one frame. If it ends with the frame still open, its Next line is where the next round on that
    # frame starts, whoever runs it.
    given = data.get("frame") if isinstance(data.get("frame"), str) else None
    still = next((n for n in research(project) if n["id"] == given and n["frame"]), None) if given and parts["next"] else None
    if still and plain(parts["next"][0]) != plain(still.get("next") or ""):
        ingest({"project": project, "type": "research", "id": still["id"], "text": still.get("text", ""), "agent": agent,
                "kind": still.get("kind") or "hypothesis", "status": still.get("status") or "open", "next": parts["next"][0][:2000]})


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
        added.append(ingest({"project": project, "type": "research", "kind": "hypothesis", "status": "open", "origin": "queue", "agent": data.get("agent"),
                             **{k: one[k] for k in ("text", "score", "area", "next", "scope") if one.get(k) is not None}})["id"])
    return {"added": added, "skipped": len(given) - len(added)}


def gates_pending(project):
    """What the loop should run next: for each live claim, the first gate it has no current verdict from, the person's
    marks first and then the oldest. A claim a gate stopped waits there until it is claimed again."""
    cfg = config(check_name(project))
    gates, root, seal = {g["name"]: g for g in gates_of(cfg)}, str((PROJECTS / project).resolve()), fingerprint(project, cfg)
    due = [n for n in research(project) if n["state"] == "waiting" and n.get("status") not in ("contradicted", "superseded")]
    out = []
    for n in sorted(due, key=lambda n: (-n["priority"], n["born"])):
        gate = gates[n["at"]]
        out.append({"id": n["id"], "artifact": n.get("artifact") or "", "text": n.get("text", ""), "gate": gate["name"], "timeout_min": gate["timeout_min"],
                    "fp": seal, "cwd": n.get("folder") or "",  # the question it runs under, and the folder its artifact was left in
                    **({"command": [part.replace("{project}", root) for part in gate["command"]]} if "command" in gate else
                       {"brief": refuter_brief(cfg, n, gate)})})
    return {"pending": out[:20]}


def refuter_brief(cfg, n, gate):
    """What a refuter round is given: the claim and nothing about how it was reached."""
    return "\n".join([
        "You are checking one claim from another round of this research. You have not seen how it was reached. Do not trust it.",
        *[f"{k.upper()}: {cfg[k]}" for k in ("target", "goal") if isinstance(cfg.get(k), str) and cfg[k].strip()],
        "",
        f'THE CLAIM [{n["id"]}]: {n.get("text", "")}',
        *([n["body"][:1500]] if n.get("body") else []),
        *([f'Its artifact, in your working folder: {n["artifact"]}'] if n.get("artifact") else ["It named no artifact. Work from the claim itself."]),
        "",
        f'YOUR JOB ({gate["name"]}): {gate["refute"]}',
        "Try to kill the claim with something concrete: a counterexample, a run that fails, an earlier source you can cite.",
        "An opinion is not a refutation, and neither is a reason it might be wrong. If you cannot kill it, say what you tried.",
        "Change nothing in the folder. End with exactly one of these lines:",
        "Verdict: refuted - <the concrete evidence>",
        "Verdict: stands - <what you tried and where you looked>"]) + "\n"


def assess(n, gates, seal):
    """Where a claim stands against the gates: how many it has passed in order, and what is stopping it."""
    if not gates or n.get("status") not in ("supported", "reproduced"):  # a frame or a dead end is not a claim
        return {"standing": 0, "state": None, "at": None}
    for count, gate in enumerate(gates):
        if "command" in gate and not n.get("artifact"):  # a command has to be run on something; a refuter can work from the claim
            return {"standing": count, "state": "unchecked", "at": gate["name"]}
        got = (n.get("gates") or {}).get(gate["name"])
        if not got or got.get("fp") != seal or (got.get("checked") or "") != (n.get("artifact") or ""):  # never run, or run on a different question or artifact
            return {"standing": count, "state": "waiting", "at": gate["name"]}
        if got.get("verdict") == "error" and (got.get("tries") or 1) < GATE_TRIES:  # a timeout or a harness hiccup is not the claim's fault: it runs again
            return {"standing": count, "state": "waiting", "at": gate["name"]}
        if got.get("verdict") != "passed":
            return {"standing": count, "state": "failed" if got.get("verdict") == "failed" else "error", "at": gate["name"]}
    return {"standing": len(gates), "state": "established", "at": None}


def queue(claims):
    """The frames the loop will take, in order: the person's marks first, then whatever is furthest through the gates
    (a claim stopped at its first gate is no further along than a fresh frame), then the highest score, then the oldest.
    A frame whose rounds are spent is not in it."""
    return sorted((n for n in claims if n["frame"] and not n["stalled"]), key=lambda n: (-n["priority"], -n["standing"], -n["budget"], n["born"]))


def merged(events, type):
    """Latest state of each thing of this type, keyed by id (or by key). "_i" is where it last changed."""
    out = {}
    for i, e in enumerate(events):
        if e["type"] == type:
            k = e.get("id") or e.get("key") or f"_{i}"
            # "asker" is whoever opened it: a later answer from the other side must not change whose question it is
            out[k] = {**out.get(k, {}), **e, "_i": i, "asker": out.get(k, {}).get("asker") or ("you" if e.get("source") == "you" else "agent")}
    return out


THREAD_KEYS = ("ts", "agent", "source", "text", "kind", "status", "area", "scope", "snapshot", "body", "conditions", "next", "evidence", "links", "priority",
               "known", "origin", "artifact", "score", "audited", "gate", "verdict", "detail", "checked", "found_in")
VERDICT_KEYS = ("verdict", "detail", "checked", "fp", "ts")
MARK_KEYS = ("priority", "status", "score", "audited", "audit_fp", "audit_artifact")
STARTED = re.compile(r"Iteration \d+ started")


def research(project, query="", area=""):
    """Current research threads with their attributed update history. A thread is a frame (a place to look) until a round
    settles it; then it is a claim, or ruled out. Each comes back with where it stands: against the gates ("standing",
    "state", "at"), as work ("frame", "attempts", "budget", "stalled"), and whether the person's audit still holds."""
    cfg = config(check_name(project))
    gates, seal = gates_of(cfg), fingerprint(project, cfg)
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
            if "kind" not in e:  # the person's mark or status change alone: it changes only that, and does not make the thread look recently worked on
                if old:
                    nodes[e["id"]] = {**old, **{k: e[k] for k in MARK_KEYS if k in e}, "history": [*old["history"], update]}
                    if e.get("priority") is True:
                        attempts[e["id"]] = 0  # Pursue puts it at the front with its rounds to spend again
                continue
            # Claimed again (an artifact named, even the same one) or reworded: a different claim, so every gate and the audit are still to come.
            fresh = "artifact" in e or bool(e.get("text") and old.get("text") and plain(e["text"]) != plain(old["text"]))
            kept = {k: v for k, v in old.items() if k not in ("gates", "audited", "audit_fp", "audit_artifact")} if fresh else old
            nodes[e["id"]] = {**kept, **e, "_i": i, "born": old.get("born", i), "history": [*old.get("history", []), update]}
    out = list(nodes.values())
    for n in out:
        n["priority"] = bool(n.get("priority"))
        n.update(assess(n, gates, seal))
        n["audited"] = bool(n.get("audited")) and n.get("audit_fp") == seal and n.get("audit_artifact", "") == (n.get("artifact") or "")
        n["attempts"], n["budget"] = attempts.get(n["id"], 0), n["score"] if isinstance(n.get("score"), int) else 3
        settled = n.get("status") in ("contradicted", "superseded")
        n["frame"] = not settled and (n.get("status") == "open" or n["state"] == "failed" or (n["priority"] and n["state"] == "unchecked"))
        n["stalled"] = n["frame"] and n["attempts"] >= n["budget"]
    if area:
        out = [n for n in out if n.get("area") == area]
    if query:
        q = query.lower()
        out = [n for n in out if q in " ".join(str(n.get(k) or "") for k in ("id", "text", "body", "conditions", "next", "area", "scope", "snapshot")).lower()]
    out.sort(key=lambda n: (n["priority"], n["_i"]), reverse=True)  # what the person marked first, then the most recently worked on
    for n in out:
        n.pop("_i", None)
    return out


def thread_lines(n):
    """One research thread as a fresh round should see it: the claim, where it applies, its next test and evidence."""
    out = [f'- [{n["id"]}] {n.get("kind", "claim")} / {n.get("status", "open")}: {n.get("text", "")}' + (f' (area: {n["area"]})' if n.get("area") else "")]
    if n.get("scope") or n.get("snapshot"):
        out += ["  Context: " + "; ".join(f"{k}={n[k]}" for k in ("scope", "snapshot") if n.get(k))]
    if n.get("next"):
        out += [f'  Next test: {n["next"][:300]}']
    if n.get("evidence"):
        out += ["  Evidence: " + "; ".join(x.get("ref", "") for x in n["evidence"][:3])]
    return out


def threads(claims, limit):
    """The person's marked threads first under their own heading, then other open threads, at most `limit` in all.
    A marked thread stops being work once it is ruled out or dropped."""
    marked = [n for n in claims if n.get("priority") and n.get("status") not in ("superseded", "contradicted")][:limit]
    rest = [n for n in claims if not n.get("priority") and n.get("status") not in ("superseded", "contradicted")][:max(0, limit - len(marked))]
    out = ["MARKED AS PRIORITY BY THE PERSON (work on these before any unmarked thread)"] if marked else []
    for n in marked:
        out += thread_lines(n)
    out += ["Other open threads:"] if marked and rest else []
    for n in rest:
        out += thread_lines(n)
    return out


def steering(cfg, events):
    """What the person has said that every round must follow: their standing direction, then what they told earlier rounds."""
    out, direction = [], cfg.get("direction")
    if isinstance(direction, str) and direction.strip():
        out += ["THE PERSON'S CURRENT DIRECTION (it outranks your own choice of next step; follow it until they change it)", direction.strip(), ""]
    said = [e for e in events if e["type"] == "activity" and e.get("source") == "you" and e.get("harness") == "terminal"][-5:]
    if said:
        out += ["WHAT THE PERSON SAID IN THE TERMINAL TO EARLIER ROUNDS (newest last; it still applies unless the direction above supersedes it)"]
        for e in said:
            words = (e.get("body") or e.get("text") or "").replace("\n", " ")
            out.append(f'- {e.get("ts", "")[:16].replace("T", " ")} to {e.get("key") or "a worker"}: ' + words[:600] + (" ..." if len(words) > 600 else ""))
        out.append("")
    # An unattended loop can circle one idea all night. Rounds that end with nothing recorded are the sign.
    quiet = 0
    for e in reversed(events):
        if e["type"] == "research" and e.get("source") != "you":
            break
        quiet += e["type"] == "activity" and bool(re.match(r"Iteration \d+ (finished|ended badly)", e.get("text") or ""))
    if quiet >= 3:
        out += [f"THE LAST {quiet} ROUNDS RECORDED NOTHING NEW (no result, ruled-out approach or lead).",
                "Do not continue the same line of work. Name three different angles in your handoff and try the one least like the recent rounds.", ""]
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


LOOP_DEFAULTS = {"rounds": 4, "timeout_min": 0, "reply_wait": 120, "keep_background": False, "tools": "recipe"}  # research: no time limit
WIDE_TOOLS = ["WebSearch", "WebFetch", "Task", "Agent"]  # web and subagents, by name: permission checks stay on and connectors stay refused
LOOP_RULES = {  # what each loop setting may be, and how to say so when it is not
    "recipe": (lambda v: isinstance(v, str) and bool(NAME.match(v)), "a recipe name from launchers.json"),
    "model": (lambda v: isinstance(v, str) and bool(MODEL.match(v)), "1 to 80 letters, digits or . : - [ ] / @"),
    "rounds": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 500, "a whole number from 0 (until stopped) to 500"),
    "timeout_min": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1440, "minutes from 0 (no limit) to 1440"),
    "reply_wait": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3600, "whole seconds from 0 (never wait) to 3600"),
    "keep_background": (lambda v: isinstance(v, bool), "true or false"),
    "tools": (lambda v: v in ("recipe", "wide"), '"recipe" (the recipe\'s own list) or "wide" (also web search, web fetch and subagents)'),
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
    """A recipe's own rounds, time limit, reply wait and background setting, with Hades's defaults where it sets none."""
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


def with_tools(command):
    """A Claude Code recipe's command with web search, web fetch and subagents added to its allow-list, for this launch only.
    Another program's command is returned as it is: its tools are whatever its own recipe says."""
    out = list(command)
    if not is_claude(out):
        return out
    for i, part in enumerate(out):
        if part in ("--allowedTools", "--allowed-tools"):
            return out[:i + 1] + [t for t in WIDE_TOOLS if t not in out] + out[i + 1:]
        if part.startswith(("--allowedTools=", "--allowed-tools=")):
            return out[:i] + [part + "".join("," + t for t in WIDE_TOOLS if t not in part.split("=", 1)[1].split(","))] + out[i + 1:]
    return out + ["--allowedTools", *WIDE_TOOLS]


def launchers():
    """Each recipe as the page offers it: label, its own model, the models to choose from, live or batch, and the command."""
    try:
        from scripts.loop import can_converse
    except ImportError:
        can_converse = lambda command: False
    out = {}
    for k, v in recipes().items():
        models = v.get("models") if isinstance(v.get("models"), list) else None
        out[k] = {"label": str(v.get("label") or k), "model": recipe_model(v["command"]),
                  "models": [str(x) for x in models if isinstance(x, str)] if models is not None else ["fable", "opus", "sonnet", "haiku"] if is_claude(v["command"]) else [],
                  "interactive": v.get("interactive") is not False and can_converse(v["command"]), "command": v["command"],
                  "takes_tools": is_claude(v["command"]), **recipe_defaults(v)}
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
                            "interactive": r["interactive"], "model": r.get("model", ""), "settings": r.get("settings", {})})
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
    if settings["tools"] == "wide" and is_claude(base):
        base = with_tools(base)
    else:
        settings["tools"] = "recipe"  # another program's tools are whatever its own recipe says
    settings["model"], rounds = recipe_model(base), settings["rounds"]
    program = resolve_program(base)  # "claude" may be claude.cmd on Windows; the loop gets the path that will start
    from scripts.loop import can_converse  # a direct `claude -p` round can be watched and messaged while it runs
    interactive = recipe.get("interactive") is not False and can_converse(base)
    agent = f"{name}-1"  # the name its round notes are kept under, so a later launch of the same recipe remembers them
    with LOCK:
        alive = [k for k, r in RUNNING.items() if r["proc"].poll() is None]
        working = [a for (p, a) in alive if p == project] or [a for a in worked(project) if orphan(project, a)]
        if working:  # two loops on one project would share a folder, a log and a brief
            raise ValueError(f"a loop is already running on this project ({working[0]}); stop it first")
        if len(alive) >= MAX_RUNNING:
            raise ValueError(f"at most {MAX_RUNNING} loops may run at once; {len(alive)} are running")
        work = ROOT / "agent-work" / project / agent
        fresh = not work.exists()
        work.mkdir(parents=True, exist_ok=True)
        seed = config(project).get("agent_files")
        if fresh and seed and Path(seed).is_dir():  # what the agent starts with: a copy of the target, a spec
            shutil.copytree(seed, work, dirs_exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        env["PROJECT_VIEW_URL"] = f"http://127.0.0.1:{PORT}"
        command = [sys.executable, str(ROOT / "scripts" / "loop.py"), project, "--agent", agent, "--harness", str(recipe.get("label") or name),
                   "--max-iterations", str(rounds), "--timeout-min", f'{settings["timeout_min"]:g}', "--reply-wait", str(settings["reply_wait"]),
                   *(["--keep-background"] if settings["keep_background"] else []), *(["--interactive"] if interactive else []), "--"] + program
        session = uuid.uuid4().hex
        env["HADES_LAUNCH_SESSION"] = session
        env["HADES_DETAILS_DB"] = str((ROOT / "agent-work" / ".hades" / "invocations.sqlite3").resolve())
        with open(work / "loop.log", "ab") as log:
            console = open(work / "loop.log", "rb")
            try:
                if not os.path.samestat(os.fstat(log.fileno()), os.fstat(console.fileno())):
                    raise OSError("terminal log changed while opening it")
                start_offset = console.seek(0, os.SEEK_END)
                proc = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, **own_group())
            except BaseException:
                console.close()
                raise
        if interactive and not WINDOWS:
            os.set_blocking(proc.stdin.fileno(), False)  # a worker that stops reading must not stall the server; tell() waits with a deadline
        (work / "loop.pid").write_text(f"{proc.pid}\n")  # survives a restart of this server, so the loop can still be found
        previous = RUNNING.get((project, agent))
        if previous:
            previous["console"].close()
        RUNNING[(project, agent)] = {"proc": proc, "recipe": name, "session": session, "console": console, "interactive": interactive, "pipe": threading.Lock(),
                                     "model": settings["model"], "settings": settings,
                                     "start_offset": start_offset, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    return {"started": [agent], "rounds": rounds, "recipe": name, "model": settings["model"], "settings": settings}


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


def write_within(stream, payload, seconds):
    """Write and flush on a helper thread, waiting at most `seconds`. Returns (bytes not known to be written, the thread if
    it is still blocked, else None)."""
    failed = []

    def write():
        try:
            stream.write(payload)
            stream.flush()
        except (OSError, ValueError) as e:
            failed.append(e)

    worker = threading.Thread(target=write, daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if failed:
        raise OSError(failed[0])
    return (payload, worker) if worker.is_alive() else (b"", None)


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


def tell(data):
    """Pass the person's message to a launched worker. Its loop hands it to the Claude session it is running."""
    project, agent, text = check_name(data.get("project")), data.get("agent"), data.get("text")
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 4000:
        raise ValueError("Enter a message of up to 4,000 characters.")
    with LOCK:
        record = RUNNING.get((project, agent)) if isinstance(agent, str) else None
        if not record or record["session"] != data.get("session") or not record["interactive"]:
            raise ValueError("This worker has no live Claude session to message.")
    # Written outside the server-wide lock and with a deadline: a worker that has stopped reading must not freeze every request
    payload, deadline = (json.dumps({"text": text.strip()}) + "\n").encode(), time.time() + 5
    with record["pipe"]:  # one message at a time per worker, so two never interleave in its pipe
        try:
            if record["proc"].poll() is not None:
                raise OSError
            if record.get("stuck") and record["stuck"].is_alive():  # an earlier message is still waiting to go in
                raise TimeoutError
            if WINDOWS:  # Windows pipes have no select: a writer thread waits instead, and one that hangs marks the worker
                payload, record["stuck"] = write_within(record["proc"].stdin, payload, deadline - time.time())
            else:
                fd = record["proc"].stdin.fileno()
                while payload and select.select([], [fd], [], max(0, deadline - time.time()))[1]:
                    try:
                        payload = payload[os.write(fd, payload):]
                    except BlockingIOError:
                        pass
        except TimeoutError:
            payload = b"unsent"
        except (OSError, ValueError):  # a closed pipe, or a worker that has exited
            raise ValueError("This worker has finished. Start it again to continue.") from None
        if payload:
            raise ValueError("This worker is not reading messages right now. Try again in a moment.")
    # Kept in the log, so every later round hears it too, not only the session that was running
    text = text.strip()
    ingest({"project": project, "type": "activity", "source": "you", "harness": "terminal", "key": agent, "text": text,
            **({"body": text} if len(text) > 2000 else {})})
    return {"sent": True}


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


VIEWS = {  # the pages a project can have besides the built-in ones
    "note": 'one block of prose per key, replaced each time. Send {"section", "key", "body": "<text>"}',
    "table": 'one row per key. Send {"section", "key", "data": {"<column>": value, ...}}',
}
STARTER_SECTIONS = [{"id": "now", "title": "Project brief", "view": "note"}]


def clean_section(s):
    if not isinstance(s, dict) or not NAME.match(str(s.get("id") or "")):
        raise ValueError("a section needs an id made of letters, digits, dots, dashes or underscores")
    out = {"id": str(s["id"]), "title": str(s.get("title") or s["id"])[:40], "view": s.get("view") if s.get("view") in VIEWS else "note"}
    if isinstance(s.get("columns"), list):
        out["columns"] = [str(x)[:40] for x in s["columns"][:12]]
    return out


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
             **({"agent_files": str(data["agent_files"])[:500]} if data.get("agent_files") else {}),
             "sections": STARTER_SECTIONS}, indent=2) + "\n")
        load(project)
    return public_config(project)


def set_project(data, create=False):
    """Set the goal, summary, rules, target, direction, pause, loop settings or sections."""
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
        if isinstance(data.get("sections"), list):
            cfg["sections"] = [clean_section(s) for s in data["sections"][:12]]
        if data.get("section") is not None:
            one = clean_section(data["section"])
            rest = [s for s in cfg.get("sections", []) if isinstance(s, dict) and s.get("id") != one["id"]]
            was = next((i for i, s in enumerate(cfg.get("sections", [])) if isinstance(s, dict) and s.get("id") == one["id"]), len(rest))
            cfg["sections"] = (rest[:was] + [one] + rest[was:])[:12]
        (PROJECTS / project).mkdir(parents=True, exist_ok=True)
        save_config(project, cfg)
        load(project)
    return public_config(project)


# ---------- the brief: everything a round with no memory needs, generated from the project ----------

def brief(project, agent):
    """What one round of the loop is given, as text."""
    return plan(project, agent)[0]


def plan(project, agent):
    """(the brief, the id of the frame it gives the round or None). The model does the research; the harness keeps the
    record from its closing handoff, so every line here is about the work."""
    cfg, events = config(check_name(project)), list(load(project))
    update = {}
    for e in events:
        if e["type"] == "item" and e.get("section") == "now" and e.get("key") in ("Current update", "Current decision"):
            update = e
    notes = [e for e in events if e["type"] == "item" and e.get("section") == "round-notes" and e.get("agent") == agent and e.get("body")]
    claims, gates = research(project), gates_of(cfg)
    ruled = [f'- {n.get("text", "")[:300]}' + (f' (why: {n["conditions"][:200]})' if n.get("conditions") else "")
             for n in claims if n.get("status") == "contradicted"][:20]
    live = [n for n in claims if n.get("status") not in ("contradicted", "superseded")]
    line = queue(claims)
    frame = line[0] if line else None  # while there is a frame, the round settles it; when there is none, the round widens
    if gates:
        root, hint = str((PROJECTS / project).resolve()), str(cfg.get("artifact") or "").strip()[:300]
        commands = [g for g in gates if "command" in g]
        rule = [f"A RESULT COUNTS ONLY WHEN IT PASSES {'THE GATES, in this order' if len(gates) > 1 else 'THE GATE'}: " + ", ".join(g["name"] for g in gates) + ".",
                "- They are run for you after the round, on every Results line. End a line with [check: <artifact>] to name what should be checked"
                + (f": {hint}" if hint else "") + "." + (" A command gate cannot run without one." if commands else ""),
                *[f'- {g["name"]}: ' + (shlex.join(part.replace("{project}", root) for part in g["command"]) + ("" if any("{artifact}" in part for part in g["command"]) else " <artifact>")
                                        if "command" in g else "a separate round that sees only your claim and tries to refute it.") for g in gates],
                *(["- You may run a gate's command yourself while you work."] if commands else []),
                "- Never edit the gates or what they read. If you think one is wrong, say so in your Handoff and work on something else."]
    else:
        rule = ["Nothing re-checks your results, so every result you report is a claim. Say what you observed and how to see it again."]
    leads = "End your handoff with a Leads: line per lead worth chasing, most promising first, each with the quickest test that would confirm or kill it and [score: 1-5] for the rounds it is worth."
    out = [*steering(cfg, events),
           *(["YOUR CLOSING NOTE FROM YOUR LAST ROUND (your only memory of it)", notes[-1]["body"][-2000:], ""] if notes else []),
           *[f"{k.upper()}: {cfg[k]}" for k in ("target", "goal", "rules") if isinstance(cfg.get(k), str) and cfg[k].strip()],
           *([] if str(cfg.get("goal") or "").strip() else
             ["GOAL: none yet. Work from the direction above."] if str(cfg.get("direction") or "").strip() else
             ["GOAL: none yet. Do not invent work: say in your handoff that the project has no goal, and stop."]),
           "",
           *(["Settle one frame this round. Your closing handoff is recorded on the dashboard for you; report nothing else.",
              "- The frame is named at the end of this brief. Read what was already tried, below, first."]
             if frame else
             ["Follow your nose this round: nothing is queued, so find the places worth looking. Your closing handoff is recorded on the dashboard for you; report nothing else.",
              "- Go where the target looks most surprising, broken or unexplained, and keep following what you find rather than finishing a plan.",
              "- Breadth beats proof this round: a quick look at several things is worth more than settling one.",
              "- Run things in parallel where you can (background jobs, subagents if you have them).",
              "- Do not spend the round gathering evidence for what you already believe. The ruled-out list below is dead ground.",
              "- " + leads]),
           "- When a Results or Ruled out line settles a thread listed below, start that line with the thread's id in brackets, like [k12].",
           "- Nobody is watching this round. If something only the person could decide comes up, do not wait: take the cautious option, say what you assumed on the Assumed line, and carry on.",
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
        out += ["", "ASSUMPTIONS IN FORCE (earlier rounds made them in the person's place; keep to them, and do not raise them again)",
                *[f'- {words.get(q["id"], "")[:300]}' for q in waiting]]
    if update and (update.get("agent") != agent or not notes):
        out += ["", "WHERE THINGS STAND (the dashboard's current update)", (update.get("body") or update.get("text") or "")[-2000:]]
    title = lambda n: f'- [{n["id"]}] {n.get("text", "")[:200]}' + (" (the person's priority)" if n["priority"] else "")
    established = sorted((n for n in live if n["state"] == "established"), key=lambda n: -n["audited"])
    if established:
        out += ["", f"ESTABLISHED (passed {'every gate' if len(gates) > 1 else 'the gate'}: build on these, do not redo them)",
                *[f'- [{n["id"]}] {n.get("text", "")[:300]}' + (f' (artifact: {n["artifact"]})' if n.get("artifact") else "") for n in established[:20]]]
    stopped, by = sorted((n for n in live if n["state"] in ("failed", "error")), key=lambda n: -n["standing"]), {g["name"]: g for g in gates}
    if stopped:
        out += ["", "CLAIMED, BUT STOPPED AT A GATE (repair it and claim it again starting the Results line with its id, or rule it out)"]
        for n in stopped[:8]:
            got = n["gates"][n["at"]]
            said = "\n".join("    " + row for row in (got.get("detail") or "").strip().splitlines()[-8:])[-600:]
            through = [g["name"] for g in gates[:n["standing"]]]
            out += [f'- [{n["id"]}] {n.get("text", "")[:300]}',
                    *([f'  Passed: {", ".join(through)}.'] if through else []),
                    f'  Stopped at {n["at"]} ({"could not run" if n["state"] == "error" else "refuted" if "refute" in by[n["at"]] else "failed"}) on '
                    f'{got.get("checked") or n.get("artifact") or "its artifact"}' + (":\n" + said if said.strip() else ".")]
    if frame:  # the rest of the record, as titles: enough to build on and to avoid proposing it again
        claimed = [n for n in live if n.get("status") != "open" and n["state"] in (None, "unchecked", "waiting") and n is not frame]
        out += ["", "CLAIMED, NOT CHECKED (another round's word, nothing more)" if gates else "CLAIMED (another round's word, nothing more)", *map(title, claimed[:8])] if claimed else []
        later = [n for n in line if n is not frame]
        out += ["", "ALSO QUEUED (for later rounds, in this order; not this round's work, and not to be proposed again as leads)", *map(title, later[:8]),
                *([f"  and {len(later) - 8} more"] if len(later) > 8 else [])] if later else []
    else:
        open_threads = threads([n for n in claims if n["state"] not in ("established", "failed", "error")], 8)
        out += ["", "RESEARCH THREADS (leads, not established)", *open_threads] if open_threads else []
    spent = [n for n in live if n["stalled"]]
    out += ["", "SET ASIDE (their rounds are spent; come back to one only as a new lead with a new angle, and say what is new)", *map(title, spent[:5])] if spent else []
    out += ["", "RULED OUT (do not retry without a new reason, and say what is different if you do)", *ruled] if ruled else []
    if frame:
        mark = f'[{frame["id"]}]'
        out += ["", f'THIS ROUND\'S FRAME (round {frame["attempts"] + 1} of {frame["budget"]} on it' + ("; the person marked it" if frame["priority"] else "") + ")",
                *thread_lines(frame),
                *([f'It is a claim that was stopped at the gate "{frame["at"]}"; what the gate said is above. Answer that.'] if frame["state"] == "failed" else
                  ["Work as if this frame pays off. Your job is to produce the result, not to judge whether one is likely."] if gates else []),
                "Settle it this round, one way or the other:",
                f"- a Results line that starts with {mark}" + (" and ends with [check: <artifact>], or" if gates else ", or"),
                f"- a Ruled out line that starts with {mark} and says what you examined and why it is not there, so the ground is not covered twice.",
                "If you cannot settle it, say what blocked you, and make your Next line the quickest test for this same frame.",
                "A new place to look goes on a Leads line; do not go after it this round."]
    return "\n".join(out) + "\n", frame["id"] if frame else None


def status(project, base):
    """Where a project stands, as plain text: is it running and on what, how far each claim got, what is next. This is
    what an assistant reads first when the person asks it what is going on."""
    cfg, events = config(check_name(project)), list(load(project))
    if not (PROJECTS / project).is_dir():
        raise ValueError("no such project")
    claims, gates = research(project), gates_of(cfg)
    live = [n for n in claims if n.get("status") not in ("contradicted", "superseded")]
    by = lambda state: [n for n in live if n["state"] == state]
    line, ago = queue(claims), _ago
    runs = launched(project)
    alive = next((r for r in runs if r["alive"]), None)
    marks = [(i, e) for i, e in enumerate(events) if e["type"] == "activity" and re.match(r"(Iteration \d+ (started|finished|ended badly)|Loop stopped|A round ended without)", e.get("text") or "")]
    began = next(((i, e) for i, e in reversed(marks) if STARTED.match(e["text"])), None)
    stopped = next((e for _, e in reversed(marks) if e["text"].startswith("Loop stopped")), None)
    silent = next((i for i, e in reversed(marks) if e["text"].startswith("A round ended without")), -1)
    doing = ""
    if began:
        i, last = began
        over = any(j > i and not e["text"].startswith("A round ended without") for j, e in marks)  # finished, ended badly, or the loop stopped
        given = (last.get("data") or {}).get("frame")
        on = next((n for n in claims if n["id"] == given), None)
        doing = ("Last round: " if over or not alive else "Round in progress: ") + f'round {last["text"].split()[1]} of this run, by {last.get("agent") or "unknown"}, started {ago(last.get("ts"))}, ' \
            + (f'settling [{on["id"]}] {on.get("text", "")[:120]}' if on else "widening (nothing was queued, so it is looking for places to look)")
    title = lambda n: f'- [{n["id"]}] {n.get("text", "")[:200]}'
    out = [f'Hades project "{project}"   page: {base}/#/{project}   record: {base}/api/research?project={project}',
           "Goal: " + (str(cfg.get("goal") or "").strip() or "none set"),
           *(["Direction: " + cfg["direction"].strip()[:400]] if isinstance(cfg.get("direction"), str) and cfg["direction"].strip() else []),
           "Loop: " + ("paused by the person; " if cfg.get("paused") else "")
           + (f'running as {alive["agent"]} ({alive["recipe"]}' + (f', {alive["model"]}' if alive.get("model") else "") + f'), up to {alive["settings"].get("rounds") or "unlimited"} rounds' if alive else
              "not running" + (f' ({stopped["text"]}, {ago(stopped.get("ts"))})' if stopped else "")),
           *([doing] if doing else []),
           *(["PROBLEM: " + config_problem(project) + ". The loop holds until it is fixed."] if config_problem(project) else []),
           "Gates: " + (" -> ".join(g["name"] + (" (refuter)" if "refute" in g else "") for g in gates) + (" [built in; the project set none]" if "gates" not in cfg else "")
                        if gates else "none: every result is only a claim"),
           f'Counts: {len(by("established"))} established, {len(by("failed")) + len(by("error"))} stopped at a gate, {len(by("waiting"))} waiting for a gate, '
           f'{len([n for n in live if n.get("status") in ("supported", "reproduced") and n["state"] in (None, "unchecked")])} claimed and unchecked, '
           f'{len([n for n in claims if n.get("status") == "contradicted"])} ruled out, {len(line)} queued, {len([n for n in live if n["stalled"]])} set aside']
    if by("established"):
        out += ["", "ESTABLISHED (through every gate)", *[title(n) + (f' (artifact: {n["artifact"]})' if n.get("artifact") else "") + (" [audited by the person]" if n["audited"] else "")
                                                         for n in by("established")[:20]]]
    halted = sorted(by("failed") + by("error"), key=lambda n: -n["standing"])
    if halted:
        out += ["", "STOPPED AT A GATE"]
        for n in halted[:10]:
            got = n["gates"][n["at"]]
            said = (got.get("detail") or "").strip().splitlines()
            out += [title(n), f'  passed {n["standing"]} of {len(gates)}; {n["at"]} {"could not run" if n["state"] == "error" else "said no"}' + (f': {said[-1][:240]}' if said else "")]
    for heading, some in (("WAITING FOR A GATE (it runs after the next round)", by("waiting")),
                          ("CLAIMED AND UNCHECKED (a round's word; no gate could run on it)", [n for n in live if n.get("status") in ("supported", "reproduced") and n["state"] in (None, "unchecked")])):
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
    at, note = next(((i, e) for i, e in reversed(list(enumerate(events))) if e["type"] == "item" and e.get("section") == "now" and e.get("key") in ("Current update", "Current decision")), (-1, None))
    if note:
        out += ["", f'LATEST HANDOFF ({note.get("agent") or "unknown"}, {ago(note.get("ts"))})',
                *(["(A later round ended without a Handoff block, so nothing was recorded from it; this is the one before.)"] if silent > at else []),
                (note.get("body") or note.get("text") or "")[:1500]]
    elif silent >= 0:
        out += ["", "No handoff yet: the last round ended without a Handoff block, so nothing was recorded from it."]
    out += ["", f"What you can do from here: {base}/AGENTS.md"]
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

    def do_GET(self):
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
                self.reply(200, gates_pending(arg("project")))
            elif url.path == "/api/research":
                self.reply(200, research(arg("project"), arg("q"), arg("area")))
            elif url.path == "/api/brief":
                text, frame = plan(arg("project"), arg("agent")[:80])
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

    def own_page(self, header):
        """True for a request made by this server's own page, open on this machine. Other sites cannot send these headers."""
        host = self.headers.get("Host", "")
        port = self.server.server_port
        allowed = {f"{name}:{port}" for name in ("localhost", "127.0.0.1", "[::1]")}
        if port == 80:
            allowed.update(("localhost", "127.0.0.1", "[::1]"))
        return (host in allowed and
                self.headers.get("Origin") == "http://" + host and
                self.headers.get(header) == "1" and
                self.headers.get_content_type() == "application/json")

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 200000:
                return self.reply(413, {"error": "request is larger than 200 kB"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            path = urlparse(self.path).path
            if path == "/api/terminal/send":  # the message reaches an agent that can run tools: this server's own page only
                if not self.own_page("X-Hades-Terminal"):
                    raise PermissionError("Open Hades on localhost to message this worker.")
                out = tell(data)
            elif path == "/api/events":
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


def starter():
    """The first project on a fresh install, with no goal yet. The person types what to research on the page."""
    return set_project({"project": "workspace", "goal": "", "sections": STARTER_SECTIONS}, create=True)


def serve():
    print(f"Hades on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--dashboard" in sys.argv and not project_ids():
        starter()
    serve()
