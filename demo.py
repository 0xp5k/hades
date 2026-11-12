#!/usr/bin/env python3
"""Run HAUS with two live demo projects, so there is something real to look at.

  python3 demo.py

  lr-schedule-search   AI research. Agents hill-climb a learning-rate schedule; the verifier retrains to check.
  parser-fuzz          Security. Agents fuzz a toy parser; the verifier replays each crash to check.

Nothing here is faked except who the agents are: they are threads pretending to be harnesses on other
machines. They report through the real port, the scores are really computed, and verifier.py really
re-scores every run. One agent in each project is deliberately unreliable, to show what that looks like.

A third project, tv-parser-setup, starts in its setup phase to show how a new project goes from nothing to running:
a stand-in survey agent writes the Understanding page and proposes the setup, and waits for you to approve it on the page.

Each start wipes and recreates those three projects. Other projects under projects/ are left alone.
"""
import json
import math
import random
import shutil
import sys
import threading
import time
import urllib.request
from collections import Counter

import server
import verifier

sys.path.insert(0, str(server.ROOT / "demo"))
import fuzz_target  # noqa: E402
import lr_score  # noqa: E402

URL = f"http://127.0.0.1:{server.PORT}"
LR, FUZZ = "lr-schedule-search", "parser-fuzz"
TOOLS = {"Claude Code": ("Read", "Edit", "Bash"), "Codex CLI": ("read_file", "apply_patch", "shell"), "Custom bash loop": ("cat", "sed", "bash")}
MAX_EVENTS = 5000  # the demo agents go quiet after this, so a forgotten tab does not grow without limit


def call(path, **body):
    data = json.dumps(body).encode() if body else None
    with urllib.request.urlopen(urllib.request.Request(URL + path, data, {"Content-Type": "application/json"}), timeout=10) as r:
        return json.loads(r.read())


def step(me, tool, text, rng):
    """One visible unit of work: say what you are doing, record the tool call, take some time."""
    call("/api/events", type="activity", text=text, **me)
    call("/api/events", type="tool", text=tool, ok=rng.random() > (0.12 if tool.lower() in ("bash", "shell") else 0.02),
         ms=round(rng.lognormvariate(6.5 if tool.lower() in ("bash", "shell") else 4.5, 0.6)), **me)
    time.sleep(rng.uniform(5, 9))


def wait_for_room(project):
    """Wait while the project is on hold (paused, or waiting on the person), or once the demo has produced enough."""
    while True:
        p = next((p for p in call("/api/projects") if p["id"] == project), None)
        if not p or (not p.get("hold") and p["count"] <= MAX_EVENTS):
            return
        time.sleep(4 if p.get("hold") else 60)


# ---------- project 1: learning-rate schedule search ----------

LR_AREAS = {"Warmup length": "Share of training spent ramping the rate up", "Peak learning rate": "The highest rate the schedule reaches",
            "Decay shape": "Cosine, linear, constant or inverse-sqrt", "Training length": "Whether results hold beyond 120 steps",
            "Batch size": "Whether the best schedule changes with batch size"}


def describe(a):
    return f"{a['decay']} decay, peak {a['peak']:g}, warmup {a['warmup']:.0%}"


def propose(rng, best):
    a, area = dict(best), rng.choice(list(LR_AREAS)[:3])
    if area == "Warmup length":
        a["warmup"] = round(rng.uniform(0, 0.5), 2)
    elif area == "Peak learning rate":
        a["peak"] = float(f"{min(60, max(0.001, best['peak'] * math.exp(rng.uniform(-1.2, 1.2)))):.3g}")
    else:
        a["decay"] = rng.choice([d for d in lr_score.DECAYS if d != best["decay"]])
    return a, area


