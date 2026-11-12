#!/usr/bin/env python3
"""The scoring command for the practice project. The verifier runs it; agents never do.

Reads a run's artifact as JSON on standard input ({"program_hex": "..."}), replays the program against the copy of
tinyvm.py that sits next to this file (not the copy the agents work with), and prints one line of JSON:
  ok      true only if the program made the machine fail with something other than its own VMError
  detail  the failure's label, "ErrorType in function"; runs with the same label count once
  covers  the parts of the machine the program reached, which is how the page measures coverage
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tinyvm  # noqa: E402


def main():
    try:
        program = bytes.fromhex(json.loads(sys.stdin.read())["program_hex"])
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "score": 0, "detail": "malformed artifact"}
    signature, seen = tinyvm.check(program)
    return {"ok": signature is not None, "score": 1 if signature else 0, "detail": signature or "no failure",
            "covers": {part: 1 for part in sorted(seen)}}


print(json.dumps(main()))
