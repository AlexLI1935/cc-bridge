import json
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import pytest

import cc_bridge as cc
import cc_governance as g


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    clock = [float(int(time.time()))]
    monkeypatch.setattr(g.time, "time", lambda: clock[0])
    monkeypatch.setattr(cc, "utc_iso", lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat())
    gov = cc.Bridge(tmp_path/"home").governance
    gov.authorize(True)
    gov.config.set_value("usage.refresh_interval_minutes", 5)
    credential = Mock(return_value="SYNTHETIC_TEST_CREDENTIAL")
    monkeypatch.setattr(g.Governance, "_fresh_usage_access_token", credential)
    reset = datetime.fromtimestamp(clock[0]+3600, timezone.utc).isoformat()
    provider = Mock(return_value=({"five_hour":{"utilization":10,"resets_at":reset},
                                  "seven_day":{"utilization":20,"resets_at":reset}}, None))
    monkeypatch.setattr(g, "fetch_usage", provider)
    return gov, clock, provider, credential


def test_five_minute_floor_and_boundary(guarded):
    gov, clock, provider, credential = guarded
    first = gov.refresh_usage()
    clock[0] += 299
    cached = gov.refresh_usage()
    assert cached["state"] == "VALID" and cached["cache_reused"] and cached["refresh_deferred"]
    assert cached["fetched_at"] == first["fetched_at"]
    assert provider.call_count == credential.call_count == 1
    clock[0] += 1
    assert gov.usage()["state"] == "STALE"
    assert not gov.refresh_usage()["refresh_deferred"]
    assert provider.call_count == 2


def test_cooldown_survives_server_restart(guarded):
    gov, _, provider, credential = guarded
    gov.refresh_usage()
    restarted = cc.Bridge(gov.bridge.home).governance
    assert restarted.refresh_usage()["cache_reused"]
    assert provider.call_count == credential.call_count == 1


def test_concurrent_callers_make_one_request(guarded):
    gov, _, provider, _ = guarded
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: gov.refresh_usage(), range(4)))
    assert all(row["state"] == "VALID" for row in results)
    assert provider.call_count == 1


@pytest.mark.parametrize("value", [0.5, 4.99, -1])
def test_interval_below_five_minutes_rejected(guarded, value):
    gov, _, _, _ = guarded
    with pytest.raises(cc.BridgeError):
        gov.config.set_value("usage.refresh_interval_minutes", value)


@pytest.mark.parametrize("retry", ["900", "http-date"])
def test_429_retry_after_persists_and_prevents_retry(guarded, retry):
    gov, clock, provider, credential = guarded
    header = (format_datetime(datetime.fromtimestamp(clock[0]+900, timezone.utc)) if retry == "http-date" else retry)
    provider.side_effect = urllib.error.HTTPError(g.ENDPOINT, 429, "SYNTHETIC_SECRET", {"Retry-After":header}, None)
    failed = gov.refresh_usage()
    assert failed["http_status"] == 429 and not failed["current_known"]
    assert failed["next_refresh_epoch"] == pytest.approx(clock[0]+900, abs=1)
    clock[0] += 301
    restarted = cc.Bridge(gov.bridge.home).governance
    deferred = restarted.refresh_usage()
    assert deferred["refresh_deferred"] and not deferred["cache_reused"]
    assert provider.call_count == credential.call_count == 1
    assert "SYNTHETIC_SECRET" not in json.dumps(deferred)
    clock[0] += 600
    restarted.refresh_usage()
    assert provider.call_count == 2


@pytest.mark.parametrize("header", ["1", "bad-value", "NaN", "1e100"])
def test_invalid_or_short_retry_after_never_shortens_floor(guarded, header):
    gov, clock, provider, _ = guarded
    provider.side_effect = urllib.error.HTTPError(g.ENDPOINT, 429, "limited", {"Retry-After":header}, None)
    value = gov.refresh_usage()
    assert value["next_refresh_epoch"] == pytest.approx(clock[0]+300)
    clock[0] += 299
    assert gov.refresh_usage()["refresh_deferred"]
    assert provider.call_count == 1


def test_network_failure_also_obeys_cooldown(guarded):
    gov, clock, provider, _ = guarded
    provider.side_effect = OSError("SYNTHETIC_SECRET")
    assert gov.refresh_usage()["state"] == "ENDPOINT_ERROR"
    clock[0] += 299
    assert not gov.refresh_usage()["current_known"]
    assert provider.call_count == 1


