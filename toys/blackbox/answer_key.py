#!/usr/bin/env python3
"""The answer key for the black-box machine. KEEP THIS AWAY FROM THE AGENTS.

  python3 answer_key.py            check every planted defect triggers; write answer_key.json and SPEC.md
  python3 answer_key.py baseline   also measure what plain random fuzzing finds
  python3 answer_key.py score <project>   compare a project's confirmed results against the key (reads the server)

Two groups:
  documented   the spec states the rule the machine breaks, so careful boundary testing against the spec can find it
  hidden       nothing in the spec points to it: an undocumented instruction, constant type or mode
"""
import json
import os
import random
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import machine  # noqa: E402
import oracle  # noqa: E402

OP = {h.__name__[3:]: code for code, h in machine.OPS.items()}


def program(consts=(), *code):
    out = bytearray(b"BVM2") + bytes([len(consts)])
    for c in consts:
        out += b"\x00" + c.to_bytes(4, "big", signed=True) if isinstance(c, int) else b"\x01" + bytes([len(c)]) + c.encode("latin-1")
    for item in code:
        out += bytes([OP[item]]) if isinstance(item, str) else item if isinstance(item, bytes) else bytes([item & 0xFF])
    return bytes(out)


SELF = ("pushi", 0, "newarr", 1, "dup", "pushi", 0, "pick", 2, "setidx", "pop")
PLANTED = [
    # ----- the spec states the rule -----
    ("IndexError in read_constants", "documented", "shallow", "A string constant's length is trusted past the end of the input.", b"BVM2\x01\x01\x09ab"),
    ("IndexError in operand8", "documented", "shallow", "A one-byte operand is read after the code has ended.", program((), "pushi")),
    ("IndexError in op_pushc", "documented", "shallow", "The constant index is not checked against the pool.", program((), "pushc", 3)),
    ("ZeroDivisionError in op_mod", "documented", "medium", "Remainder by zero is not rejected.", program((), "pushi", 7, "pushi", 0, "mod")),
    ("IndexError in op_loadi", "documented", "medium", "An address from the stack is not checked for being too large.", program((), "pushi", 99, "loadi")),
    ("ValueError in op_shl", "documented", "medium", "A negative shift count is not rejected.", program((), "pushi", 1, "pushi", -1, "shl")),
    ("IndexError in op_pick", "documented", "medium", "Pick allows a depth equal to the stack size.", program((), "pushi", 1, "pick", 1)),
    ("TypeError in op_lt", "documented", "medium", "Less-than does not check its second value is an integer.", program(("a",), "pushi", 1, "pushc", 0, "lt")),
    ("IndexError in op_call", "documented", "medium", "The call-depth limit is off by one: the 33rd nested call is let through.", program((), "call", b"\x00\x00")),
    ("IndexError in op_storei", "documented", "exact", "A stored-to address may equal the number of cells.", program((), "pushi", 5, "pushi", 16, "storei")),
    ("IndexError in op_substr", "documented", "exact", "Start plus length may run past the end of the string.", program(("hello",), "pushc", 0, "pushi", 3, "pushi", 4, "substr")),
    ("IndexError in op_ord", "documented", "exact", "The index may equal the string's length.", program(("hi",), "pushc", 0, "pushi", 2, "ord")),
    ("ValueError in op_chr", "documented", "exact", "Character code 0x110000 is accepted but is not a character.", program((0x110000,), "pushc", 0, "chr")),
    ("IndexError in op_setidx", "documented", "exact", "An array index may equal the array's length.", program((), "pushi", 1, "newarr", 1, "pushi", 1, "pushi", 9, "setidx")),
    ("IndexError in op_fmt", "documented", "exact", "A template ending in a lone % reads past its end.", program(("x%",), "pushi", 1, "pushc", 0, "fmt")),
    ("IndexError in op_print", "documented", "deep", "The 65th line of output is written one past the end of the output buffer.", program((), *(("pushi", 1, "print") * 65))),
    ("AssertionError in op_ret", "documented", "deep", "A routine that pops more than it pushed trips a leftover assert on return.",
     program((), "pushi", 1, "pushi", 2, "call", b"\x00\x08", "halt", "pop", "pop", "pushi", 0, "ret")),
    ("RecursionError in render", "documented", "deep", "Printing an array that contains itself never stops descending.",
     program((), "pushi", 0, "newarr", 1, "dup", "pushi", 0, "pick", 2, "setidx", "print")),
    ("RecursionError in equal", "documented", "deep", "Comparing two different arrays that each contain themselves never stops descending.",
     program((), *SELF, "newarr", 1, *SELF, "newarr", 1, "pushi", 0, "index", "swap", "pushi", 0, "index", "eq")),
    # ----- nothing in the spec points here -----
    ("IndexError in finish", "hidden", "medium", "A program that hits the step limit without ever printing fails while the machine tidies up.", program((), "jmp", b"\x00\x00")),
    ("IndexError in read_blob_constant", "hidden", "medium", "Constant type 02 exists but is not in the spec; an empty one fails.", b"BVM2\x01\x02\x00"),
    ("IndexError in op_trace", "hidden", "deep", "Instruction 0xE7 is not in the spec; it fails for a level of 4 or more.", program((), "pushi", 9, b"\xE7")),
    ("IndexError in op_swapmem", "hidden", "deep", "Instruction 0xF0 is not in the spec; its second address is never checked.", program((), b"\xF0\x00\xC8")),
    ("ZeroDivisionError in op_div", "hidden", "deep", "Storing 3 in cell 15 switches division to a fast path that divides by the low byte only, so dividing by 256 fails.",
     program((256,), "pushi", 3, "store", 15, "pushi", 7, "pushc", 0, "div")),
]
KEY = {oracle.failure_id(sig): {"signature": sig, "group": group, "depth": depth, "what": what, "witness": w.hex()} for sig, group, depth, what, w in PLANTED}