def seed_lr():
    d = server.PROJECTS / LR
    d.mkdir(parents=True)
    (d / "project.json").write_text(json.dumps({
        "goal": "Find the learning-rate schedule with the lowest validation loss in a fixed 120-step training run",
        "summary": "Agents change one part of the schedule at a time. Every run is retrained by the verifier before it counts.",
        "rules": "Change one part of the schedule per run. Do not edit the scorer or the data.",
        "metric": {"name": "val_loss", "direction": "lower", "aggregate": "min", "tolerance": 0.0005, "verify": "python3 ../../demo/lr_score.py",
                   "artifact": 'a JSON object {"warmup": 0 to 0.9, "peak": above 0 up to 100, "decay": "cosine" | "linear" | "constant" | "invsqrt"}'},
        "sections": [
            {"id": "coverage", "title": "Coverage", "icon": "target", "view": "grid", "statuses": ["untouched", "partial", "explored"], "progress": True},
            {"id": "primitives", "title": "Primitives", "icon": "cube", "view": "grid", "statuses": ["suspected", "supported", "confirmed"]},
        ]}, indent=2))
    who = {"project": LR, "agent": "vm-1/claude-code", "harness": "Claude Code", "host": "10.0.0.11"}
    for area, text in LR_AREAS.items():
        server.ingest({"project": LR, "type": "item", "section": "coverage", "key": area, "text": text, "status": "untouched"})
    best, state = None, {"lock": threading.Lock(), "runs": Counter(), "decay": {}}
    for a, area in [({"warmup": 0.0, "peak": 0.005, "decay": "constant"}, "Peak learning rate"), ({"warmup": 0.0, "peak": 0.02, "decay": "constant"}, "Peak learning rate"),
                    ({"warmup": 0.0, "peak": 0.02, "decay": "cosine"}, "Decay shape"), ({"warmup": 0.2, "peak": 0.02, "decay": "constant"}, "Warmup length"),
                    ({"warmup": 0.0, "peak": 0.06, "decay": "constant"}, "Peak learning rate")]:
        ok, value, detail = lr_score.score(a)
        run = server.ingest({**who, "type": "run", "text": describe(a), "score": value, "artifact": a, "area": area})
        server.ingest({"project": LR, "type": "verdict", "run": run["id"], "ok": ok, "score": value, "text": detail, "agent": "verifier", "harness": "verifier.py"})
        state["runs"][area] += 1
        seen = state["decay"].setdefault(a["decay"], [value, 0])
        seen[0], seen[1] = min(seen[0], value), seen[1] + 1
        if best is None or value < best[0]:
            best = (value, a)
    state["best"], state["params"] = best
    for area, n in state["runs"].items():
        server.ingest({"project": LR, "type": "item", "section": "coverage", "key": area, "status": "partial"})
    server.ingest({**who, "type": "finding", "text": "Raising the peak rate helps far more than changing the decay shape",
                   "body": "From the first five runs: tripling the peak rate cut loss by about 0.06, while switching constant to cosine decay made it slightly worse.\n\nNext: keep raising the peak until it stops helping."})
    server.ingest({**who, "type": "item", "section": "primitives", "key": "Higher peak rate", "status": "supported",
                   "text": "Every increase in peak rate so far has lowered loss. The upper limit is not yet known."})
    server.ingest({**who, "type": "item", "section": "primitives", "key": "Warmup at low rates", "status": "suspected",
                   "text": "At a low peak rate, warmup only wastes steps. It may matter once the peak is high."})
    for key, body in LR_TOPICS:
        server.ingest({**who, "type": "topic", "key": key, "body": body, "diagram": DIAGRAMS.get(key)})
    server.ingest({**who, "type": "question", "text": "Does the best schedule still win if training runs for 500 steps? I cannot test that: the scorer fixes training at 120 steps."})
    for text, area in [("Find the peak rate where loss stops improving", "Peak learning rate"), ("Test warmup again once the peak rate is high", "Warmup length"),
                       ("Compare all four decay shapes at the best peak", "Decay shape")]:
        server.ingest({"project": LR, "type": "task", "text": text, "area": area, "status": "queued"})
    return state