def test_expired_snapshot_does_not_launch_during_cooldown(guarded, tmp_path):
    gov, clock, provider, _ = guarded
    gov.config.set_value("usage.fresh_seconds", 60)
    gov.refresh_usage()
    clock[0] += 61
    row = gov.refresh_usage()
    assert row["state"] == "STALE" and row["refresh_deferred"] and not row["current_known"]
    with pytest.raises(cc.BridgeError, match="STALE"):
        gov.gate({"mode":"read_only","workdir":str(tmp_path)})
    assert provider.call_count == 1


def test_crossing_reset_requires_new_snapshot(guarded, tmp_path):
    gov, clock, provider, _ = guarded
    reset = datetime.fromtimestamp(clock[0]+100, timezone.utc).isoformat()
    provider.return_value = ({"five_hour":{"utilization":10,"resets_at":reset},
                              "seven_day":{"utilization":20,"resets_at":reset}}, None)
    gov.refresh_usage()
    clock[0] += 101
    with pytest.raises(cc.BridgeError, match="STALE"):
        gov.gate({"mode":"read_only","workdir":str(tmp_path)})
    assert provider.call_count == 1
    clock[0] += 200
    provider.return_value[0]["five_hour"]["resets_at"] = datetime.fromtimestamp(clock[0]+3600, timezone.utc).isoformat()
    provider.return_value[0]["seven_day"]["resets_at"] = provider.return_value[0]["five_hour"]["resets_at"]
    assert gov.gate({"mode":"read_only","workdir":str(tmp_path)})["state"] == "VALID"


def test_revocation_overrides_valid_cached_usage(guarded):
    gov, _, provider, credential = guarded
    gov.refresh_usage()
    gov.revoke()
    value = gov.refresh_usage()
    assert value["state"] == "AUTH_REQUIRED" and not value["current_known"]
    assert provider.call_count == credential.call_count == 1


def test_legacy_defaults_migrate_without_losing_authorization(tmp_path):
    from cc_config import defaults, ConfigStore
    config = defaults()
    config["usage"].update(fresh_seconds=420, refresh_interval_minutes=6)
    config["usage"]["authorization"].update(authorized=True, authorized_at="2026-10-01T00:00:00Z")
    config["usage"]["five_hour"]["max_utilization_pct"] = 85
    path = tmp_path/"config.json"
    path.write_text(json.dumps(config),encoding="utf-8")
    migrated = ConfigStore(tmp_path).load()
    assert migrated["usage"]["fresh_seconds"] == 300
    assert migrated["usage"]["refresh_interval_minutes"] == 5
    assert migrated["usage"]["authorization"] == config["usage"]["authorization"]
    assert migrated["usage"]["five_hour"]["max_utilization_pct"] == 85
    assert json.loads(path.read_text(encoding="utf-8")) == migrated


def test_extended_cache_lifetime_rejected(guarded):
    gov, _, _, _ = guarded
    with pytest.raises(cc.BridgeError):
        gov.config.set_value("usage.fresh_seconds", 301)


@pytest.mark.parametrize("five,weekly", [(90,20),(10,95)])
def test_cached_reading_still_obeys_both_ceilings(guarded, tmp_path, five, weekly):
    gov, _, provider, _ = guarded
    provider.return_value[0]["five_hour"]["utilization"] = five
    provider.return_value[0]["seven_day"]["utilization"] = weekly
    gov.refresh_usage()
    with pytest.raises(g.UsageBlocked):
        gov.gate({"mode":"read_only","workdir":str(tmp_path)})
    assert provider.call_count == 1


def test_expired_cache_refreshes_and_uses_latest_usage(guarded, tmp_path):
    gov, clock, provider, _ = guarded
    gov.refresh_usage()
    provider.return_value[0]["five_hour"]["utilization"] = 90
    provider.return_value[0]["seven_day"]["utilization"] = 95
    clock[0] += 299
    assert gov.gate({"mode":"read_only","workdir":str(tmp_path)})["five_hour"]["utilization"] == 10
    assert provider.call_count == 1
    clock[0] += 1
    with pytest.raises(g.UsageBlocked):
        gov.gate({"mode":"read_only","workdir":str(tmp_path)})
    cached = gov.usage()
    assert provider.call_count == 2
    assert cached["five_hour"]["utilization"] == 90 and cached["weekly"]["utilization"] == 95
    assert cached["fetched_at"] == cc.utc_iso()
