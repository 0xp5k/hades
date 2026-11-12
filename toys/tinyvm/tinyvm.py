"""TinyVM: a small stack machine that runs programs given as bytes.

A program is:
    the four bytes "TVM1"
    a constant pool:  one count byte, then that many constants, each either
                        00 + four bytes        a signed integer
                        01 + length byte + that many bytes   a string
    the code:         instructions, to the end of the input

Values on the stack are integers (kept to 64 bits), strings and arrays. There are 16 memory cells holding
integers, a call stack, and a limit on steps so every program ends.

A program that is malformed, or that does something the machine does not allow (popping an empty stack,
adding a string to a number, jumping outside the code), is stopped with VMError. That is the machine working
as intended. Any other kind of failure is a defect in the machine.

    check(data) -> (failure signature or None, set of the parts of the machine the program reached)
"""
import traceback

MAX_INPUT = 2048
MAX_STEPS = 1000
MAX_STACK = 128
MEMORY = 16
MAX_STRING = 4096
MAX_ARRAY = 64
MAX_FRAMES = 32
WORK_BUDGET = 20000  # how many values one program may print or compare in total

HALT = object()


class VMError(Exception):
    """The program is invalid, or did something the machine detected and refused."""


def wrap(n):
    """Keep an integer to 64 bits, signed."""
    n &= (1 << 64) - 1
    return n - (1 << 64) if n >> 63 else n