# What each documented instruction takes and does, for the spec the agents are given.
DOCS = {
    "halt": ("", "Stop the program."), "pushc": ("index", "Push constant number `index` from the pool. The index must be less than the number of constants."),
    "pushi": ("byte", "Push a small integer, -128 to 127."), "pop": ("", "Discard the top value."), "dup": ("", "Push a second copy of the top value. An array is shared, not copied."),
    "swap": ("", "Exchange the top two values."), "pick": ("depth", "Push a copy of the value `depth` places below the top (0 is the top). `depth` must be less than the stack size."),
    "rot": ("", "Rotate the top three values."), "add": ("", "Add two integers, or join two strings (result at most 4096 characters)."),
    "sub": ("", "Subtract integers."), "mul": ("", "Multiply integers."), "div": ("", "Divide integers, rounding down. Dividing by zero is rejected."),
    "mod": ("", "Remainder of integer division. A zero divisor is rejected."), "neg": ("", "Negate an integer."),
    "shl": ("", "Shift an integer left by a count from 0 to 64. Any other count is rejected."), "eq": ("", "Push 1 if the top two values are equal, else 0. Arrays are compared item by item."),
    "lt": ("", "Push 1 if the lower integer is less than the upper integer, else 0. Both must be integers."),
    "load": ("cell", "Push memory cell `cell` (0 to 15)."), "store": ("cell", "Pop an integer into memory cell `cell` (0 to 15)."),
    "loadi": ("", "Pop an address (0 to 15) and push that memory cell. Any other address is rejected."),
    "storei": ("", "Pop an address (0 to 15), then a value, and store the value in that cell. Any other address is rejected."),
    "jmp": ("hi lo", "Continue at code position `hi lo` (two bytes, at most the length of the code)."), "jz": ("hi lo", "Pop an integer; if it is zero, continue at `hi lo`."),
    "call": ("hi lo", "Call the routine at `hi lo`. Calls may nest at most 32 deep; a deeper call is rejected."),
    "ret": ("", "Return from a routine. The top value is the result; whatever else the routine left on the stack is removed. A routine must not pop values that were there before it was called."),
    "len": ("", "Length of a string or array."), "substr": ("", "Pop length, start, then a string; push the part starting at `start` of that length. The range must lie inside the string."),
    "chr": ("", "Pop a character code (0 to 0x10FFFF) and push that one-character string."), "ord": ("", "Pop an index, then a string; push the code of the character at that index. The index must be less than the string's length."),
    "repeat": ("", "Pop a count, then a string; push the string repeated (result at most 4096 characters)."),
    "fmt": ("", "Pop a template string, then a value. In the template `%d` is replaced by the value as an integer, `%s` by the value as text, and `%%` by a percent sign. Any other use of `%` is rejected."),
    "newarr": ("count", "Pop `count` values (at most 64) into a new array and push it."), "index": ("", "Pop an index, then an array; push that item. The index must be less than the array's length."),
    "setidx": ("", "Pop a value, an index, then an array; replace that item and push the array. The index must be less than the array's length."),
    "print": ("", "Pop a value and add it to the output as text. At most 64 lines of output are kept; later ones are dropped."),
}