def lr_agent(name, harness, host, optimistic, st):
    me, rng, tools = dict(project=LR, agent=name, harness=harness, host=host), random.Random(name), TOOLS[harness]
    time.sleep(rng.uniform(0, 4))
    # Agents add their own pages as the work takes shape. Each of the three makes a different kind.
    if optimistic:
        call("/api/project", project=LR, section={"id": "dead-ends", "title": "Dead ends", "icon": "flag", "view": "grid"})
    elif harness == "Claude Code":
        call("/api/project", project=LR, section={"id": "by-decay", "title": "By decay", "view": "table", "columns": ["Best loss", "Runs"]})
    else:
        call("/api/project", project=LR, section={"id": "summary", "title": "Summary", "view": "note"})
        call("/api/events", type="item", section="summary", key="Where things stand", **me,
             body=f"Best so far is {st['best']} with {describe(st['params'])}.\n\nOnly five runs in. Peak rate is the lever that matters so far; nothing else has moved the loss much.")
    while True:
        wait_for_room(LR)
        with st["lock"]:
            params, area = propose(rng, st["params"])
        task = call("/api/claim", **me)
        if task:
            tid, label = task["id"], task["text"]
            area = task.get("area") if task.get("area") in list(LR_AREAS)[:3] else area
        else:
            label = f"Try {describe(params)}"
            tid = call("/api/events", type="task", text=label, status="running", area=area, **me)["id"]
        step(me, tools[0], f"Starting: {label}", rng)
        step(me, tools[1], f"Editing schedule.json to {describe(params)}", rng)
        step(me, tools[2], "Training for 120 steps", rng)
        ok, value, detail = lr_score.score(params)
        with st["lock"]:
            improved = ok and value < st["best"]
            if improved:
                st["best"], st["params"] = value, params
            st["runs"][area] += 1
            runs, total = st["runs"][area], sum(st["runs"].values())
            shape = st["decay"].setdefault(params["decay"], [value, 0])
            shape[0], shape[1] = min(shape[0], value), shape[1] + 1
            ranking = sorted(st["decay"].items(), key=lambda kv: kv[1][0])
        # The unreliable agent sometimes reports a better number than it measured. The verifier catches it.
        claimed = round(value - rng.uniform(0.01, 0.04), 4) if optimistic and ok and rng.random() < 0.35 else value
        call("/api/events", type="run", text=describe(params), score=claimed, artifact=params, area=area, **me)
        call("/api/events", type="task", text=label, id=tid, status="review" if improved else "done", area=area, **me)
        call("/api/events", type="item", section="coverage", key=area, status="explored" if runs >= 12 else "partial", **me)
        call("/api/events", type="item", section="by-decay", key=params["decay"], data={"Best loss": shape[0], "Runs": shape[1]}, **me)
        if improved:
            call("/api/events", type="item", section="summary", key="Where things stand", **me,
                 body=f"Best so far is {value} with {describe(params)}, after {total} runs.\n\nBy decay shape, best first: "
                      + ", ".join(f"{name} ({best})" for name, (best, _) in ranking) + ".\n\n"
                      + ("Gains are now small; the peak rate looks close to its best value." if total > 25 else "Still improving quickly; keep pushing the peak rate."))
            call("/api/events", type="item", section="primitives", key="Best known schedule", status="supported",
                 text=f"{describe(params)} gives {value}.", body=json.dumps(params, indent=2), **me)
            call("/api/events", type="finding", text=f"New best: {value} with {describe(params)}", **me)
        elif optimistic and value > 0.5:
            call("/api/events", type="item", section="dead-ends", key=describe(params), text=f"Loss {value}. Not worth revisiting.", **me)


# ---------- project 2: fuzzing a toy parser ----------

SAMPLE = b"TV\x01" + b"\x01\x05hello" + b"\x02\x02\x01\x00" + b"\x03\x02\x01\x02hi\x02\x01\x07" + b"\x04\x00\x03abc"
EXPLAIN = {
    "IndexError in read_string": "The string reader trusts its length byte and reads past the end of the input.",
    "ZeroDivisionError in read_int": "An integer with a declared width of zero divides by zero.",
    "RecursionError in read_list": "Lists can nest without limit; a few hundred levels exhausts the stack.",
    "IndexError in read_blob": "The blob buffer is capped at 64 bytes but the copy loop is not.",
}