class VM:
    def __init__(self, data, seen):
        self.data, self.seen = data, seen
        self.consts, self.code = [], b""
        self.stack, self.memory, self.frames, self.out = [], [0] * MEMORY, [], []
        self.pc, self.steps, self.budget = 0, 0, WORK_BUDGET

    # ----- loading -----

    def read_header(self):
        self.seen.add("read_header")
        if len(self.data) < 5 or self.data[:4] != b"TVM1":
            raise VMError("bad header")
        return 4

    def read_constants(self, pos):
        self.seen.add("read_constants")
        count = self.data[pos]
        pos += 1
        for _ in range(count):
            if pos >= len(self.data):
                raise VMError("truncated constant pool")
            kind = self.data[pos]
            pos += 1
            if kind == 0:
                if pos + 4 > len(self.data):
                    raise VMError("truncated integer constant")
                self.consts.append(int.from_bytes(self.data[pos:pos + 4], "big", signed=True))
                pos += 4
            elif kind == 1:
                if pos >= len(self.data):
                    raise VMError("truncated string constant")
                n = self.data[pos]
                chars = [self.data[pos + 1 + i] for i in range(n)]
                self.consts.append(bytes(chars).decode("latin-1"))
                pos += 1 + n
            else:
                raise VMError("unknown constant type")
        return pos

    # ----- helpers -----

    def push(self, value):
        if len(self.stack) >= MAX_STACK:
            raise VMError("stack overflow")
        self.stack.append(value)

    def pop(self):
        if not self.stack:
            raise VMError("stack underflow")
        return self.stack.pop()

    def pop_int(self):
        value = self.pop()
        if not isinstance(value, int):
            raise VMError("expected an integer")
        return value

    def pop_str(self):
        value = self.pop()
        if not isinstance(value, str):
            raise VMError("expected a string")
        return value

    def pop_array(self):
        value = self.pop()
        if not isinstance(value, list):
            raise VMError("expected an array")
        return value

    def operand8(self):
        value = self.code[self.pc]
        self.pc += 1
        return value

    def operand16(self):
        if self.pc + 2 > len(self.code):
            raise VMError("truncated instruction")
        value = int.from_bytes(self.code[self.pc:self.pc + 2], "big")
        self.pc += 2
        return value

    def target(self):
        where = self.operand16()
        if where > len(self.code):
            raise VMError("jump outside the code")
        return where

    def spend(self):
        self.budget -= 1
        if self.budget < 0:
            raise VMError("too much work")

    def render(self, value):
        self.spend()
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return repr(value)
        parts = []
        for item in value:
            parts.append(self.render(item))
        return "[" + ", ".join(parts) + "]"

    def equal(self, a, b):
        self.spend()
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                if not self.equal(x, y):
                    return False
            return True
        return type(a) is type(b) and a == b

    # ----- running -----

    def run(self):
        while self.pc < len(self.code):
            self.steps += 1
            if self.steps > MAX_STEPS:
                raise VMError("step limit reached")
            handler = OPS.get(self.code[self.pc])
            self.pc += 1
            if handler is None:
                raise VMError("unknown instruction")
            self.seen.add(handler.__name__)
            if handler(self) is HALT:
                break

    # ----- instructions: stack -----

    def op_halt(self):
        return HALT

    def op_pushc(self):
        self.push(self.consts[self.operand8()])

    def op_pushi(self):
        value = self.operand8()
        self.push(value - 256 if value > 127 else value)

    def op_pop(self):
        self.pop()

    def op_dup(self):
        value = self.pop()
        self.push(value)
        self.push(value)

    def op_swap(self):
        b, a = self.pop(), self.pop()
        self.push(b)
        self.push(a)

    def op_pick(self):
        depth = self.operand8()
        if depth > len(self.stack):
            raise VMError("pick reaches below the stack")
        self.push(self.stack[-(depth + 1)])

    def op_rot(self):
        c, b, a = self.pop(), self.pop(), self.pop()
        self.push(b)
        self.push(c)
        self.push(a)

    # ----- instructions: arithmetic -----

    def op_add(self):
        b, a = self.pop(), self.pop()
        if isinstance(a, int) and isinstance(b, int):
            self.push(wrap(a + b))
        elif isinstance(a, str) and isinstance(b, str):
            if len(a) + len(b) > MAX_STRING:
                raise VMError("string too long")
            self.push(a + b)
        else:
            raise VMError("cannot add these")

    def op_sub(self):
        b, a = self.pop_int(), self.pop_int()
        self.push(wrap(a - b))

    def op_mul(self):
        b, a = self.pop_int(), self.pop_int()
        self.push(wrap(a * b))

    def op_div(self):
        b, a = self.pop_int(), self.pop_int()
        if b == 0:
            raise VMError("division by zero")
        self.push(wrap(a // b))

    def op_mod(self):
        b, a = self.pop_int(), self.pop_int()
        self.push(wrap(a % b))

    def op_neg(self):
        self.push(wrap(-self.pop_int()))

    def op_shl(self):
        count, value = self.pop_int(), self.pop_int()
        if count > 64:
            raise VMError("shift too large")
        self.push(wrap(value << count))

    def op_eq(self):
        b, a = self.pop(), self.pop()
        self.push(1 if self.equal(a, b) else 0)

    def op_lt(self):
        b, a = self.pop(), self.pop()
        if not isinstance(a, int):
            raise VMError("expected an integer")
        self.push(1 if a < b else 0)

    # ----- instructions: memory -----

    def op_load(self):
        address = self.operand8()
        if address >= MEMORY:
            raise VMError("bad address")
        self.push(self.memory[address])

    def op_store(self):
        address = self.operand8()
        if address >= MEMORY:
            raise VMError("bad address")
        self.memory[address] = self.pop_int()

    def op_loadi(self):
        address = self.pop_int()
        if address < 0:
            raise VMError("bad address")
        self.push(self.memory[address])

    def op_storei(self):
        address, value = self.pop_int(), self.pop_int()
        if address < 0 or address > MEMORY:
            raise VMError("bad address")
        self.memory[address] = value

    # ----- instructions: control -----

    def op_jmp(self):
        self.pc = self.target()

    def op_jz(self):
        where = self.target()
        if self.pop_int() == 0:
            self.pc = where

    def op_call(self):
        where = self.target()
        if len(self.frames) >= MAX_FRAMES:
            raise VMError("call stack overflow")
        self.frames.append((self.pc, len(self.stack)))
        self.pc = where

    def op_ret(self):
        if not self.frames:
            raise VMError("return outside a call")
        self.pc, base = self.frames.pop()
        result = self.pop()
        assert len(self.stack) >= base, "callee left the stack below where it found it"
        del self.stack[base:]
        self.push(result)

    # ----- instructions: strings -----

    def op_len(self):
        value = self.pop()
        if not isinstance(value, (str, list)):
            raise VMError("expected a string or an array")
        self.push(len(value))

    def op_substr(self):
        length, start, text = self.pop_int(), self.pop_int(), self.pop_str()
        if start < 0 or length < 0 or start > len(text):
            raise VMError("bad substring range")
        self.push("".join(text[start + i] for i in range(length)))

    def op_chr(self):
        code = self.pop_int()
        if code < 0 or code > 0x110000:
            raise VMError("not a character code")
        self.push(chr(code))

    def op_ord(self):
        index, text = self.pop_int(), self.pop_str()
        if index < 0 or index > len(text):
            raise VMError("bad index")
        self.push(ord(text[index]))

    def op_repeat(self):
        count, text = self.pop_int(), self.pop_str()
        if count < 0 or len(text) * count > MAX_STRING:
            raise VMError("string too long")
        self.push(text * count)

    def op_fmt(self):
        template, value = self.pop_str(), self.pop()
        out, i = [], 0
        while i < len(template):
            ch = template[i]
            if ch != "%":
                out.append(ch)
                i += 1
                continue
            spec = template[i + 1]
            if spec == "%":
                out.append("%")
            elif spec == "d":
                if not isinstance(value, int):
                    raise VMError("%d needs an integer")
                out.append(str(value))
            elif spec == "s":
                out.append(value if isinstance(value, str) else self.render(value))
            else:
                raise VMError("unknown format")
            i += 2
        text = "".join(out)
        if len(text) > MAX_STRING:
            raise VMError("string too long")
        self.push(text)

    # ----- instructions: arrays and output -----

    def op_newarr(self):
        count = self.operand8()
        if count > MAX_ARRAY or count > len(self.stack):
            raise VMError("bad array size")
        start = len(self.stack) - count
        items = self.stack[start:]
        del self.stack[start:]
        self.push(items)

    def op_index(self):
        index, array = self.pop_int(), self.pop_array()
        if index < 0 or index >= len(array):
            raise VMError("bad index")
        self.push(array[index])

    def op_setidx(self):
        value, index, array = self.pop(), self.pop_int(), self.pop_array()
        if index < 0 or index > len(array):
            raise VMError("bad index")
        array[index] = value
        self.push(array)

    def op_print(self):
        text = self.render(self.pop())
        if len(self.out) < 64:
            self.out.append(text[:200])


OPS = {
    0x00: VM.op_halt, 0x01: VM.op_pushc, 0x02: VM.op_pushi, 0x03: VM.op_pop, 0x04: VM.op_dup, 0x05: VM.op_swap,
    0x06: VM.op_add, 0x07: VM.op_sub, 0x08: VM.op_mul, 0x09: VM.op_div, 0x0A: VM.op_mod, 0x0B: VM.op_neg,
    0x0C: VM.op_load, 0x0D: VM.op_store, 0x0E: VM.op_loadi, 0x0F: VM.op_storei,
    0x10: VM.op_jmp, 0x11: VM.op_jz, 0x12: VM.op_call, 0x13: VM.op_ret,
    0x14: VM.op_len, 0x15: VM.op_substr, 0x16: VM.op_chr, 0x17: VM.op_ord, 0x18: VM.op_repeat, 0x19: VM.op_fmt,
    0x1A: VM.op_newarr, 0x1B: VM.op_index, 0x1C: VM.op_setidx, 0x1D: VM.op_print,
    0x1E: VM.op_eq, 0x1F: VM.op_lt, 0x20: VM.op_shl, 0x21: VM.op_pick, 0x22: VM.op_rot,
}
PARTS = ["read_header", "read_constants"] + [handler.__name__ for handler in OPS.values()]


def check(data):
    """Run one program. Returns (failure signature or None, set of parts reached).
    A signature names the kind of failure and where it happened, for example "IndexError in op_pushc"."""
    seen = set()
    if len(data) > MAX_INPUT:
        return None, seen
    vm = VM(data, seen)
    try:
        position = vm.read_constants(vm.read_header())
        vm.code = data[position:]
        vm.run()
    except VMError:
        return None, seen
    except Exception as error:
        frames = [f.name for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__ and not f.name.startswith("<")]
        if isinstance(error, RecursionError) and frames:
            where = max(sorted(set(frames)), key=frames.count)  # the function that was recursing, not whichever helper hit the limit
        else:
            where = frames[-1] if frames else "check"
        return f"{type(error).__name__} in {where}", seen
    return None, seen
