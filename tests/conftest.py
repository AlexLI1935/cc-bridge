from __future__ import annotations

import io
import json
import os
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def offline_external_time(monkeypatch):
    import cc_observed
    from datetime import datetime, timezone
    monkeypatch.setattr(cc_observed, "fetch_external_utc", lambda: datetime.now(timezone.utc))


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cc_bridge import Bridge

SESSION = "12345678-1234-1234-1234-123456789abc"

FAKE_SOURCE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
action = os.environ.get("CC_BRIDGE_FAKE_ACTION", "success")
capture = Path(os.environ["CC_BRIDGE_FAKE_CAPTURE"])
data = {"argv": args, "prompt": prompt, "cwd": os.getcwd(), "pid": os.getpid()}
if action == "tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    data["child_pid"] = child.pid
capture.write_text(json.dumps(data), encoding="utf-8")
if action in ("wait", "tree"):
    time.sleep(120)
if action == "malformed":
    print("not a JSON result")
    sys.exit(0)
if action == "array":
    print("[]")
    sys.exit(0)
if action == "plain_limit":
    print("Usage limit reached")
    sys.exit(1)
payload = {"type": "result", "subtype": "success", "is_error": False,
           "session_id": "12345678-1234-1234-1234-123456789abc",
           "result": "Review completed"}
if action == "write":
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    Path("changed.txt").write_text("authorized change", encoding="utf-8")
    payload["result"] = "Changed changed.txt"
if action == "limit":
    payload.update(subtype="error_during_execution", is_error=True, api_error_status=429,
                   result="Usage limit reached")
if action == "permission":
    payload["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "pytest"}}]
if action == "needs_input":
    payload["result"] = "NEEDS_INPUT: Which implementation do you prefer?"
if action == "quoted_limit":
    payload["result"] = "The error handler displays 'usage limit reached'."
if action == "failure":
    payload.update(subtype="error_during_execution", is_error=True, result="Intentional failure")
print(json.dumps(payload))
sys.exit(1 if payload["is_error"] else 0)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fake bin"
    bin_dir.mkdir()
    if os.name == "nt":
        # Build a small native launcher without a compiler or paid CLI.
        from pip._vendor import distlib
        launcher = Path(distlib.__file__).parent / ("t64.exe" if struct.calcsize("P") == 8 else "t32.exe")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("__main__.py", FAKE_SOURCE)
        executable = bin_dir / "claude.exe"
        executable.write_bytes(launcher.read_bytes() + f'#!"{sys.executable}"\n'.encode("utf-8") + archive.getvalue())
    else:
        executable = bin_dir / "claude"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_SOURCE, encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CC_BRIDGE_HOME", str(tmp_path / "bridge data"))
    monkeypatch.setenv("CC_BRIDGE_FAKE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.delenv("CC_BRIDGE_FAKE_ACTION", raising=False)
    # Mock only the provider for legacy lifecycle tests, including detached
    # subprocesses. This adapter lives in temporary test storage, never core.
    from cc_governance import Governance
    def fake_usage(self, reason="explicit"):
        from cc_governance import timezone_info, reset_fields
        from datetime import datetime, timezone, timedelta
        reset = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        window = {"utilization": 10.0, "remaining": 90.0, **reset_fields(reset)}
        return {"state": "VALID", "current_known": True, "stale": False,
                "five_hour": window, "weekly": window, "timezone": timezone_info()}
    monkeypatch.setattr(Governance, "refresh_usage", fake_usage)
    adapter = tmp_path / "provider-adapter"
    adapter.mkdir()
    import inspect, textwrap
    adapter_source = ("import sys\nsys.path.insert(0, " + repr(str(ROOT)) + ")\n"
                      "from cc_governance import Governance\nimport cc_observed\nfrom datetime import datetime, timezone\ncc_observed.fetch_external_utc = lambda: datetime.now(timezone.utc)\n" + textwrap.dedent(inspect.getsource(fake_usage))
                      + "\nGovernance.refresh_usage = fake_usage\n")
    (adapter / "sitecustomize.py").write_text(adapter_source, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(adapter))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    Bridge().governance.authorize(True)
    Bridge().governance.config.set_value("reset_scheduler.backend", "caller_polling")
    Bridge().governance.trust(str(tmp_path), confirmed=True)
    return executable


@pytest.fixture
def bridge(fake_claude, tmp_path):
    return Bridge(tmp_path / "bridge data")


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "sample project"
    path.mkdir()
    (path / "README.md").write_text("Sample repository", encoding="utf-8")
    return path


def wait_for(bridge, task_id, states, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = bridge.get_task(task_id, full=True)
        if record["status"] in states:
            return record
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {states}: {record}")


@pytest.fixture
def wait_state():
    return wait_for


@pytest.fixture
def capture(monkeypatch):
    def read(timeout=10):
        deadline = time.monotonic() + timeout
        path = Path(os.environ["CC_BRIDGE_FAKE_CAPTURE"])
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("Fake Claude did not start")
    return read
