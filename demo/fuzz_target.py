"""A deliberately buggy toy parser, used only as the fuzzing target for the demo.

Format: the bytes "TV", a version byte, then values. Each value starts with a tag:
  1 string  (length byte, then that many bytes)
  2 int     (width byte, then that many bytes)
  3 list    (count byte, then that many values)
  4 blob    (two length bytes, then that many bytes)

Malformed input should be rejected with ValueError. Four planted bugs crash it instead.
"""
import traceback

MAX_INPUT = 4096
FUNCTIONS = ["read_header", "read_value", "read_string", "read_int", "read_list", "read_blob"]


def parse(data, seen):
    pos, out = read_header(data, seen), []
    while pos < len(data):
        value, pos = read_value(data, pos, seen)
        out.append(value)
    return out


def read_header(data, seen):
    seen.add("read_header")
    if len(data) < 3 or data[:2] != b"TV":
        raise ValueError("bad header")
    return 3


def read_value(data, pos, seen):
    seen.add("read_value")
    if pos + 1 >= len(data):
        raise ValueError("truncated")
    reader = {1: read_string, 2: read_int, 3: read_list, 4: read_blob}.get(data[pos])
    if not reader:
        raise ValueError("unknown tag")
    return reader(data, pos + 1, seen)


def read_string(data, pos, seen):
    seen.add("read_string")
    n = data[pos]
    chars = [data[pos + 1 + i] for i in range(n)]  # BUG: trusts the length byte, reads past the end
    return bytes(chars), pos + 1 + n


def read_int(data, pos, seen):
    seen.add("read_int")
    width = data[pos]
    if pos + 1 + width > len(data):
        raise ValueError("truncated")
    value = int.from_bytes(data[pos + 1:pos + 1 + width], "big")
    return (value, value % width), pos + 1 + width  # BUG: a width of zero divides by zero


def read_list(data, pos, seen):
    seen.add("read_list")
    items, pos = [], pos + 1
    for _ in range(data[pos - 1]):
        value, pos = read_value(data, pos, seen)  # BUG: no depth limit, deep nesting exhausts the stack
        items.append(value)
    return items, pos


def read_blob(data, pos, seen):
    seen.add("read_blob")
    if pos + 2 > len(data):
        raise ValueError("truncated")
    n = int.from_bytes(data[pos:pos + 2], "big")
    buf = bytearray(min(n, 64))
    for i in range(n):
        if pos + 2 + i >= len(data):
            raise ValueError("truncated")
        buf[i] = data[pos + 2 + i]  # BUG: buffer is capped at 64 bytes but the copy is not
    return bytes(buf), pos + 2 + n


def check(data):
    """Run the parser on one input. Returns (crash signature or None, set of functions reached)."""
    seen = set()
    if len(data) > MAX_INPUT:
        return None, seen
    try:
        parse(data, seen)
    except ValueError:
        return None, seen
    except RecursionError:
        return "RecursionError in read_list", seen
    except Exception as e:
        frames = [f.name for f in traceback.extract_tb(e.__traceback__) if f.filename == __file__]
        return f"{type(e).__name__} in {frames[-1] if frames else 'parse'}", seen
    return None, seen
