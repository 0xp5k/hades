"""Exercise the public dashboard command in an empty workspace, without a model."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def check(directory):
    for name in ("server.py", "index.html", "AGENTS.md"):
        shutil.copy(ROOT / name, directory)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def get(route, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(base + route, body, timeout=3) as response:
            return json.load(response)

    message = "Review complete — résumé 日本語"
    for first in (True, False):
        proc = subprocess.Popen([sys.executable, "server.py", "--dashboard"], cwd=directory,
                                env={**os.environ, "PROJECT_VIEW_PORT": str(port)})
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Dashboard exited during startup")
                try:
                    projects = get("/api/projects")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Dashboard did not become ready")
            assert [p["id"] for p in projects] == ["workspace"]
            with urlopen(base + "/", timeout=3) as page:
                assert b'id="view"' in page.read()
            with urlopen(base + "/AGENTS.md", timeout=3) as guide:
                assert b"topic" in guide.read()
            if first:
                count = projects[0]["count"]
                get("/api/events", {"project": "workspace", "type": "item", "section": "now",
                                    "key": "Current update", "body": message})
                assert get("/api/projects")[0]["count"] == count + 1
                assert get(f"/api/events?project=workspace&after={count}")["events"][0]["body"] == message
            else:
                assert get("/api/events?project=workspace")["events"][-1]["body"] == message
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="Hades dashboard ") as workspace:
        check(workspace)
    print("Dashboard startup, live updates and restart passed.")
