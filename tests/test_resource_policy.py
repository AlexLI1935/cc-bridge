import copy
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import cc_governance as g
from cc_bridge import Bridge, BridgeError, write_json_atomic
from cc_config import ConfigStore, defaults, validate


def snapshot(five=10,weekly=10, five_epoch=None, weekly_epoch=None):
    now=time.time()
    def window(used,epoch):
        reset=datetime.fromtimestamp(epoch,timezone.utc).isoformat()
        return {"utilization":used,"remaining":100-used,**g.reset_fields(reset)}
    return {"state":"VALID","stale":False,"current_known":True,"timezone":g.timezone_info(),
            "five_hour":window(five,five_epoch or now+2220),"weekly":window(weekly,weekly_epoch or now+86400)}


def make(bridge,project):
    return bridge.create_task("Inspect",str(project),"Review source")["task_id"]


def due(bridge,task_id):
    record=bridge.get_task(task_id,full=True)
    record["next_check_epoch"]=time.time()-1
    write_json_atomic(bridge._result_path(task_id),record)


def test_default_config_generated_and_roundtrip(tmp_path):
    bridge=Bridge(tmp_path/"home")
    c=bridge.governance.config.load()
    assert c["usage"]["five_hour"]["max_utilization_pct"]==90
    assert c["usage"]["weekly"]["max_utilization_pct"]==95
    assert not c["usage"]["authorization"]["authorized"]
    assert c["timezone"]["detected_name"]
    assert ConfigStore(bridge.home).load()==c


@pytest.mark.parametrize("key,value",[("usage.five_hour.max_utilization_pct",-1),
    ("usage.five_hour.max_utilization_pct",101),("usage.weekly.max_utilization_pct",float("nan")),
    ("timezone.mode","guess"),("usage.refresh_interval_minutes",0),
    ("usage.weekly.auto_resume_after_reset","yes")])
def test_invalid_config_rejected_atomically(tmp_path,key,value):
    store=ConfigStore(tmp_path)
    old=store.load()
    with pytest.raises(BridgeError): store.set_value(key,value)
    assert store.load()==old


def test_unknown_secret_fields_rejected(tmp_path):
    config=defaults()
    config["usage"]["accessToken"]="TEST_SECRET_DO_NOT_LEAK"
    with pytest.raises(BridgeError): validate(config)
    path=tmp_path/"config.json"
    path.write_text('{malformed')
    with pytest.raises(BridgeError): ConfigStore(tmp_path).load()


@pytest.mark.parametrize("five,weekly,allowed",[(89,94,True),(90,94,False),(89,95,False),
                                               (90,95,False)])
def test_default_gate_edges(bridge,project,monkeypatch,five,weekly,allowed):
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(five,weekly))
    task_id=make(bridge,project)
    if allowed:
        assert bridge.governance.gate(bridge._task(task_id))["state"]=="VALID"
    else:
        result=bridge.submit_task(task_id)
        assert result["status"]=="WAITING_USAGE_RESET"
        assert result["session_id"] is None and result["waiting_before_launch"]
        assert result["last_usage"]["five_hour"]["utilization"]==five
        assert not result["dispatched"]


def test_configurable_five_hour_threshold(bridge,project,monkeypatch):
    bridge.governance.config.set_value("usage.five_hour.max_utilization_pct",95)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(94,94))
    assert bridge.governance.gate(bridge._task(make(bridge,project)))["state"]=="VALID"


def test_later_actual_reset_and_grace_win(bridge,project,monkeypatch):
    early=time.time()+1100
    later=time.time()+92300
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,95,early,later))
    result=bridge.submit_task(make(bridge,project))
    assert result["blocked_windows"]==["five_hour","weekly"]
    assert result["reset_epoch"]==pytest.approx(later)
    assert result["next_check_epoch"]==pytest.approx(later+60)
    assert result["reset_epoch"]!=pytest.approx(time.time()+5*3600,abs=2)


