#!/usr/bin/env python3
"""The black-box oracle: the only way agents can run the machine. They never see its source.

  python3 oracle.py            serves on http://127.0.0.1:8770

  POST /run  {"programs": ["<hex>", ...]}      up to 200 programs per request
  ->  {"results": [{"outcome": "ok" | "rejected" | "failure",
                    "reason":  why it was rejected (the machine's own message),
                    "failure": an opaque id such as "F-3a9c1e"; the same defect always gives the same id,
                    "output":  the first lines the program printed,
                    "reached": the instructions it executed, by name,
                    "edges":   opaque numbers for the lines of machine code it ran; a number you have not
                               seen before means the program did something new}]}

"rejected" is the machine working as intended. "failure" is a defect.
"""
import hashlib
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import machine  # noqa: E402

PORT = 8770
FILE = machine.__file__
HIDDEN = {"op_trace": "undocumented instruction 0xE7", "op_swapmem": "undocumented instruction 0xF0"}


def failure_id(signature):
    return "F-" + hashlib.sha1(signature.encode()).hexdigest()[:6]


def run_one(data):
    """Returns (result for the caller, plain signature or None). The plain signature never leaves this process."""
    if len(data) > machine.MAX_INPUT:
        return {"outcome": "rejected", "reason": "input is larger than 2048 bytes", "reached": [], "edges": []}, None
    lines, seen = set(), set()

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != FILE:
            return None
        if event == "line":
            lines.add(frame.f_lineno)
        return tracer

    vm, result, signature = machine.VM(data, seen), {}, None
    sys.settrace(tracer)
    try:
        vm.code = data[vm.read_constants(vm.read_header()):]
        vm.run()
        result = {"outcome": "ok"}
    except machine.VMError as error:
        result = {"outcome": "rejected", "reason": str(error)}
    except Exception as error:
        frames = [f.name for f in traceback.extract_tb(error.__traceback__) if f.filename == FILE and not f.name.startswith("<")]
        where = max(set(frames), key=frames.count) if isinstance(error, RecursionError) and frames else frames[-1] if frames else "run"
        signature = f"{type(error).__name__} in {where}"
        result = {"outcome": "failure", "failure": failure_id(signature)}
    finally:
        sys.settrace(None)
    result["output"] = [str(line) for line in vm.out[:5]]
    result["reached"] = sorted(HIDDEN.get(name, name.replace("op_", "")) for name in seen)
    result["edges"] = sorted((n * 7919) % 9973 for n in lines)
    return result, signature


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, {"use": 'POST /run {"programs": ["<hex>", ...]}', "limit": "200 programs per request, 2048 bytes each"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 2_000_000:
                return self.reply(413, {"error": "request too large"})
            programs = json.loads(self.rfile.read(size))["programs"]
            if not isinstance(programs, list) or len(programs) > 200:
                raise ValueError
            results = [run_one(bytes.fromhex(p))[0] for p in programs]
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": 'send {"programs": ["<hex>", ...]} with at most 200 hex strings'})
        self.reply(200, {"results": results})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Oracle on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
