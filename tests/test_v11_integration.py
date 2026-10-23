import json
import subprocess
import sys
from pathlib import Path

import pytest

from cc_bridge import BridgeError
from conftest import ROOT


def test_real_concurrent_submit_rejects_second(bridge,project,monkeypatch,capture,wait_state):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION","wait")
    ids=[bridge.create_task("Inspect",str(project),"Review")["task_id"] for _ in range(2)]
    children=[subprocess.Popen([sys.executable,str(ROOT/"cc_bridge.py"),"submit",task_id],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE) for task_id in ids]
    results=[]
    for p in children:
        out,err=p.communicate(timeout=15)
        results.append(json.loads(out))
    assert sum(r["success"] is True for r in results)==1
    active=next(r["task_id"] for r in results if r["success"])
    try:
        capture()
        bridge.cancel_task(active)
        wait_state(bridge,active,{"CANCELLED"})
        events=bridge.governance.pending()["pending"]
        assert any(e["task_id"]==active and e["type"]=="TASK_CANCELLED" for e in events)
    finally:
        if bridge.get_task(active)["status"] in ("QUEUED","RUNNING","CANCELLING"):
            bridge.cancel_task(active)


def test_terminal_report_and_event_integration(bridge,project):
    task_id=bridge.create_task("Inspect",str(project),"Review")["task_id"]
    result=bridge.run_task(task_id)
    assert result["report_validated"]
    assert Path(result["report_path"]).is_file()
    assert bridge.governance.pending()["pending"][0]["type"]=="TASK_COMPLETED"
