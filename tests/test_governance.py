import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import cc_governance as g
from cc_bridge import Bridge, BridgeError, write_json_atomic

SECRET = "TEST_SECRET_DO_NOT_LEAK_synthetic_subscription_credential"


def response(five=20, weekly=30):
    reset = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    return {"five_hour": {"utilization": five, "resets_at": reset},
            "seven_day": {"utilization": weekly, "resets_at": reset}}


@pytest.fixture
def gov(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / "runtime-home")
    bridge.governance.config.set_value("reset_scheduler.backend", "caller_polling")
    credential = tmp_path / "fake-account.json"
    credential.write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET,
        "refreshToken": SECRET + "_refresh", "expiresAt": time.time() * 1000 + 3600000}}))
    monkeypatch.setattr(g.Governance, "credential_path", lambda _: credential)
    monkeypatch.setattr(g, "fetch_usage", lambda _: (response(), format_datetime(datetime.now(timezone.utc))))
    return bridge.governance


def task(gov, tmp_path, mode="read_only"):
    directory = tmp_path / "project"
    directory.mkdir(exist_ok=True)
    created = gov.bridge.create_task("Inspect", str(directory), "Review source", mode=mode)
    return gov.bridge._task(created["task_id"])


def test_authorization_required_without_credential_read(gov, monkeypatch, tmp_path):
    probe = Mock(side_effect=AssertionError("Credentials must not be accessed"))
    monkeypatch.setattr(gov, "credential_path", probe)
    assert gov.refresh_usage()["state"] == "AUTH_REQUIRED"
    with pytest.raises(BridgeError, match="AUTH_REQUIRED"):
        gov.gate(task(gov, tmp_path))
    probe.assert_not_called()
    with pytest.raises(BridgeError):
        gov.authorize(False)


def test_authorized_usage_parse_and_revoke(gov):
    gov.authorize(True)
    data = gov.refresh_usage()
    assert data["state"] == "VALID"
    assert data["five_hour"]["remaining"] == 80
    assert data["weekly"]["remaining"] == 70
    assert data["last_valid_at"]
    assert data["five_hour"]["resets_at_epoch"] > time.time()
    assert data["timezone"]["local_timezone"]
    gov.revoke()
    assert gov.refresh_usage()["state"] == "AUTH_REQUIRED"
    assert gov.usage()["stale"]


@pytest.mark.parametrize("raw", [{}, [], {"five_hour": {}}, response(True), response(float("nan")),
                                    response(-1), response(101)])
def test_malformed_usage_never_fabricates(gov, monkeypatch, raw):
    gov.authorize(True)
    monkeypatch.setattr(g, "fetch_usage", lambda _: (raw, None))
    value = gov.refresh_usage()
    assert value["state"] == "UNKNOWN"
    assert value["stale"] and not value["current_known"]


@pytest.mark.parametrize("error,state", [(OSError(SECRET), "ENDPOINT_ERROR"),
    (ValueError(SECRET), "UNKNOWN"),
    (urllib.error.HTTPError(g.ENDPOINT, 401, SECRET, {}, None), "AUTH_STALE"),
    (urllib.error.HTTPError(g.ENDPOINT, 500, SECRET, {}, None), "ENDPOINT_ERROR")])
def test_failure_retains_last_valid_without_error_secrets(gov, monkeypatch, error, state):
    gov.authorize(True)
    good = gov.refresh_usage()
    # Simulate the next permitted query rather than bypassing the shared cooldown.
    good["fetched_at"] = good["last_valid_at"] = (datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    gov._save("usage.json", good)
    gov._save("usage-query.json", {"last_attempt_epoch": time.time()-361, "next_allowed_epoch": time.time()-1})
    monkeypatch.setattr(g, "fetch_usage", Mock(side_effect=error))
    bad = gov.refresh_usage()
    assert bad["state"] == state and bad["stale"]
    assert bad["five_hour"] == good["five_hour"]
    assert bad["last_valid_at"] == good["last_valid_at"]
    assert SECRET not in json.dumps(bad)


def test_old_cache_is_historical_only(gov):
    gov.authorize(True)
    old = gov.refresh_usage()
    old["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    gov._save("usage.json", old)
    assert gov.usage()["state"] == "STALE"
    assert not gov.usage()["current_known"]


def test_stale_auth_status_failure_never_uses_model_by_default(gov, monkeypatch):
    gov.authorize(True)
    gov.credential_path().write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET, "expiresAt": 0}}))
    import cc_bridge
    monkeypatch.setattr(cc_bridge, "discover_claude_exe", lambda: Path("fake-claude"))
    command = Mock(side_effect=OSError(SECRET))
    monkeypatch.setattr(g.subprocess, "run", command)
    data = gov.refresh_usage()
    assert data["state"] == "AUTH_STALE"
    assert command.call_args.args[0] == ["fake-claude", "auth", "status"]
    assert SECRET not in json.dumps(data)