def mutate(rng, deep):
    if deep and rng.random() < 0.02:
        return b"TV\x01" + b"\x03\x01" * rng.randint(20, 900)
    b = bytearray(SAMPLE)
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(4)
        if op == 0:
            b[rng.randrange(3, len(b))] = rng.randrange(256)
        elif op == 1:
            del b[rng.randrange(3, len(b)):]
        elif op == 2:
            i = rng.randrange(3, len(b))
            b[i:i] = bytes([rng.randrange(1, 5), rng.randrange(256)])
        else:
            b += bytes(rng.randrange(256) for _ in range(rng.randint(1, 80)))
        if len(b) <= 3:
            b = bytearray(SAMPLE)
    return bytes(b)


def seed_fuzz():
    d = server.PROJECTS / FUZZ
    d.mkdir(parents=True)
    (d / "project.json").write_text(json.dumps({
        "goal": "Find every distinct way to crash the toy TV-format parser",
        "summary": "Agents mutate a valid sample and report inputs that crash. The verifier replays each one; only crashes that reproduce count.",
        "rules": "Only report inputs you have seen crash. An input rejected with a clean error is not a crash. Inputs are at most 4096 bytes.",
        "metric": {"name": "unique crashes", "direction": "higher", "aggregate": "unique", "verify": "python3 ../../demo/fuzz_score.py",
                   "artifact": 'a JSON object {"hex": "<the crashing input, hex-encoded>"}. A probe may instead send up to 200 inputs: {"hex": ["...", "..."]}'},
        "sections": [
            # "measured" makes this page's statuses come from what the verifier counts while replaying, not from what agents say
            {"id": "surface", "title": "Surface", "icon": "shield", "view": "grid", "statuses": ["untouched", "probed", "covered"], "progress": True,
             "unit": "inputs", "measured": {"probed": 1, "covered": 300}},
            {"id": "primitives", "title": "Primitives", "icon": "bug", "view": "board", "statuses": ["suspected", "confirmed", "rejected"]},
        ]}, indent=2))
    for fn in fuzz_target.FUNCTIONS:
        server.ingest({"project": FUZZ, "type": "item", "section": "surface", "key": fn, "text": "Parser function", "status": "untouched"})
    writer = {"project": FUZZ, "agent": "vm-4/claude-code", "harness": "Claude Code", "host": "10.0.0.14"}
    for key, body in FUZZ_TOPICS:
        server.ingest({**writer, "type": "topic", "key": key, "body": body, "diagram": DIAGRAMS.get(key)})
    server.ingest({**writer, "type": "question", "text": "Is a clean ValueError on truncated input the intended behaviour, or should it recover?"})
    for text in ["Truncate the sample at every offset", "Set every length field to its maximum", "Nest lists as deep as the input limit allows"]:
        server.ingest({"project": FUZZ, "type": "task", "text": text, "status": "queued"})
    return {"lock": threading.Lock(), "reported": set(), "inputs": 0, "crashes": {}}


