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
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def check_conversation():
    # Exercise real OS pipes on every supported platform without credentials or model usage.
    sys.path.insert(0, str(ROOT))
    from scripts import review
    child = """
import json, sys
for number, line in enumerate(sys.stdin, start=1):
    question = json.loads(line)["message"]["content"]
    for delta in ({"type": "thinking_delta", "thinking": "internal"}, {"type": "text_delta", "text": question}):
        print(json.dumps({"type": "stream_event", "event": {"delta": delta}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "result": str(number)}), flush=True)
"""
    spawn = subprocess.Popen
    key = ("fixture", "test")
    def fixture(argv, **kwargs):
        assert argv[argv.index("--tools") + 1] == ""
        return spawn([sys.executable, "-u", "-c", child], **kwargs)
    try:
        with patch.object(review.shutil, "which", return_value="claude"), patch.object(review.subprocess, "Popen", fixture):
            for first, question in [(True, "First"), (False, "Follow-up")]:
                session = review.acquire(key, first)
                packets = []
                if first:
                    original = session.process
                assert session.process is original
                session.reply(question, packets.append)
                assert [p["type"] for p in packets] == ["input", "text"]
                assert packets[0]["message"]["message"]["content"] == packets[1]["text"] == question
                review.finish(session, True)
    finally:
        review.close(key)
    assert original.poll() is not None


def check(directory):
    for name in ("server.py", "index.html", "review.html", "AGENTS.md"):
        shutil.copy(ROOT / name, directory)
    (Path(directory) / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "review.py", Path(directory) / "scripts")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def get(route, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(base + route, body, timeout=3) as response:
            return json.load(response)

    archive = Path(directory) / "reports"
    archive.mkdir()
    snapshot = archive / "workspace.json"
    message = "Review complete — résumé 日本語"
    for first in (True, False):
        proc = subprocess.Popen([sys.executable, "server.py", "--dashboard"], cwd=directory,
                                env={**os.environ, "PROJECT_VIEW_PORT": str(port), "HADES_EVIDENCE_DIR": str(archive)})
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
            with urlopen(base + "/review?project=workspace", timeout=3) as page:
                assert b'id="composer"' in page.read()
            request = Request(base + "/api/review/ask", b"{}", {"Origin": "https://other.example",
                              "X-Hades-Review": "1", "Content-Type": "application/json"})
            try:
                urlopen(request, timeout=3)
                raise AssertionError("Cross-origin review request succeeded")
            except HTTPError as error:
                assert error.code == 403
            route = "/api/run-details?project=workspace&agent=fixture&session=sample&round="
            prompt = "\nReview Ω\r\n$HOME 'literal' <tag>\n"
            if first:
                env = {**os.environ, "HADES_DETAILS_DB": str(Path(directory) / "agent-work/.hades/invocations.sqlite3"),
                       "HADES_DETAILS_PROJECT": "workspace", "HADES_DETAILS_AGENT": "fixture",
                       "HADES_LAUNCH_SESSION": "sample", "HADES_DETAILS_ROUND": "1"}
                command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
                forwarded = subprocess.run([sys.executable, str(ROOT / "scripts/loop.py"), "--capture-cli", *command],
                                           env=env, input=prompt.encode(), stdout=subprocess.PIPE, check=True)
                assert forwarded.stdout == prompt.encode()
                assert get(route + "1")[0]["argv"] == command
                assert get(route + "2") == []
                claim = {"project": "workspace", "type": "item", "section": "experiments",
                         "key": "Sample", "status": "rce", "revision": "forged"}
                saved = get("/api/events", claim)
                assert len(saved["revision"]) == 32 and saved["revision"] != "forged"
                assert not get("/api/projects")[0]["experimentChecks"]
                record = {"experiment": "Sample", "revision": saved["revision"], "outcome": "inconclusive",
                          "build": "sample", "configuration": "default", "evidence": "artifact-1"}
                snapshot.write_text(json.dumps({"project": "other", "checks": [record]}))
                assert "error" in get("/api/projects")[0]["experimentChecks"]
                snapshot.write_text(json.dumps({"project": "workspace", "checks": [record]}))
                assert get("/api/projects")[0]["experimentChecks"]["records"] == [record]
                assert get("/api/events", claim)["revision"] != record["revision"]
                snapshot.write_text("{")
                assert "error" in get("/api/projects")[0]["experimentChecks"]
                snapshot.unlink()
                count = get("/api/projects")[0]["count"]
                get("/api/events", {"project": "workspace", "type": "item", "section": "now",
                                    "key": "Current update", "body": message})
                assert get("/api/projects")[0]["count"] == count + 1
                assert get(f"/api/events?project=workspace&after={count}")["events"][0]["body"] == message
            else:
                assert get("/api/events?project=workspace")["events"][-1]["body"] == message
            assert get(route + "1")[0]["stdin"] == prompt
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    check_conversation()
    with tempfile.TemporaryDirectory(prefix="Hades dashboard ") as workspace:
        check(workspace)
    print("Dashboard startup, live updates and restart passed.")