def test_cli_owns_optional_refresh_probe(gov, monkeypatch):
    gov.authorize(True)
    gov.credential_path().write_text('{}')
    gov.config.set_value("usage.allow_cli_refresh_probe", True)
    import cc_bridge
    monkeypatch.setattr(cc_bridge, "discover_claude_exe", lambda: Path("fake-claude"))
    info=g.timezone_info()
    monkeypatch.setattr(g,"timezone_info",lambda: info)
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        if "-p" in argv:
            gov.credential_path().write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET,
                                                       "expiresAt": time.time()*1000+3600000}}))
    monkeypatch.setattr(g.subprocess, "run", run)
    assert gov.refresh_usage()["state"] == "VALID"
    assert len(calls) == 2


@pytest.mark.parametrize("five,weekly", [(100,10),(10,100),(100,100)])
def test_gate_blocks_either_exhausted_window(gov, tmp_path, monkeypatch, five, weekly):
    gov.authorize(True)
    monkeypatch.setattr(g, "fetch_usage", lambda _: (response(five, weekly), None))
    with pytest.raises(BridgeError, match="insufficient"):
        gov.gate(task(gov,tmp_path))


def test_configured_ceiling_reserves_capacity(gov, tmp_path, monkeypatch):
    gov.authorize(True)
    monkeypatch.setattr(g, "fetch_usage", lambda _: (response(91,94), None))
    with pytest.raises(BridgeError): gov.gate(task(gov,tmp_path))
    gov.config.set_value("usage.five_hour.max_utilization_pct", 95)
    assert gov.gate(task(gov,tmp_path))["five_hour"]["remaining"] == 9


def test_workspace_trust_revoke_and_scope(gov,tmp_path):
    item = task(gov,tmp_path,"write")
    with pytest.raises(BridgeError, match="WORKSPACE_AUTH_REQUIRED"):
        gov.check_workspace(item)
    gov.trust(str(tmp_path/"project"),confirmed=True)
    gov.check_workspace(item)
    sibling = tmp_path/"project-other"
    sibling.mkdir()
    with pytest.raises(BridgeError):
        gov.check_workspace({**item,"workdir":str(sibling)})
    gov.trust(str(tmp_path/"project"),revoke=True)
    with pytest.raises(BridgeError):
        gov.check_workspace(item)


@pytest.mark.parametrize("zone,offset", [("UTC","+00:00"),("Europe/Berlin","+01:00"),
    ("America/New_York","-05:00"),("Asia/Tokyo","+09:00"),("Asia/Yekaterinburg","+05:00")])