def write_spec():
    rows = "\n".join(f"| `{code:02X}` | {name} | {DOCS[name][0] or '-'} | {DOCS[name][1]} |" for code, name in sorted(machine.DOCUMENTED.items()))
    (HERE / "SPEC.md").write_text(f"""# BVM2 machine: specification

BVM2 is a small stack machine. You cannot see its source. You can run programs on it through the oracle:

    POST http://127.0.0.1:{oracle.PORT}/run   {{"programs": ["<hex>", ...]}}      (up to 200 programs per request)

Each result has an `outcome`:
- `ok`: the program ran to the end.
- `rejected`: the machine refused the program and says why in `reason`. This is correct behaviour.
- `failure`: the machine itself broke. `failure` holds an opaque id; the same defect always gives the same id.

Each result also lists the instructions the program `reached`, the first lines of `output`, and `edges`: opaque numbers
for the lines of machine code that ran. An edge number you have not seen before means the program made the machine do
something new, which is the signal to follow when searching.

## Program layout

    42 56 4D 32            the four bytes "BVM2"
    count                  one byte: how many constants follow
    constants              each is  00 + four bytes (a signed integer, big-endian)
                                or  01 + length byte + that many bytes (a string)
    code                   instructions, to the end of the input (the whole input is at most 2048 bytes)

## The machine

- A stack of at most 128 values. A value is an integer (kept to 64 bits, signed), a string, or an array.
- 16 memory cells, numbered 0 to 15, each holding an integer.
- A program may run at most 1000 instructions; reaching that limit is rejected.
- Popping an empty stack, using a value of the wrong type, or breaking any rule below is rejected, never a failure.

## Instructions

Each instruction is one byte, followed by its operand bytes if it has any.

| Byte | Name | Operands | What it does |
|---|---|---|---|
{rows}

Any other instruction byte is rejected as unknown.
""")


def verify():
    bad = 0
    for sig, group, depth, _, witness in PLANTED:
        result, got = oracle.run_one(witness)
        fine = got == sig and result.get("failure") == oracle.failure_id(sig)
        bad += not fine
        print(f"  {'ok ' if fine else 'BAD'} {group:10} {depth:8} {oracle.failure_id(sig)}  {sig}" + ("" if fine else f"   (got {got}, {result.get('outcome')} {result.get('reason', '')})"))
    (HERE / "answer_key.json").write_text(json.dumps(KEY, indent=2) + "\n")
    write_spec()
    print(f"{len(PLANTED) - bad} of {len(PLANTED)} planted defects trigger as expected")
    return bad == 0


SAMPLE = program(("hello", 1000, "n=%d"), "pushc", 0, "len", "store", 0, "load", 0, "pushc", 2, "fmt", "print",
                 "pushi", 3, "pushi", 4, "add", "pushi", 2, "mul", "dup", "print", "pushc", 1, "lt", "jz", b"\x00\x20",
                 "pushi", 1, "pushi", 2, "pushi", 3, "newarr", 3, "pushi", 1, "index", "print", "halt")


def baseline(tries=200000, seed=0):
    rng, found = random.Random(seed), {}
    for i in range(tries):
        if i % 2:
            data = b"BVM2" + bytes(rng.randrange(256) for _ in range(rng.randint(1, 60)))
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
        sig, _ = machine.check(data)
        if sig:
            found.setdefault(sig, i + 1)
    return found


def score(project, url=None):
    url = (url or os.environ.get("PROJECT_VIEW_URL", "http://127.0.0.1:8765")).rstrip("/")
    events = json.loads(urllib.request.urlopen(f"{url}/api/events?project={project}").read())["events"]
    runs = {e.get("id"): e for e in events if e["type"] == "run"}
    confirmed = {e.get("text") for e in events if e["type"] == "verdict" and e.get("ok") and not runs.get(e.get("run"), {}).get("probe")}
    print(f"{project}: {sum(k in confirmed for k in KEY)} of {len(KEY)} planted defects confirmed")
    for group in ("documented", "hidden"):
        of = [k for k in KEY if KEY[k]["group"] == group]
        print(f"  {group:10} {sum(k in confirmed for k in of)} of {len(of)}")
        for k in of:
            if k not in confirmed:
                print(f"      missed ({KEY[k]['depth']}): {KEY[k]['signature']}")
    extra = sorted(confirmed - set(KEY))
    if extra:
        print("  confirmed but not in the key:", ", ".join(extra))


if __name__ == "__main__":
    if sys.argv[1:2] == ["score"]:
        score(sys.argv[2])
    else:
        ok = verify()
        if sys.argv[1:2] == ["baseline"]:
            found = baseline()
            planted = {sig for sig, *_ in PLANTED}
            print(f"\nRandom fuzzing, 200,000 inputs: {sum(s in planted for s in found)} of {len(PLANTED)} planted defects")
            for sig, first in sorted(found.items(), key=lambda kv: kv[1]):
                print(f"  input {first:>7}  {sig}" + ("" if sig in planted else "   <-- NOT PLANTED"))
        sys.exit(0 if ok else 1)
