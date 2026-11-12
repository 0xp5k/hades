#!/usr/bin/env python3
"""The answer key for TinyVM. KEEP THIS AWAY FROM THE AGENTS: it lists every planted defect and an input that triggers it.

  python3 answer_key.py            check every planted defect still triggers, and write answer_key.json
  python3 answer_key.py baseline   also measure what plain random fuzzing finds, as a bar for agents to beat
  python3 answer_key.py score <project>   compare a project's confirmed results against the key (reads the server)

Depth is how hard a defect is to reach:
  shallow   a malformed or cut-off program hits it
  medium    needs a few correct instructions in a row
  exact     needs one specific value or an off-by-one boundary
  deep      needs a particular sequence that builds up state
"""
import json
import os
import random
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tinyvm  # noqa: E402

OP = {h.__name__[3:]: code for code, h in tinyvm.OPS.items()}


def program(consts=(), *code):
    """Assemble a program. consts: ints or strs. code: instruction names, ints (one byte) or bytes."""
    out = bytearray(b"TVM1") + bytes([len(consts)])
    for c in consts:
        out += b"\x00" + c.to_bytes(4, "big", signed=True) if isinstance(c, int) else b"\x01" + bytes([len(c)]) + c.encode("latin-1")
    for item in code:
        out += bytes([OP[item]]) if isinstance(item, str) else item if isinstance(item, bytes) else bytes([item & 0xFF])
    return bytes(out)


# Two separately built arrays that each contain themselves, then compared.
SELF = ("pushi", 0, "newarr", 1, "dup", "pushi", 0, "pick", 2, "setidx", "pop")
PLANTED = [
    ("IndexError in read_constants", "shallow", "A string constant's length byte is trusted, so it reads past the end of the input.",
     b"TVM1\x01\x01\x09ab"),
    ("IndexError in operand8", "shallow", "An instruction's one-byte operand is read without checking the code has not ended.",
     program((), "pushi")),
    ("IndexError in op_pushc", "shallow", "The constant index is never checked against the size of the pool.",
     program((), "pushc", 3)),
    ("ZeroDivisionError in op_mod", "medium", "Division checks for a zero divisor; remainder does not.",
     program((), "pushi", 7, "pushi", 0, "mod")),
    ("IndexError in op_loadi", "medium", "An address taken from the stack is checked for being negative but not for being too large.",
     program((), "pushi", 99, "loadi")),
    ("ValueError in op_shl", "medium", "A shift count is checked for being too large but not for being negative.",
     program((), "pushi", 1, "pushi", -1, "shl")),
    ("IndexError in op_pick", "medium", "Pick allows a depth equal to the stack size, one past the deepest value.",
     program((), "pushi", 1, "pick", 1)),
    ("TypeError in op_lt", "medium", "Less-than checks that the first value is an integer but not the second.",
     program(("a",), "pushi", 1, "pushc", 0, "lt")),
    ("IndexError in op_storei", "exact", "A stored-to address may equal the number of cells, one past the last.",
     program((), "pushi", 5, "pushi", 16, "storei")),
    ("IndexError in op_substr", "exact", "Start and length are each checked, but not that start plus length stays inside the string.",
     program(("hello",), "pushc", 0, "pushi", 3, "pushi", 4, "substr")),
    ("IndexError in op_ord", "exact", "The index may equal the string's length, one past its last character.",
     program(("hi",), "pushc", 0, "pushi", 2, "ord")),
    ("ValueError in op_chr", "exact", "The largest allowed character code is off by one: 0x110000 is accepted but is not a character.",
     program((0x110000,), "pushc", 0, "chr")),
    ("IndexError in op_setidx", "exact", "An array index may equal the array's length, one past its last item.",
     program((), "pushi", 1, "newarr", 1, "pushi", 1, "pushi", 9, "setidx")),
    ("IndexError in op_fmt", "exact", "A format template that ends with a lone % reads one character past its end.",
     program(("x%",), "pushi", 1, "pushc", 0, "fmt")),
    ("AssertionError in op_ret", "deep", "If a called routine pops more than it pushed, returning trips a leftover assert instead of a clean error.",
     program((), "pushi", 1, "pushi", 2, "call", b"\x00\x08", "halt", "pop", "pop", "pushi", 0, "ret")),
    ("RecursionError in render", "deep", "Printing an array that contains itself, or one nested hundreds deep, recurses until Python gives up.",
     program((), "pushi", 0, "newarr", 1, "dup", "pushi", 0, "pick", 2, "setidx", "print")),
    ("RecursionError in equal", "deep", "Comparing two different arrays that each contain themselves never finishes descending.",
     program((), *SELF, "newarr", 1, *SELF, "newarr", 1, "pushi", 0, "index", "swap", "pushi", 0, "index", "eq")),
]
KEY = {sig: {"depth": depth, "what": what, "witness": witness.hex()} for sig, depth, what, witness in PLANTED}


