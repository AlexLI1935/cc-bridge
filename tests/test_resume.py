import pytest

from cc_bridge import BridgeError
from conftest import SESSION


def test_resume_exact_session_preserves_attempts(bridge, project, capture, wait_state, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", "limit")
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    assert bridge.run_task(task_id)["status"] == "WAITING_USAGE_RESET"
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION", "success")
    bridge.resume_task(task_id, "Continue with the README", model="sonnet", effort="low")
    final = wait_state(bridge, task_id, {"COMPLETED", "FAILED"})
    assert final["status"] == "COMPLETED", final
    assert final["attempt"] == 2
    data = capture()
    assert data["argv"][data["argv"].index("--resume") + 1] == SESSION
    assert "Continue with the README" in data["prompt"]
    assert "--continue" not in data["argv"]
    assert (bridge.results / task_id / "attempt-1" / "result.json").is_file()
    assert (bridge.results / task_id / "attempt-2" / "result.json").is_file()


def test_resume_requires_saved_session(bridge, project):
    task_id = bridge.create_task("Review", str(project), "Inspect")["task_id"]
    with pytest.raises(BridgeError):
        bridge.resume_task(task_id)
    bridge.cancel_task(task_id)
    with pytest.raises(BridgeError, match="session_id"):
        bridge.resume_task(task_id)


def test_external_session_is_exact(bridge, project, capture):
    task_id = bridge.create_task("Review", str(project), "Continue", prior_session_id=SESSION)["task_id"]
    assert bridge.run_task(task_id)["status"] == "COMPLETED"
    assert capture()["argv"][capture()["argv"].index("--resume") + 1] == SESSION
