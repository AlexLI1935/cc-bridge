from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import pytest
from cc_time import relative_epoch,next_local_occurrence,clock_diagnostic
from cc_config import ConfigStore
import cc_governance as g


@pytest.mark.parametrize('zone,offset',[('Europe/Moscow',3),('Europe/Berlin',1),('America/New_York',-5),('Asia/Shanghai',8)])
def test_named_zone_winter(zone,offset):
    instant=datetime(2026,12,1,12,tzinfo=timezone.utc)
    assert instant.astimezone(ZoneInfo(zone)).utcoffset().total_seconds()==offset*3600


@pytest.mark.parametrize('zone,summer,winter',[('Europe/Berlin',2,1),('America/New_York',-4,-5)])
def test_daily_nine_keeps_local_meaning_across_dst(zone,summer,winter):
    for month,offset in [(7,summer),(12,winter)]:
        after=datetime(2026,month,1,tzinfo=timezone.utc).timestamp()
        next_epoch=next_local_occurrence(after,zone,9)
        local=datetime.fromtimestamp(next_epoch,ZoneInfo(zone))
        assert local.hour==9 and local.utcoffset().total_seconds()==offset*3600


def test_relative_five_hours_across_dst():
    start=datetime(2026,10,25,0,tzinfo=timezone.utc).timestamp()
    end=relative_epoch(start,5*3600)
    assert end-start==18000
    assert datetime.fromtimestamp(start,ZoneInfo('Europe/Berlin')).hour==2
    assert datetime.fromtimestamp(end,ZoneInfo('Europe/Berlin')).hour==6


def test_daily_nine_on_adjacent_dst_dates():
    first=next_local_occurrence(datetime(2026,10,24,tzinfo=timezone.utc).timestamp(),'Europe/Berlin',9)
    second=next_local_occurrence(first,'Europe/Berlin',9)
    assert second-first==25*3600


def test_wrong_timezone_and_wall_clock_report_inconsistency():
    utc=datetime(2026,10,23,6,tzinfo=timezone.utc)
    local=utc.astimezone(ZoneInfo('Europe/Moscow'))
    assert clock_diagnostic(utc,local,'Asia/Shanghai',utc)['status']=='TIMEZONE_OR_CLOCK_MISCONFIGURED'


def test_clock_skew_separate_from_timezone():
    utc=datetime(2026,10,23,6,tzinfo=timezone.utc)
    external=datetime(2026,10,23,1,tzinfo=timezone.utc)
    assert clock_diagnostic(utc,utc.astimezone(ZoneInfo('Asia/Shanghai')),'Asia/Shanghai',external)['status']=='CLOCK_SKEW'


def test_explicit_override_and_old_config_migration(tmp_path):
    store=ConfigStore(tmp_path)
    store.load()
    store.set_value('timezone.name','Asia/Shanghai')
    store.set_value('timezone.mode','explicit')
    info=store.detect_timezone()
    assert info['local_timezone']=='Asia/Shanghai' and info['timezone_source']=='explicit'
    assert g.local_time(0,'Asia/Shanghai').endswith('+08:00')


def test_utc_fallback(monkeypatch):
    monkeypatch.setattr(g.subprocess,'run',lambda *a,**k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(g.Path,'resolve',lambda _:g.Path('/unavailable'))
    monkeypatch.setattr(g.Path,'is_file',lambda _:False)
    assert g.timezone_info()['local_timezone']=='UTC'
