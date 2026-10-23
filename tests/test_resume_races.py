import json
import os
import subprocess
import sys
import time

import cc_governance as g
from cc_bridge import write_json_atomic
from conftest import ROOT
from test_resource_policy import snapshot,make,due


def test_periodic_ceiling_stops_running_task_and_keeps_session(bridge,project,monkeypatch,wait_state,capture):
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION","wait")
    monkeypatch.setattr(g.UsageRefresher,"start",lambda self:setattr(self,"latest",snapshot(90,10)))
    task_id=make(bridge,project)
    waiting=bridge.run_task(task_id)
    assert waiting["status"]=="WAITING_USAGE_RESET",waiting
    assert waiting["session_id"]
    saved=waiting["session_id"]
    due(bridge,task_id)
    monkeypatch.setenv("CC_BRIDGE_FAKE_ACTION","success")
    assert bridge.governance.verify_reminders()["resumed"]==[task_id]
    final=wait_state(bridge,task_id,{"COMPLETED","FAILED"})
    assert final["status"]=="COMPLETED",final
    args=capture()["argv"]
    assert args[args.index("--resume")+1]==saved


def test_two_scheduler_processes_start_one_attempt(bridge,project,monkeypatch,wait_state):
    task_id=make(bridge,project)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,10))
    assert bridge.submit_task(task_id)["status"]=="WAITING_USAGE_RESET"
    due(bridge,task_id)
    command=[sys.executable,str(ROOT/"cc_bridge.py"),"scheduler-tick"]
    processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
    outputs=[]
    for p in processes:
        out,err=p.communicate(timeout=15)
        assert p.returncode==0,err
        outputs.append(json.loads(out))
    assert sum(len(r["resumed"]) for r in outputs)==1
    final=wait_state(bridge,task_id,{"COMPLETED","FAILED"})
    assert final["status"]=="COMPLETED" and final["attempt"]==1,final
