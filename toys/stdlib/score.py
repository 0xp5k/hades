#!/usr/bin/env python3
"""The scoring command for the standard-library project. The verifier runs it; agents never do.

Reads a run's artifact as JSON on standard input and replays it with the copy of targets.py next to this file.

  A finding    {"target": "<name>", "hex": "<one input>"}
               ok is true only if that input shows a defect; detail is the defect's label, so equal labels count once.
  A probe      {"target": "<name>", "inputs": ["<hex>", ...]}   up to 200 inputs
               never counts as a result (ok is false); detail says how many inputs showed a defect and lists the labels,
               so that a batch is a cheap way to look for leads but each finding must be sent on its own.

covers reports how many inputs were tried against the target, which is how the page measures coverage.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import targets  # noqa: E402


def main():
    try:
        art = json.loads(sys.stdin.read())
        target = art["target"]
        if target not in targets.TARGETS:
            return {"ok": False, "score": 0, "detail": "unknown target"}
        if "inputs" in art:
            inputs = [bytes.fromhex(h) for h in art["inputs"]]
            if not 1 <= len(inputs) <= 200:
                raise ValueError
        else:
            inputs = None
            data = bytes.fromhex(art["hex"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "score": 0, "detail": "malformed artifact"}
    if inputs is None:
        label, outcome = targets.check(target, data)
        return {"ok": label is not None, "score": 1 if label else 0, "detail": label or f"no defect: {outcome}", "covers": {target: 1}}
    labels = sorted({label for label, _ in (targets.check(target, d) for d in inputs) if label})
    detail = f"probe of {len(inputs)} inputs: " + (f"{len(labels)} defect labels seen ({'; '.join(labels[:6])}). Send each as its own finding." if labels else "no defects")
    return {"ok": False, "score": 0, "detail": detail[:300], "covers": {target: len(inputs)}}


print(json.dumps(main()))
