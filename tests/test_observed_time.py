from datetime import datetime,timezone,timedelta
from unittest.mock import Mock
import cc_observed as observed
from cc_bridge import Bridge


def test_external_utc_minus_wall_observes_offset_once(tmp_path,monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls(2026,10,23,7,tzinfo=timezone.utc) if tz else cls(2026,10,23,10)
    probe=Mock(return_value=datetime(2026,10,23,7,tzinfo=timezone.utc))
    monkeypatch.setattr(observed,'datetime',Clock)
    monkeypatch.setattr(observed,'fetch_external_utc',probe)
    gov=Bridge(tmp_path/'home').governance
    assert observed.observe(gov)['observed_utc_offset_minutes']==180
    assert observed.observe(Bridge(gov.bridge.home).governance)['observed_utc_offset_minutes']==180
    assert probe.call_count==1
    epoch=datetime(2026,10,23,15,tzinfo=timezone.utc).timestamp()
    assert gov.local_display(epoch).startswith('2026-10-23T18:00:00')
    assert epoch==datetime(2026,10,23,15,tzinfo=timezone.utc).timestamp()
    gov.config.set_value('timezone.refresh_observed_offset',True)
    observed.observe(gov)
    assert probe.call_count==2 and not gov.config.load()['timezone']['refresh_observed_offset']
    observed._cache.pop(str(gov.bridge.home))  # new process lifecycle
    observed.observe(gov)
    assert probe.call_count==3


def test_timezone_change_reprobes(tmp_path,monkeypatch):
    probe=Mock(return_value=datetime.now(timezone.utc))
    monkeypatch.setattr(observed,'fetch_external_utc',probe)
    gov=Bridge(tmp_path/'home').governance
    observed.observe(gov)
    gov.config.update(lambda c:c['timezone'].update(detected_name='changed-label'))
    observed.observe(gov)
    assert probe.call_count==2


def test_observation_failure_is_unverified_not_task_permission(tmp_path,monkeypatch):
    monkeypatch.setattr(observed,'fetch_external_utc',Mock(side_effect=OSError()))
    gov=Bridge(tmp_path/'home').governance
    assert observed.observe(gov)=={'status':'UNVERIFIED','observed_utc_offset_minutes':None}
    assert observed.display(0,gov._load('time-baseline.json')) is None
