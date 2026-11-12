#!/usr/bin/env python3
"""Scorer for the fuzzing demo. Replays inputs against the toy parser and reports what really happened.

Reads {"hex": "5456..."} on stdin, or {"hex": ["...", "..."]} for a batch sent as a probe.
Prints {"ok", "score", "detail", "covers"}:
  detail  the crash signature, so two inputs that hit the same bug count as one unique crash
  covers  how many of the replayed inputs reached each parser function: coverage measured here, not claimed
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fuzz_target  # noqa: E402

MAX_BATCH = 200

if __name__ == "__main__":
    try:
        given = json.load(sys.stdin)["hex"]
        batch = isinstance(given, list)
        inputs = [bytes.fromhex(h) for h in (given if batch else [given])][:MAX_BATCH]
        if not inputs:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "detail": "Artifact was not a hex-encoded input, or a list of them"}))
        sys.exit()
    covers, crashes = Counter(), []
    for data in inputs:
        signature, seen = fuzz_target.check(data)
        covers.update(seen)
        if signature:
            crashes.append(signature)
    if batch:
        print(json.dumps({"ok": True, "score": len(inputs), "covers": covers,
                          "detail": f"Replayed {len(inputs)} inputs; {len(crashes)} crashed ({', '.join(sorted(set(crashes))) or 'none'})"}))
    elif crashes:
        print(json.dumps({"ok": True, "score": 1, "detail": crashes[0], "covers": covers}))
    else:
        print(json.dumps({"ok": False, "score": 0, "detail": "Did not reproduce: the parser handled this input", "covers": covers}))