def fuzz_agent(name, harness, host, sloppy, deep, st, project=None):
    project = project or FUZZ
    me, rng, tools = dict(project=project, agent=name, harness=harness, host=host), random.Random(name), TOOLS[harness]
    waiting, cursor, verdicts, mine = [], 0, {}, 0
    time.sleep(rng.uniform(0, 4))
    if deep:  # this agent builds the project three pages nobody planned for: a table, a chart and key numbers
        call("/api/project", project=project, section={"id": "crashes", "title": "Crashes", "view": "table", "columns": ["Function", "Smallest input (bytes)", "Times seen"]})
        call("/api/project", project=project, section={"id": "throughput", "title": "Throughput", "view": "chart"})
        call("/api/project", project=project, section={"id": "totals", "title": "Totals", "view": "stats"})
    while True:
        wait_for_room(project)
        if waiting:  # promote or drop primitives once the verifier has replayed the crash
            new = call(f"/api/events?project={project}&after={cursor}")["events"]
            cursor += len(new)
            verdicts.update({e["run"]: e for e in new if e["type"] == "verdict"})
            for run_id, sig in [w for w in waiting if w[0] in verdicts]:
                call("/api/events", type="item", section="primitives", key=sig, status="confirmed" if verdicts[run_id]["ok"] else "rejected", **me)
                waiting.remove((run_id, sig))
        task = call("/api/claim", **me)
        label = task["text"] if task else rng.choice(["Mutating length fields", "Truncating the sample", "Inserting extra values", "Appending random tails"])
        step(me, tools[0], f"Starting: {label}", rng)
        crashes, clean, sample = {}, None, []
        for _ in range(150):
            data = mutate(rng, deep)
            sig, seen = fuzz_target.check(data)
            if len(sample) < 40 and len(data) <= 120:
                sample.append(data.hex())
            if sig:
                crashes.setdefault(sig, data)
            elif clean is None and len(seen) > 3:
                clean = data
        step(me, tools[2], f"Ran 150 inputs, saw {len(crashes)} kinds of crash", rng)
        # Coverage is not something this agent gets to declare. It sends a sample of what it ran as a probe, and the
        # verifier counts what those inputs really reach.
        call("/api/events", type="run", probe=True, text=f"Sample of 40 inputs from: {label}", artifact={"hex": sample}, **me)
        with st["lock"]:
            fresh = [s for s in crashes if s not in st["reported"]]
            st["reported"].update(fresh[:1])
            st["inputs"] += 150
            mine += 150
            for sig, data in crashes.items():
                row = st["crashes"].setdefault(sig, [len(data), 0])
                row[0], row[1] = min(row[0], len(data)), row[1] + 1
            rows, total = {s: list(r) for s, r in st["crashes"].items() if s in crashes}, st["inputs"]
            smallest = min((r[0] for r in st["crashes"].values()), default=None)
        call("/api/events", type="item", section="throughput", key=name, value=mine, **me)
        call("/api/events", type="item", section="totals", key="Inputs tried", value=total, text="Across all agents", **me)
        call("/api/events", type="item", section="totals", key="Crash signatures seen", value=len(st["crashes"]), text="Before verification", **me)
        if smallest is not None:
            call("/api/events", type="item", section="totals", key="Smallest crashing input", value=f"{smallest} bytes", **me)
        for sig, (size, times) in rows.items():
            call("/api/events", type="item", section="crashes", key=sig, data={"Function": sig.split(" in ")[-1], "Smallest input (bytes)": size, "Times seen": times}, **me)
        if fresh:
            sig, data = fresh[0], crashes[fresh[0]]
            run = call("/api/events", type="run", text=f"{sig}, {len(data)}-byte input", score=1, artifact={"hex": data.hex()}, area=sig.split(" in ")[-1], **me)
            call("/api/events", type="item", section="primitives", key=sig, status="suspected", text=EXPLAIN.get(sig, "Unexplained crash."), body="Input (hex):\n" + data.hex(), **me)
            call("/api/events", type="finding", text=f"New crash: {sig}", **me)
            waiting.append((run["id"], sig))
        elif sloppy and clean and rng.random() < 0.3:
            # The unreliable agent sometimes reports an input that does not actually crash. The verifier rejects it.
            call("/api/events", type="run", text=f"Possible crash in read_value, {len(clean)}-byte input", score=1, artifact={"hex": clean.hex()}, area="read_value", **me)
        if task:
            call("/api/events", type="task", text=task["text"], id=task["id"], status="done", **me)


# ---------- the Understanding page: what agents wrote so a person can learn each target ----------