def test_reset_uses_os_rules_at_target_instant(monkeypatch,zone,offset):
    class OSDateTime(datetime):
        def astimezone(self,tz=None):
            return super().astimezone(tz or ZoneInfo(zone))
    monkeypatch.setattr(g,"datetime",OSDateTime)
    value=g.reset_fields("2026-12-01T12:00:00Z")
    assert value["resets_at_local"].endswith(offset)
    assert value["resets_at_epoch"] == datetime(2026,12,1,12,tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("zone,a,b,offset_a,offset_b", [
    ("Europe/Berlin","2026-10-25T00:30:00Z","2026-10-25T01:30:00Z","+02:00","+01:00"),
    ("America/New_York","2026-11-01T05:30:00Z","2026-11-01T06:30:00Z","-04:00","-05:00")])
def test_dst_transition(monkeypatch,zone,a,b,offset_a,offset_b):
    class OSDateTime(datetime):
        def astimezone(self,tz=None):
            return super().astimezone(tz or ZoneInfo(zone))
    monkeypatch.setattr(g,"datetime",OSDateTime)
    assert g.reset_fields(a)["resets_at_local"].endswith(offset_a)
    assert g.reset_fields(b)["resets_at_local"].endswith(offset_b)


def test_offset_and_utc_same_instant():
    assert g.reset_fields("2026-12-01T12:00:00Z")["resets_at_epoch"] == g.reset_fields("2026-12-01T21:00:00+09:00")["resets_at_epoch"]
    with pytest.raises(ValueError):
        g.reset_fields("2026-12-01T12:00:00")


def test_clock_skew_blocks_gate_and_reminders(gov,tmp_path,monkeypatch):
    gov.authorize(True)
    monkeypatch.setattr(g,"fetch_usage",lambda _: (response(100),format_datetime(datetime.now(timezone.utc)-timedelta(hours=1))))
    item=task(gov,tmp_path)
    assert gov.refresh_usage()["state"] == "CLOCK_SKEW"
    with pytest.raises(BridgeError): gov.gate(item)
    with pytest.raises(BridgeError): gov.schedule_reset_reminder(item["task_id"])


def test_reminder_rechecks_usage_and_machine_timezone(gov,tmp_path,monkeypatch):
    gov.authorize(True)
    for w in ("five_hour","weekly"):
        gov.config.set_value("usage."+w+".auto_resume_after_reset",False)
    monkeypatch.setattr(g,"fetch_usage",lambda _: (response(100),None))
    item=task(gov,tmp_path)
    gov.refresh_usage()
    reminder=gov.schedule_reset_reminder(item["task_id"])
    epoch=reminder["epoch"]
    original_tz=g.timezone_info
    monkeypatch.setattr(g,"timezone_info",lambda: {**original_tz(),"local_timezone":"changed-os-zone"})
    gov.refresh_usage()
    updated=gov.bridge.get_task(item["task_id"],full=True)
    assert updated["reset_epoch"]==epoch and updated["local_timezone"]=="changed-os-zone"
    updated["next_check_epoch"]=time.time()-1
    write_json_atomic(gov.bridge._result_path(item["task_id"]),updated)
    assert gov.verify_reminders()["verified"] == []
    monkeypatch.setattr(g,"fetch_usage",lambda _: (response(10),None))
    cached=gov._load("usage.json")
    cached["fetched_at"]=cached["last_valid_at"]=(datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    gov._save("usage.json",cached)
    gov._save("usage-query.json", {"last_attempt_epoch":time.time()-361,"next_allowed_epoch":time.time()-1})
    updated=gov.bridge.get_task(item["task_id"],full=True)
    updated["next_check_epoch"]=time.time()-1
    write_json_atomic(gov.bridge._result_path(item["task_id"]),updated)
    assert gov.verify_reminders()["verified"] == [item["task_id"]]
    assert any(e["type"]=="USAGE_AVAILABLE" for e in gov.pending()["pending"])


@pytest.mark.parametrize("status,kind",[("COMPLETED","TASK_COMPLETED"),("FAILED","TASK_FAILED"),
                                     ("NEEDS_INPUT","TASK_NEEDS_INPUT")])
def test_outbox_persistence_and_duplicate_protection(gov,status,kind):
    record={"task_id":"CC-20261023-123456abcdef","attempt":1,"status":status,"report_path":"report.md"}
    event=gov.event(record)
    assert event["type"]==kind
    assert gov.event(record)==event
    assert gov.change_event(event["event_id"])["first_claim"]
    assert not gov.change_event(event["event_id"])["first_claim"]
    restarted=Bridge(gov.bridge.home).governance
    assert restarted.pending()["pending"][0]["status"]=="claimed"
    restarted.change_event(event["event_id"],done=True)
    assert restarted.pending()["pending"]==[]
    assert restarted.event(record)["status"]=="done"


def test_report_receipt_current_attempt_and_tamper(tmp_path):
    record={"task_id":"example","attempt":1,"summary":"Completed","result":"Final answer"}
    assert not g.canonical_report_committed(tmp_path,record)
    g.ensure_report(tmp_path,record)
    assert g.canonical_report_committed(tmp_path,record)
    assert not g.canonical_report_committed(tmp_path,{**record,"attempt":2})
    with pytest.raises(BridgeError): g.ensure_report(tmp_path,record)
    (tmp_path/"report-other.md").write_text("Conflicting candidate")
    assert not g.canonical_report_committed(tmp_path,record)
    (tmp_path/"report-other.md").unlink()
    (tmp_path/"report.md").write_text("Tampered")
    assert not g.canonical_report_committed(tmp_path,record)


def test_secret_never_persisted_or_returned(gov,tmp_path,monkeypatch):
    gov.authorize(True)
    valid=gov.refresh_usage()
    from mcp_server import refresh_cc_usage, get_cc_usage
    monkeypatch.setenv("CC_BRIDGE_HOME",str(gov.bridge.home))
    outputs=[valid,refresh_cc_usage(),get_cc_usage()]
    record={"task_id":"CC-20261023-123456abcdef","attempt":1,"status":"COMPLETED","summary":"Done"}
    report_dir=gov.bridge.results/record["task_id"]/"attempt-1"
    report_dir.mkdir(parents=True)
    g.ensure_report(report_dir,record)
    write_json_atomic(report_dir/"result.json",record)
    outputs.append(gov.event(record))
    monkeypatch.setattr(g,"fetch_usage",Mock(side_effect=ValueError(SECRET)))
    outputs.append(gov.refresh_usage())
    assert SECRET not in json.dumps(outputs)
    for p in gov.bridge.home.rglob("*"):
        if p.is_file(): assert SECRET.encode() not in p.read_bytes(),p


def test_redirect_never_forwards_bearer():
    assert g.NoRedirect().redirect_request(None,None,302,"",{},"https://example.invalid") is None
