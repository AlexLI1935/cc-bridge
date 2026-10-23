"""One external/local baseline per process lifecycle; display only, never geography."""
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

_cache = {}
_lock = threading.Lock()


def fetch_external_utc():
    # No account credential and no usage request. Refuse redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request('https://api.anthropic.com/', method='HEAD')
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        external = parsedate_to_datetime(response.headers['Date'])
    if external.tzinfo is None:
        raise ValueError('External UTC needs an absolute reference')
    return external.astimezone(timezone.utc)


def observe(governance):
    key = str(governance.bridge.home)
    with _lock:
        config = governance.config.load()
        zone = config['timezone']
        fingerprint = (zone['detected_name'], zone['last_offset'])
        previous = _cache.get(key)
        skewed = previous and abs((time.time()-previous['system_epoch'])-(time.monotonic()-previous['monotonic'])) > config['clock']['max_skew_seconds']
        if previous and previous['fingerprint'] == fingerprint and not zone['refresh_observed_offset'] and not skewed:
            return previous['value']
        try:
            external = fetch_external_utc()
            wall = datetime.now().replace(tzinfo=timezone.utc)
            system = datetime.now(timezone.utc)
            value = {'status':'OBSERVED', 'observed_utc_offset_minutes':round((wall-external).total_seconds()/60),
                     'external_utc':external.isoformat(), 'local_wall_time':wall.replace(tzinfo=None).isoformat(),
                     'system_utc':system.isoformat(), 'clock_skew_seconds':(system-external).total_seconds()}
        except (OSError, ValueError, TypeError, KeyError):
            value = {'status':'UNVERIFIED', 'observed_utc_offset_minutes':None}
        _cache[key] = {'fingerprint':fingerprint,'system_epoch':time.time(),'monotonic':time.monotonic(),'value':value}
        governance._save('time-baseline.json',value)
        if zone['refresh_observed_offset']:
            governance.config.update(lambda c:c['timezone'].update(refresh_observed_offset=False))
        return value


def display(epoch, baseline):
    minutes = baseline.get('observed_utc_offset_minutes')
    if baseline.get('status') != 'OBSERVED' or minutes is None or abs(minutes) >= 1440:
        return None
    return datetime.fromtimestamp(epoch, timezone(timedelta(minutes=minutes))).isoformat()