# Pictures for the Understanding page. Agents describe boxes and arrows; the page draws them.
DIAGRAMS = {
    "What this is": {"nodes": [
        {"id": "in", "label": "Input bytes", "note": "Whatever someone sends. It may be malformed on purpose."},
        {"id": "parse", "label": "parse", "note": "Checks the header, then reads values until the input runs out."},
        {"id": "header", "label": "read_header", "note": "Checks the letters TV and the version byte."},
        {"id": "value", "label": "read_value", "note": "Reads one tag byte and hands off to the reader for that kind of value."},
        {"id": "string", "label": "read_string", "note": "A length byte, then that many bytes of text."},
        {"id": "int", "label": "read_int", "note": "A width byte, then the number."},
        {"id": "list", "label": "read_list", "note": "A count, then that many values, which may themselves be lists."},
        {"id": "blob", "label": "read_blob", "note": "A two-byte length, then raw bytes."},
        {"id": "out", "label": "Parsed values", "note": "What a well-formed input turns into."}],
     "edges": [{"from": "in", "to": "parse"}, {"from": "parse", "to": "header", "label": "first 3 bytes"}, {"from": "parse", "to": "value", "label": "each value"},
               {"from": "value", "to": "string", "label": "tag 01"}, {"from": "value", "to": "int", "label": "tag 02"}, {"from": "value", "to": "list", "label": "tag 03"},
               {"from": "value", "to": "blob", "label": "tag 04"}, {"from": "list", "to": "value", "label": "nested"},
               {"from": "string", "to": "out"}, {"from": "int", "to": "out"}, {"from": "list", "to": "out"}, {"from": "blob", "to": "out"}]},
    "How a run is scored": {"nodes": [
        {"id": "agent", "label": "Agent", "note": "Picks a schedule to try and reports the loss it measured."},
        {"id": "schedule", "label": "Schedule", "note": "Three settings: peak, warmup and decay."},
        {"id": "train", "label": "Train 120 steps", "note": "The same small model and data every time, so results are repeatable."},
        {"id": "loss", "label": "Validation loss", "note": "Error on data the model never trained on. Lower is better."},
        {"id": "verifier", "label": "Verifier", "note": "Retrains with the agent's schedule and measures the loss itself."},
        {"id": "compare", "label": "Claim vs measured", "note": "If they differ, the run is a mismatch and the verifier's number counts."}],
     "edges": [{"from": "agent", "to": "schedule", "label": "proposes"}, {"from": "schedule", "to": "train"}, {"from": "train", "to": "loss"},
               {"from": "schedule", "to": "verifier", "label": "same schedule"}, {"from": "verifier", "to": "compare", "label": "measured"}, {"from": "loss", "to": "compare", "label": "claimed"}]},
}


LR_TOPICS = [
    ("What we are tuning", "**A learning rate is how big a step a model takes each time it corrects itself.** Too small and it learns slowly; too big and it overshoots and gets worse.\n\n"
     "A *schedule* changes that step size over the course of training instead of keeping it fixed. This project searches for the schedule that leaves a small model with the lowest error after exactly 120 training steps.\n\n"
     "Why it matters: the schedule costs nothing extra to run, yet in the first few runs it moved the error by almost a factor of two."),
    ("The three knobs", "Every schedule here is described by three settings. An agent changes one per run, so we can tell which change caused what.\n\n"
     "| Knob | What it means | Example |\n|---|---|---|\n"
     "| Peak | The largest step size the schedule ever uses | `0.06` |\n"
     "| Warmup | The share of training spent ramping up to the peak from near zero | `0.2` means the first 24 of 120 steps |\n"
     "| Decay | The shape of the ramp back down after warmup | `cosine`, `linear`, `constant`, `invsqrt` |"),
    ("How a run is scored", "**The verifier retrains the model itself and measures the error on data the model never trained on.** That number is the *validation loss*; lower is better.\n\n"
     "An agent reports the schedule it tried and the loss it measured. The verifier then repeats the training with that schedule. Training here is deterministic, so an honest report matches exactly. "
     "If the two numbers differ, the run is marked as a mismatch and the verifier's number is the one that counts."),
]
FUZZ_TOPICS = [
    ("What this is", "**The target is a small parser: a program that reads raw bytes and turns them into structured values.** Parsers matter because they are the first code to touch data from outside, so a mistake in one can be reached by anyone who can send it a file.\n\n"
     "This one reads a made-up format called TV. The project's aim is to find every distinct way to make it fail badly."),
    ("How the TV format works", "A TV file starts with the two letters `TV` and one version byte. After that comes a series of values. Each value begins with a one-byte *tag* saying what kind it is:\n\n"
     "| Tag | Kind | What follows the tag |\n|---|---|---|\n"
     "| `01` | String | One length byte, then that many bytes of text |\n"
     "| `02` | Integer | One width byte, then that many bytes holding the number |\n"
     "| `03` | List | One count byte, then that many values (which can themselves be lists) |\n"
     "| `04` | Blob | Two length bytes, then that many raw bytes |\n\n"
     "Example: `54 56 01  01 02 68 69` is the header, then a string of length 2 containing `hi`."),
    ("What counts as a crash", "**A parser is allowed to refuse bad input. It is not allowed to fall over.**\n\n"
     "- Refusing: the parser notices the input is wrong and stops with a clear `ValueError`. That is correct behaviour and does not count.\n"
     "- Crashing: the parser trusts something in the input it should have checked, and fails with a different error part-way through. That counts.\n\n"
     "Two inputs that crash in the same place for the same reason are one finding, not two. The verifier labels each crash with a *signature*, such as `IndexError in read_string`, and counts distinct signatures."),
    ("Words to know", "- **Fuzzing:** feeding a program huge numbers of slightly wrong inputs to see which ones break it.\n"
     "- **Mutation:** making a new test input by changing a valid one a little: flip a byte, cut it short, repeat a piece.\n"
     "- **Signature:** a short label for *where and how* a crash happened. It is how we tell a new bug from a repeat.\n"
     "- **Coverage:** how much of the program the test inputs have actually reached. Here the verifier measures it per parser function.\n"
     "- **Probe:** a batch of inputs sent only so the verifier can measure coverage. It never counts as a result."),
]


