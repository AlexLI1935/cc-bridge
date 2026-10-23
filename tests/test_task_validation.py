import math

import pytest

from cc_bridge import BridgeError, build_command


@pytest.mark.parametrize("options", [
    {"goal": ""}, {"prompt": "  "}, {"workdir": "relative"}, {"mode": "invalid"},
    {"complexity": "invalid"}, {"effort": "invalid"}, {"model": "--bad"},
    {"max_runtime_minutes": 0}, {"max_runtime_minutes": 481},
    {"max_runtime_minutes": math.inf}, {"max_runtime_minutes": math.nan},
    {"max_runtime_minutes": True}, {"prior_session_id": "--continue"},
])
def test_reject_invalid_task_without_persisting(bridge, project, options):
    values = dict(goal="Review", workdir=str(project), prompt="Inspect source")
    values.update(options)
    with pytest.raises(BridgeError):
        bridge.create_task(**values)
    assert not list(bridge.tasks.glob("*/task.json"))


@pytest.mark.parametrize("task_id", ["../other", "", "CC-00000000-../../other", "C:/other"])
def test_reject_path_traversal(bridge, task_id):
    with pytest.raises(BridgeError, match="task_id"):
        bridge.get_task(task_id)


@pytest.mark.parametrize("limit", [0, -1, 1001, True])
def test_invalid_list_limit(bridge, limit):
    with pytest.raises(BridgeError):
        bridge.list_tasks(limit)


def test_permission_surface(bridge, project):
    created = bridge.create_task("Review", str(project), "Inspect")
    task = bridge._task(created["task_id"])
    command = build_command(task)
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--strict-mcp-config" in command
    assert "--add-dir" not in command
    task["mode"] = "write"
    command = build_command(task)
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert not any("bypass" in item.lower() for item in command)