def test_restart_and_duplicate_notifications_resume_only_once(bridge,project,monkeypatch,wait_state):
    task_id=make(bridge,project)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,95))
    waiting=bridge.submit_task(task_id)
    cycle=waiting["wait_cycle"]
    restarted=Bridge(bridge.home)
    assert restarted.get_task(task_id,full=True)["wait_cycle"]==cycle
    due(restarted,task_id)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(10,10))
    calls=Mock(wraps=restarted._dispatch)
    monkeypatch.setattr(restarted,"_dispatch",calls)
    first=restarted.governance.verify_reminders()
    assert first["resumed"]==[task_id]
    second=restarted.governance.verify_reminders()
    assert second["resumed"]==[]
    final=wait_state(restarted,task_id,{"COMPLETED","FAILED"})
    assert final["status"]=="COMPLETED",final
    assert calls.call_count==1
    kinds={e["type"] for e in restarted.governance.pending()["pending"]}
    assert {"TASK_WAITING_USAGE_RESET","USAGE_AVAILABLE","TASK_RESUMED"}<=kinds


def test_reset_still_blocked_uses_new_response(bridge,project,monkeypatch):
    task_id=make(bridge,project)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,10))
    bridge.submit_task(task_id)
    due(bridge,task_id)
    new_epoch=time.time()+9999
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(96,10,new_epoch))
    dispatch=Mock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(bridge,"_dispatch",dispatch)
    assert bridge.governance.verify_reminders()["resumed"]==[]
    record=bridge.get_task(task_id,full=True)
    assert record["reset_epoch"]==pytest.approx(new_epoch)
    assert record["status"]=="WAITING_USAGE_RESET"
    dispatch.assert_not_called()


def test_disabling_auto_resume_requires_manual_action(bridge,project,monkeypatch):
    task_id=make(bridge,project)
    bridge.governance.config.set_value("usage.five_hour.auto_resume_after_reset",False)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,10))
    bridge.submit_task(task_id)
    due(bridge,task_id)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot())
    assert bridge.governance.verify_reminders()["resumed"]==[]
    assert bridge.get_task(task_id)["status"]=="WAITING_USAGE_RESET"


def test_usage_disabled_never_reads_credentials(tmp_path,project,monkeypatch):
    bridge=Bridge(tmp_path/"home")
    bridge.governance.config.set_value("usage.enabled",False)
    probe=Mock(side_effect=AssertionError("must not read credentials"))
    monkeypatch.setattr(g.Governance,"credential_path",probe)
    assert bridge.governance.refresh_usage()["state"]=="USAGE_DISABLED"
    with pytest.raises(BridgeError,match="USAGE_DISABLED"): bridge.submit_task(make(bridge,project))
    probe.assert_not_called()


def test_revoke_prevents_automatic_resume(bridge,project,monkeypatch):
    task_id=make(bridge,project)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,10))
    bridge.submit_task(task_id)
    due(bridge,task_id)
    bridge.governance.revoke()
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":{
        "state":"AUTH_REQUIRED","stale":True,"current_known":False})
    assert bridge.governance.verify_reminders()["resumed"]==[]
    assert bridge.get_task(task_id)["status"]=="WAITING_USAGE_RESET"


def test_cancel_waiting_prevents_later_resume(bridge,project,monkeypatch):
    task_id=make(bridge,project)
    monkeypatch.setattr(g.Governance,"refresh_usage",lambda self,reason="explicit":snapshot(90,10))
    bridge.submit_task(task_id)
    bridge.cancel_task(task_id)
    assert bridge.governance.verify_reminders()["resumed"]==[]
    assert bridge.get_task(task_id)["status"]=="CANCELLED"


def test_windows_schedule_uses_absolute_instant_and_explicit_home(bridge,monkeypatch):
    captured=[]
    import xml.etree.ElementTree as ET
    def run(argv,**kwargs):
        assert argv[0]=="schtasks"
        xml=__import__('pathlib').Path(argv[argv.index('/XML')+1]).read_bytes()
        captured.append(ET.fromstring(xml))
        return __import__('types').SimpleNamespace(returncode=0)
    monkeypatch.setattr(g.subprocess,"run",run)
    epoch=time.time()+3600
    result=g.register_wakeup(bridge,epoch)
    assert result["scheduled"]
    ns={"t":"http://schemas.microsoft.com/windows/2004/02/mit/task"}
    tree=captured[0]
    boundary=tree.find('.//t:StartBoundary',ns).text
    assert g.instant(boundary).timestamp()==pytest.approx(epoch,abs=0.001)
    arguments=tree.find('.//t:Arguments',ns).text
    assert 'scheduler-tick' in arguments and '--home' in arguments and str(bridge.home) in arguments
    assert tree.find('.//t:StartWhenAvailable',ns).text=='true'