# ---------- project 3: a brand-new project being set up, to show the zero-to-one flow ----------

SETUP = "tv-parser-setup"


def survey_agent():
    """A stand-in survey agent. It writes the Understanding page, asks a question and proposes the setup, then responds to
    what the person does on the page. Its topics and proposal are real for this target; its replies to feedback are canned
    and say so, because a script cannot really rewrite an explanation or answer a question."""
    me = dict(project=SETUP, agent="vm-6/survey", harness="Claude Code", host="10.0.0.16")
    say = lambda text: call("/api/events", type="activity", text=text, **me)
    time.sleep(4)
    say("Reading fuzz_target.py from top to bottom")
    time.sleep(6)
    for key, body in FUZZ_TOPICS:
        say(f"Writing up: {key}")
        call("/api/events", type="topic", key=key, body=body, diagram=DIAGRAMS.get(key), **me)
        time.sleep(6)
    call("/api/events", type="question", text="Should inputs that make the parser slow, but not crash, count as findings too?", **me)
    say("Testing a draft scoring script on one crashing input and one clean one")
    time.sleep(6)
    scorer = (server.ROOT / "demo" / "fuzz_score.py").read_text().replace("str(Path(__file__).parent)", repr(str(server.ROOT / "demo")))
    proposal = dict(
        type="proposal", text="Fuzz the parser for distinct crashes, with coverage measured per function",
        body="**Proposed goal: find every distinct way to crash the parser.** The parser has six functions, and each reads a length or count from the input, "
             "so each is a place where trusting the input could go wrong.\n\n"
             "- **What counts:** an input that makes the parser fail with anything other than a clean `ValueError`. Results are counted by distinct crash signature.\n"
             "- **How it is checked:** the scoring script replays the input against the parser and reports the signature it sees. An agent's word is never taken.\n"
             "- **Coverage:** the same script reports which functions each input reached, so the map below fills in from measurement, not from claims.",
        config={"goal": "Find every distinct way to crash the toy TV-format parser",
                "summary": "Agents mutate a valid sample and report inputs that crash. The verifier replays each one; only crashes that reproduce count.",
                "rules": "Only report inputs you have seen crash. An input rejected with a clean error is not a crash. Inputs are at most 4096 bytes. Do not modify the parser.",
                "metric": {"name": "unique crashes", "direction": "higher", "aggregate": "unique",
                           "artifact": 'a JSON object {"hex": "<the crashing input, hex-encoded>"}. A probe may instead send up to 200 inputs: {"hex": ["...", "..."]}'},
                "sections": [{"id": "surface", "title": "Surface", "icon": "shield", "view": "grid", "statuses": ["untouched", "probed", "covered"], "progress": True,
                              "unit": "inputs", "measured": {"probed": 1, "covered": 300}},
                             {"id": "primitives", "title": "Primitives", "icon": "bug", "view": "board", "statuses": ["suspected", "confirmed", "rejected"]}],
                "entries": [{"section": "surface", "key": fn, "text": text} for fn, text in [
                    ("read_header", "Checks the TV letters and version byte"), ("read_value", "Reads a tag and hands off to the right reader"),
                    ("read_string", "Reads a length byte, then that many bytes"), ("read_int", "Reads a width byte, then the number"),
                    ("read_list", "Reads a count, then that many nested values"), ("read_blob", "Reads a two-byte length, then raw bytes")]]},
        scorer={"source": scorer})
    call("/api/events", **proposal, **me)
    say("Proposal sent. Waiting for the person to review it")

    canned = "*This is the demo's stand-in agent, so this reply is canned. A real agent would {} from the target itself.*"
    cursor, answered = 0, set()
    while call(f"/api/project?project={SETUP}").get("phase") == "survey":
        new = call(f"/api/events?project={SETUP}&after={cursor}")["events"]
        cursor += len(new)
        for e in new:  # questions the person asked
            if e["type"] == "question" and e.get("source") == "you" and e.get("text") and e["id"] not in answered:
                answered.add(e["id"])
                call("/api/events", type="question", id=e["id"], answer=canned.format("answer this") , **me)
        task = call("/api/claim", **me)
        if task:
            say("Working on what the person sent back")
            time.sleep(4)
            topic = next((key for key, _ in FUZZ_TOPICS if f'"{key}"' in task["text"]), None)
            if topic:
                call("/api/events", type="topic", key=topic, body=canned.format("rewrite this topic") + "\n\n" + dict(FUZZ_TOPICS)[topic], diagram=DIAGRAMS.get(topic), **me)
            elif "proposal" in task["text"]:
                call("/api/events", **{**proposal, "text": "Revised: " + proposal["text"], "body": canned.format("revise the proposal") + "\n\n" + proposal["body"]}, **me)
            call("/api/events", type="task", id=task["id"], text=task["text"], status="done", **me)
        time.sleep(3)
    say("Setup approved. Starting to fuzz")
    state = {"lock": threading.Lock(), "reported": set(), "inputs": 0, "crashes": {}}
    threading.Thread(target=fuzz_agent, args=("vm-6/claude-code", "Claude Code", "10.0.0.16", False, True, state, SETUP), daemon=True).start()