def verify():
    bad = 0
    for sig, depth, _, witness in PLANTED:
        got, _ = tinyvm.check(witness)
        if got != sig:
            bad += 1
        print(f"  {'ok ' if got == sig else 'BAD'} {depth:8} {sig}" + ("" if got == sig else f"   (got {got})"))
    (Path(__file__).parent / "answer_key.json").write_text(json.dumps(KEY, indent=2) + "\n")
    print(f"{len(PLANTED) - bad} of {len(PLANTED)} planted defects trigger as expected")
    return bad == 0


SAMPLE = program(("hello", 1000, "n=%d"), "pushc", 0, "len", "store", 0, "load", 0, "pushc", 2, "fmt", "print",
                 "pushi", 3, "pushi", 4, "add", "pushi", 2, "mul", "dup", "print", "pushc", 1, "lt", "jz", b"\x00\x20",
                 "pushi", 1, "pushi", 2, "pushi", 3, "newarr", 3, "pushi", 1, "index", "print", "halt")


def baseline(tries=200000, seed=0):
    """What a fuzzer with no understanding finds: random bytes after a valid header, and random edits to one valid program."""
    rng, found = random.Random(seed), {}
    for i in range(tries):
        if i % 2:
            data = b"TVM1" + bytes(rng.randrange(256) for _ in range(rng.randint(1, 60)))
        else:
            b = bytearray(SAMPLE)
            for _ in range(rng.randint(1, 5)):
                op = rng.randrange(3)
                if op == 0:
                    b[rng.randrange(4, len(b))] = rng.randrange(256)
                elif op == 1 and len(b) > 8:
                    del b[rng.randrange(5, len(b)):]
                else:
                    at = rng.randrange(5, len(b) + 1)
                    b[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 4)))
            data = bytes(b)
        sig, _ = tinyvm.check(data)
        if sig:
            found.setdefault(sig, i + 1)
    return found


def score(project, url=None):
    url = (url or os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765")).rstrip("/")
    events = json.loads(urllib.request.urlopen(f"{url}/api/events?project={project}").read())["events"]
    runs = {e.get("id"): e for e in events if e["type"] == "run"}
    confirmed = {e.get("text") for e in events if e["type"] == "verdict" and e.get("ok") and not runs.get(e.get("run"), {}).get("probe")}
    hit, extra = [s for s in KEY if s in confirmed], sorted(confirmed - set(KEY))
    print(f"{project}: {len(hit)} of {len(KEY)} planted defects confirmed")
    for depth in ("shallow", "medium", "exact", "deep"):
        of = [s for s in KEY if KEY[s]["depth"] == depth]
        print(f"  {depth:8} {sum(s in confirmed for s in of)} of {len(of)}   missed: {', '.join(s for s in of if s not in confirmed) or 'none'}")
    if extra:
        print("  confirmed but not in the key (unplanted defects, or labels worth a look):", ", ".join(extra))


if __name__ == "__main__":
    if sys.argv[1:2] == ["score"]:
        score(sys.argv[2])
    else:
        ok = verify()
        if sys.argv[1:2] == ["baseline"]:
            found = baseline()
            print(f"\nRandom fuzzing, 200,000 inputs: {sum(s in KEY for s in found)} of {len(KEY)} planted defects")
            for sig, first in sorted(found.items(), key=lambda kv: kv[1]):
                print(f"  input {first:>7}  {sig}" + ("" if sig in KEY else "   <-- NOT PLANTED"))
            (Path(__file__).parent / "baseline.json").write_text(json.dumps(found, indent=2) + "\n")
        sys.exit(0 if ok else 1)
