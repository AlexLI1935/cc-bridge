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
    clock = [time.time()]
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