def start_agents(lr_state, fuzz_state):
    while True:  # wait for the server to accept connections
        try:
            call("/api/projects")
            break
        except OSError:
            time.sleep(0.2)
    agents = [(lr_agent, ("vm-1/claude-code", "Claude Code", "10.0.0.11", False, lr_state)),
              (lr_agent, ("vm-2/codex", "Codex CLI", "10.0.0.12", False, lr_state)),
              (lr_agent, ("vm-3/loop", "Custom bash loop", "10.0.0.13", True, lr_state)),
              (fuzz_agent, ("vm-4/claude-code", "Claude Code", "10.0.0.14", False, True, fuzz_state)),
              (fuzz_agent, ("vm-5/loop", "Custom bash loop", "10.0.0.15", True, False, fuzz_state))]
    for fn, args in agents:
        threading.Thread(target=fn, args=args, daemon=True).start()
    threading.Thread(target=survey_agent, daemon=True).start()
    threading.Thread(target=verifier.loop, args=(URL,), daemon=True).start()


if __name__ == "__main__":
    for name in (LR, FUZZ, SETUP):  # the demo always starts fresh; it only ever removes its own three projects
        shutil.rmtree(server.PROJECTS / name, ignore_errors=True)
    server.new_project({"project": SETUP, "target": f"The file {server.ROOT / 'demo' / 'fuzz_target.py'}. It parses a small binary format called TV. "
                        "Run it with python3; fuzz_target.check(data) tries one input.", "goal": ""})
    threading.Thread(target=start_agents, args=(seed_lr(), seed_fuzz()), daemon=True).start()
    server.serve()
