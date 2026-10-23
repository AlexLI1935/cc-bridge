import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cc_bridge import Bridge, BridgeError, FileLock, pid_alive, write_json_atomic
from conftest import ROOT, SESSION


def test_foreground_lifecycle_and_persistence(bridge, project, capture):
    created = bridge.create_task("Review", str(project), "Inspect the README")
    task_id = created["task_id"]
    assert bridge.get_task(task_id)["status"] == "QUEUED"
    final = bridge.run_task(task_id)
    assert final["status"] == "COMPLETED"
    assert final["session_id"] == SESSION
    assert final["result"] == "Review completed"
    assert Path(final["report_path"]).read_text().strip() == final["result"]
    assert Bridge(bridge.home).get_task(task_id, full=True)["claude_result"]["subtype"] == "success"
    assert capture()["cwd"] == str(project)
    assert sorted(p.name for p in project.iterdir()) == ["README.md"]
    assert bridge.list_tasks()["tasks"][0]["task_id"] == task_id
    with pytest.raises(BridgeError):
        bridge.submit_task(task_id)


@pytest.mark.parametrize("action,state,stage", [
    ("malformed", "FAILED", "result_parse"), ("array", "FAILED", "result_parse"),
    ("failure", "FAILED", "claude"), ("limit", "WAITING_USAGE_RESET", "usage"),
    ("plain_limit", "WAITING_USAGE_RESET", "usage"),
    ("permission", "NEEDS_INPUT", "permission"), ("needs_input", "NEEDS_INPUT", "decision"),
    ("quoted_limit", "COMPLETED", None),
])
def test_outcomes(bridge, project, monkeypatch, action, state, stage):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", action)
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    record = bridge.run_task(task_id)
    assert (record["status"], record["stage"]) == (state, stage)


def test_authorized_write(bridge, project, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", "write")
    task_id = bridge.create_task("Edit", str(project), "Change a file", mode="write")["task_id"]
    assert bridge.run_task(task_id)["status"] == "COMPLETED"
    assert (project / "changed.txt").read_text() == "authorized change"


def test_timeout_stops_child(bridge, project, monkeypatch, capture):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", "wait")
    task_id = bridge.create_task("Review", str(project), "Inspect", max_runtime_minutes=0.03)["task_id"]
    record = bridge.run_task(task_id)
    assert (record["status"], record["stage"]) == ("FAILED", "timeout")
    assert not pid_alive(capture()["pid"])


def test_background_cancel_stops_tree_and_blocks_overlap(bridge, project, monkeypatch, capture, wait_state):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", "tree")
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    try:
        bridge.submit_task(task_id)
        ids = capture()
        assert bridge.get_task(task_id)["status"] == "RUNNING"
        with pytest.raises(BridgeError):
            bridge.submit_task(task_id)
        other = bridge.create_task("Other", str(project), "Inspect")["task_id"]
        with pytest.raises(BridgeError, match="One task"):
            bridge.submit_task(other)
        with pytest.raises(BridgeError):
            bridge.run_task(other)
        assert bridge.cancel_task(task_id)["status"] in ("CANCELLING", "CANCELLED")
        assert wait_state(bridge, task_id, {"CANCELLED"})["status"] == "CANCELLED"
        assert not pid_alive(ids["pid"])
        assert not pid_alive(ids["child_pid"])
        assert bridge.cancel_task(task_id)["status"] == "CANCELLED"
    finally:
        if bridge.get_task(task_id)["status"] in ("QUEUED", "RUNNING", "CANCELLING"):
            bridge.cancel_task(task_id)
            wait_state(bridge, task_id, {"CANCELLED"})


def test_queued_cancel_cannot_run(bridge, project):
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    bridge.cancel_task(task_id)
    with pytest.raises(BridgeError):
        bridge.run_task(task_id)


def test_runner_launch_failure_is_persisted(bridge, project, monkeypatch):
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    def fail(*a, **k):
        raise OSError("Launch refused")
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(BridgeError, match="Cannot start runner"):
        bridge.submit_task(task_id)
    assert bridge.get_task(task_id)["stage"] == "runner_launch"


def test_crashed_runner_releases_kernel_lock(bridge, project):
    lock_path = bridge.runtime / "runner.lock"
    code = "from pathlib import Path; from cc_bridge import FileLock; import os; lock=FileLock(Path(__import__('sys').argv[1])); lock.__enter__(); os._exit(7)"
    child = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], cwd=ROOT)
    assert child.wait(timeout=10) == 7
    with FileLock(lock_path, timeout=0):
        pass
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    record = bridge.get_task(task_id, full=True)
    record.update(status="RUNNING", runner_pid=child.pid, dispatched=True)
    write_json_atomic(bridge._result_path(task_id), record)
    assert bridge.get_task(task_id)["stage"] == "runner_exit"


def test_cli_help_and_complete_lifecycle(fake_claude, project, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Inspect the README", encoding="utf-8")
    def cli(*args, expected=0):
        proc = subprocess.run([sys.executable, str(ROOT / "cc_bridge.py"), *args],
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == expected, proc.stdout + proc.stderr
        return proc.stdout
    assert "submit" in cli("--help")
    assert cli("--version").strip() == "1.0.0"
    created = json.loads(cli("new", "--goal", "Review", "--workdir", str(project), "--prompt-file", str(prompt)))
    task_id = created["task_id"]
    assert json.loads(cli("run", task_id))["status"] == "COMPLETED"
    assert json.loads(cli("status", task_id))["status"] == "COMPLETED"
    assert json.loads(cli("result", task_id))["result"] == "Review completed"
    assert json.loads(cli("list"))["tasks"][0]["task_id"] == task_id
    assert json.loads(cli("status", "../invalid", expected=2))["success"] is False


def test_parallel_creation_has_unique_ids(fake_claude, project, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Inspect", encoding="utf-8")
    command = [sys.executable, str(ROOT / "cc_bridge.py"), "new", "--goal", "Review",
               "--workdir", str(project), "--prompt-file", str(prompt)]
    children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
    ids = []
    for child in children:
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, err
        ids.append(json.loads(out)["task_id"])
    assert len(set(ids)) == 6


def test_failed_tree_cleanup_blocks_new_paid_work(bridge, project):
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    record = bridge.get_task(task_id, full=True)
    record.update(status="FAILED", stage="cleanup", cleanup_required=True)
    write_json_atomic(bridge._result_path(task_id), record)
    other = bridge.create_task("Other", str(project), "Inspect")["task_id"]
    with pytest.raises(BridgeError, match="manual process cleanup"):
        bridge.submit_task(other)
    with pytest.raises(BridgeError, match="cleanup"):
        bridge.resume_task(task_id)
