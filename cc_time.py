"""Absolute scheduling and explicit local-time conversion; never geolocation."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def relative_epoch(now_epoch: float, seconds: float) -> float:
    return now_epoch + seconds


def next_local_occurrence(after_epoch: float, zone: str, hour: int, minute: int = 0,
                          weekday: int | None = None) -> float:
    """Resolve a daily/weekly rule afresh. Skip gaps; use the first fold once.

    This is a conversion helper, not a new recurring execution service. Persist
    the zone, local hour/minute and recurrence; do not persist a fixed UTC offset.
    """
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or weekday is not None and not 0 <= weekday <= 6:
        raise ValueError("Invalid local recurrence")
    tz = ZoneInfo(zone)
    start = datetime.fromtimestamp(after_epoch, tz).date()
    for days in range(370):
        date = start + timedelta(days=days)
        if weekday is not None and date.weekday() != weekday:
            continue
        local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz, fold=0)
        epoch = local.timestamp()
        if datetime.fromtimestamp(epoch, tz).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if epoch > after_epoch:
            return epoch
    raise ValueError("No next occurrence")


def clock_diagnostic(system_utc: datetime, local: datetime, os_zone: str,
                     external_utc: datetime | None, max_skew: float = 300) -> dict:
    skew = (system_utc - external_utc).total_seconds() if external_utc else None
    status = "OK" if external_utc else "UNVERIFIED"
    if abs(local.timestamp() - system_utc.timestamp()) > 1:
        status = "TIMEZONE_OR_CLOCK_MISCONFIGURED"
    else:
        try:
            expected = system_utc.astimezone(ZoneInfo(os_zone))
            if local.replace(tzinfo=None) != expected.replace(tzinfo=None):
                status = "TIMEZONE_OR_CLOCK_MISCONFIGURED"
        except (KeyError, ValueError):
            pass  # Native Windows identities are resolved by the OS, not guessed.
    if status != "TIMEZONE_OR_CLOCK_MISCONFIGURED" and skew is not None and abs(skew) > max_skew:
        status = "CLOCK_SKEW"
    return {"system_utc_time":system_utc.isoformat(), "system_timezone":os_zone,
            "system_local_time":local.isoformat(), "external_utc":external_utc.isoformat() if external_utc else None,
            "clock_skew_seconds":skew, "status":status}
