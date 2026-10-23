"""Local consent, subscription usage, workspace trust and durable events.

The credential freshness check, two-window parser, last-valid fallback and
runner-owned low-frequency refresher are adapted from the original bridge.
Credentials are never part of bridge records or diagnostic strings.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
SCOPE = "claude_subscription_usage"


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("An absolute timestamp with an offset is required")
    return parsed.astimezone(timezone.utc)


def local_time(epoch: float, zone: str | None = None) -> str:
    # Convert at the target instant, not using today's fixed UTC offset.
    if zone:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(epoch, ZoneInfo(zone)).isoformat()
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat()


def timezone_info(override: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    name = None
    if os.name == "nt":
        try:
            result = subprocess.run(["tzutil", "/g"], capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0:
                name = result.stdout.decode(errors="replace").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        link = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in link:
            name = link.split("/zoneinfo/", 1)[1]
        elif Path("/etc/timezone").is_file():
            name = Path("/etc/timezone").read_text().strip()
    if name and os.name != "nt":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
        except (KeyError, ValueError):
            name = None
    os_name = name
    if override:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(override))
        name = override
    elif name:
        local = now.astimezone()
    else:
        name = "UTC"
        local = now
    from cc_time import clock_diagnostic
    diagnostic = clock_diagnostic(now, now.astimezone() if os_name else now, os_name or "UTC", None)
    return {"os_timezone": os_name, "timezone_source": "explicit" if override else "system" if os_name else "utc_fallback", "clock_diagnostic": diagnostic, "local_timezone": name, "local_utc_offset_now": local.strftime("%z"),
            "timezone_checked_at": now.isoformat(), "system_utc_time": now.isoformat(),
            "system_local_time": local.isoformat()}


def reset_fields(value: Any, zone: str | None = None) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Missing reset timestamp")
    dt = instant(value)
    return {"resets_at_raw": value, "resets_at_utc": dt.isoformat(), "resets_at_epoch": dt.timestamp(),
            "resets_at_local": local_time(dt.timestamp(), zone)}


def _validated_percentage(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"Invalid {label} utilization")
    return float(value)


def _credential_access_if_fresh(credentials: Any) -> str | None:
    auth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    access = auth.get("accessToken") if isinstance(auth, dict) else None
    expiry = auth.get("expiresAt") if isinstance(auth, dict) else None
    if not isinstance(access, str) or not access or isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    if not math.isfinite(expiry) or expiry - time.time() * 1000 < 5 * 60 * 1000:
        return None
    return access


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Never forward the bearer credential to a redirect destination.
        return None


def fetch_usage(access: str) -> tuple[Any, str | None]:
    request = urllib.request.Request(ENDPOINT, headers={"Authorization": "Bearer " + access,
        "anthropic-beta": "oauth-2025-04-20", "User-Agent": "cc-bridge/1.1.0", "Accept": "application/json"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Usage response too large")
        return json.loads(raw.decode("utf-8")), response.headers.get("Date")



from cc_bridge import BridgeError


class UsageBlocked(BridgeError):
    def __init__(self, usage: dict[str, Any]):
        super().__init__("USAGE_GATE: insufficient capacity under configured ceilings")
        self.usage = usage


def register_wakeup(bridge: Any, epoch: float) -> dict[str, Any]:
    """One durable Windows wakeup per runtime home; never encode quota guesses."""
    import sys
    import tempfile
    import xml.etree.ElementTree as ET
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    def node(parent: Any, name: str, text: str | None = None) -> Any:
        item = ET.SubElement(parent, "{"+ns+"}"+name)
        item.text = text
        return item
    task = ET.Element("{"+ns+"}Task", version="1.2")
    triggers = node(task,"Triggers")
    trigger = node(triggers,"TimeTrigger")
    node(trigger,"StartBoundary",bridge.governance.local_display(max(epoch,time.time()+5)))
    node(trigger,"Enabled","true")
    settings = node(task,"Settings")
    node(settings,"MultipleInstancesPolicy","IgnoreNew")
    node(settings,"DisallowStartIfOnBatteries","false")
    node(settings,"StopIfGoingOnBatteries","false")
    node(settings,"StartWhenAvailable","true")
    node(settings,"ExecutionTimeLimit","PT10M")
    action = node(node(task,"Actions"),"Exec")
    node(action,"Command",sys.executable)
    node(action,"Arguments",subprocess.list2cmdline([str(Path(__file__).with_name("cc_bridge.py")),
         "--home",str(bridge.home),"scheduler-tick"]))
    name = "CC_Bridge_Reset_"+hashlib.sha256(str(bridge.home).encode()).hexdigest()[:16]
    descriptor, temporary = tempfile.mkstemp(suffix=".xml",dir=bridge.runtime)
    try:
        with os.fdopen(descriptor,"wb") as handle:
            handle.write(ET.tostring(task,encoding="utf-16",xml_declaration=True))
        result = subprocess.run(["schtasks","/Create","/TN",name,"/XML",temporary,"/F"],
            capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        return {"delivery":"windows_task_scheduler" if result.returncode==0 else "caller_polling",
                "scheduled":result.returncode==0,"next_check_epoch":epoch,
                "last_error":None if result.returncode==0 else "OS scheduler registration refused; run scheduler-tick or poll pending"}
    except (OSError,subprocess.SubprocessError):
        return {"delivery":"caller_polling","scheduled":False,"next_check_epoch":epoch,
                "last_error":"OS scheduler unavailable; run scheduler-tick or poll pending"}
    finally:
        Path(temporary).unlink(missing_ok=True)


class Governance:
    def __init__(self, bridge: Any):
        self.bridge = bridge
        self.root = bridge.runtime
        from cc_config import ConfigStore
        self.config = ConfigStore(bridge.home)
        info = self.config.detect_timezone()
        self.reconcile_reminders({"timezone": info})

    def _load(self, name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        from cc_bridge import read_json
        path = self.root / name
        return read_json(path) if path.exists() else dict(default or {})

    def _save(self, name: str, value: dict[str, Any]) -> None:
        from cc_bridge import write_json_atomic
        write_json_atomic(self.root / name, value)

    def local_display(self, epoch: float) -> str:
        zone = self.config.load()["timezone"]
        return local_time(epoch, zone["name"] if zone["mode"] == "explicit" else "UTC" if zone["detected_name"] == "UTC" else None)

    def policy(self) -> dict[str, Any]:
        c = self.config.load()
        return {"refresh_seconds": c["usage"]["refresh_interval_minutes"] * 60,
                "fresh_seconds": c["usage"]["fresh_seconds"],
                "clock_skew_seconds": c["clock"]["max_skew_seconds"],
                "allow_cli_refresh_probe": c["usage"]["allow_cli_refresh_probe"],
                "trusted_roots": c["workspace"]["trusted_roots"]}

    def authorize(self, confirmed: bool) -> dict[str, Any]:
        from cc_bridge import BridgeError, FileLock, utc_iso
        if not confirmed:
            raise BridgeError("Explicit user consent is required for subscription usage access")
        with FileLock(self.root / "usage.lock"):
            self.config.update(lambda c: c["usage"]["authorization"].update(
                authorized=True, authorized_at=utc_iso()))
        return {"success": True, "usage_access_authorized": True, "scope": SCOPE}

    def revoke(self) -> dict[str, Any]:
        from cc_bridge import FileLock
        with FileLock(self.root / "usage.lock"):
            self.config.update(lambda c: c["usage"]["authorization"].update(authorized=False))
        return {"success": True, "usage_access_authorized": False}

    def authorized(self) -> bool:
        c = self.config.load()
        return c["usage"]["authorization"]["authorized"] is True

    def trust(self, path: str, revoke: bool = False, confirmed: bool = False) -> dict[str, Any]:
        from cc_bridge import BridgeError
        candidate = Path(path).expanduser().resolve()
        if not revoke and (not confirmed or not Path(path).is_absolute() or not candidate.is_dir()):
            raise BridgeError("Trust requires explicit user approval and an existing absolute directory")
        def change(c: dict[str, Any]) -> None:
            roots = set(c["workspace"]["trusted_roots"])
            roots.discard(str(candidate)) if revoke else roots.add(str(candidate))
            c["workspace"]["trusted_roots"] = sorted(roots)
        c = self.config.update(change)
        return {"success": True, "trusted_roots": c["workspace"]["trusted_roots"]}

    def check_workspace(self, task: dict[str, Any]) -> None:
        from cc_bridge import BridgeError
        if task["mode"] == "write":
            directory = Path(task["workdir"]).resolve()
            if not any(directory.is_relative_to(Path(root).resolve()) for root in self.policy().get("trusted_roots", [])):
                raise BridgeError("WORKSPACE_AUTH_REQUIRED: explicitly trust this workspace before write execution")

    def credential_path(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / ".credentials.json"

    def _fresh_usage_access_token(self) -> str | None:
        if not self.authorized() or not self.config.load()["usage"]["enabled"]:
            return None
        from cc_bridge import discover_claude_exe
        def read() -> str | None:
            try:
                return _credential_access_if_fresh(json.loads(self.credential_path().read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return None
        access = read()
        if access:
            return access
        # Let the real CLI own authentication. Status is non-generative, but
        # not guaranteed to refresh expired sessions on every CLI version.
        try:
            subprocess.run([str(discover_claude_exe()), "auth", "status"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
            access = read()
            if not access and self.policy().get("allow_cli_refresh_probe") is True:
                # Original minimal CLI refresh fallback, now separately opt-in:
                # it can consume model usage and is never used by offline tests.
                subprocess.run([str(discover_claude_exe()), "-p", "Reply OK.", "--tools", "",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--disable-slash-commands", "--settings", '{"disableAllHooks":true}'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
                access = read()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return None
        return access

    def usage(self) -> dict[str, Any]:
        from cc_bridge import BridgeError
        try:
            data = self._load("usage.json", {"state": "UNKNOWN", "stale": True, "current_known": False})
            if not self.config.load()["usage"]["enabled"]:
                data.update(state="USAGE_DISABLED", stale=True, current_known=False)
            elif not self.authorized():
                data.update(state="AUTH_REQUIRED", stale=True, current_known=False)
            elif data.get("state") == "VALID" and data.get("fetched_at") and (time.time() - instant(data["fetched_at"]).timestamp() >= self.policy()["fresh_seconds"]
                    or time.time() < instant(data["fetched_at"]).timestamp() - self.policy()["clock_skew_seconds"]):
                data.update(state="STALE", stale=True, current_known=False)
            elif data.get("state") == "VALID" and data.get("fetched_at") and any(
                    instant(data["fetched_at"]).timestamp() < data.get(key, {}).get("resets_at_epoch", float("inf")) <= time.time()
                    for key in ("five_hour", "weekly")):
                # A snapshot from before a reset cannot verify availability after it.
                data.update(state="STALE", stale=True, current_known=False)
            for key in ("five_hour", "weekly"):
                if isinstance(data.get(key), dict) and data[key].get("resets_at_epoch") is not None:
                    data[key]["resets_at_local"] = self.local_display(data[key]["resets_at_epoch"])
            return data
        except (BridgeError, ValueError, TypeError):
            return {"state": "UNKNOWN", "stale": True, "current_known": False, "last_error": "Usage cache unavailable"}

    def refresh_usage(self, reason: str = "explicit") -> dict[str, Any]:
        from cc_bridge import FileLock, utc_iso
        with FileLock(self.root / "usage.lock", timeout=120):
            previous = self.usage()
            query = self._load("usage-query.json")
            # Migrate an existing cache conservatively without making an extra request.
            if not query and previous.get("checked_at") and (previous.get("fetched_at") or previous.get("state") == "ENDPOINT_ERROR"):
                last = instant(previous["checked_at"]).timestamp()
                query = {"last_attempt_epoch": last, "next_allowed_epoch": last + self.policy()["refresh_seconds"]}
            last = query.get("last_attempt_epoch", 0)
            next_allowed = max(query.get("next_allowed_epoch", 0),
                               last + self.policy()["refresh_seconds"] if last else 0)
            if previous.get("state") == "VALID" and previous.get("fetched_at"):
                next_allowed = max(next_allowed, instant(previous["fetched_at"]).timestamp() + self.policy()["fresh_seconds"])
            data = {k: previous[k] for k in ("five_hour", "weekly", "fetched_at", "last_valid_at") if k in previous}
            data.update(stale=True, current_known=False, checked_at=utc_iso(), timezone=self.config.detect_timezone(), reason=reason)
            if not self.config.load()["usage"]["enabled"]:
                data.update(state="USAGE_DISABLED", stale=True, current_known=False)
            elif not self.authorized():
                data.update(state="AUTH_REQUIRED", auth_state="AUTH_REQUIRED", last_error="Usage access needs explicit user consent")
            elif time.time() < next_allowed:
                # All callers share this persisted cooldown under the same kernel lock.
                # Never reread credentials or send another request during the interval.
                data = {**previous, "timezone": data["timezone"], "reason": reason,
                        "refresh_deferred": True, "cache_reused": previous.get("state") == "VALID",
                        "next_refresh_epoch": next_allowed,
                        "next_refresh_at": datetime.fromtimestamp(next_allowed, timezone.utc).isoformat()}
            else:
                try:
                    access = self._fresh_usage_access_token()
                    if not access:
                        data.update(state="AUTH_STALE", auth_state="AUTH_STALE", last_error="Claude authentication is unavailable or stale")
                    else:
                        now = time.time()
                        query = {"last_attempt_epoch": now,
                                 "next_allowed_epoch": now + self.policy()["refresh_seconds"]}
                        # Reserve the interval before networking, including failed/crashed requests.
                        self._save("usage-query.json", query)
                        raw, server_date = fetch_usage(access)
                        # Whitelist only numeric utilization and parsed absolute resets.
                        windows = {}
                        for key, source in (("five_hour", "five_hour"), ("weekly", "seven_day")):
                            used = _validated_percentage(raw[source]["utilization"], key)
                            windows[key] = {"utilization": used, "remaining": round(100 - used, 2),
                                            **reset_fields(raw[source]["resets_at"], self.config.load()["timezone"]["name"] if self.config.load()["timezone"]["mode"] == "explicit" else None)}
                        external = None
                        skew = None
                        if server_date:
                            server_dt = parsedate_to_datetime(server_date)
                            if server_dt.tzinfo is None:
                                raise ValueError("Invalid server date")
                            external = server_dt
                            skew = round(time.time() - server_dt.timestamp(), 2)
                        from cc_time import clock_diagnostic
                        now = datetime.now(timezone.utc)
                        diagnosis = clock_diagnostic(now, now.astimezone(), data["timezone"].get("os_timezone") or data["timezone"]["local_timezone"], external, self.policy()["clock_skew_seconds"])
                        data["clock_diagnostic"] = diagnosis
                        if diagnosis["status"] == "TIMEZONE_OR_CLOCK_MISCONFIGURED":
                            data.update(state="TIMEZONE_OR_CLOCK_MISCONFIGURED", auth_state="VALID", last_error="OS local time and timezone are inconsistent; correct system configuration")
                        elif skew is not None and abs(skew) > self.policy()["clock_skew_seconds"]:
                            data.update(state="CLOCK_SKEW", auth_state="VALID", clock_skew_seconds=skew,
                                        last_error="System clock differs significantly from the HTTPS server date")
                        else:
                            data.update(windows, state="VALID", auth_state="VALID", stale=False, current_known=True,
                                        fetched_at=utc_iso(), last_valid_at=utc_iso(), last_error=None, clock_skew_seconds=skew)
                except urllib.error.HTTPError as exc:
                    data.update(state="AUTH_STALE" if exc.code in (401, 403) else "ENDPOINT_ERROR",
                                auth_state="AUTH_STALE" if exc.code in (401, 403) else "UNKNOWN",
                                http_status=exc.code, last_error="Usage endpoint rejected request")
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after:
                        try:
                            try:
                                retry_epoch = time.time() + float(retry_after)
                            except ValueError:
                                retry_epoch = parsedate_to_datetime(retry_after).timestamp()
                            if math.isfinite(retry_epoch):
                                datetime.fromtimestamp(retry_epoch, timezone.utc)
                                query["next_allowed_epoch"] = max(query.get("next_allowed_epoch", 0), retry_epoch)
                                self._save("usage-query.json", query)
                        except (ValueError, TypeError, OverflowError):
                            pass
                except (OSError, urllib.error.URLError):
                    data.update(state="ENDPOINT_ERROR", auth_state="UNKNOWN", last_error="Usage endpoint unavailable")
                except Exception:
                    # Do not serialize exception text, response bodies, or request objects.
                    data.update(state="UNKNOWN", auth_state="UNKNOWN", last_error="Usage response or authentication is incompatible")
                if query.get("next_allowed_epoch"):
                    data.update(refresh_deferred=False, cache_reused=False,
                                next_refresh_epoch=query["next_allowed_epoch"],
                                next_refresh_at=datetime.fromtimestamp(query["next_allowed_epoch"], timezone.utc).isoformat())
            self._save("usage.json", data)
        self.reconcile_reminders(data)
        # Use the persisted, revalidated cache as the single result after a refresh.
        return self.usage()

    def gate(self, task: dict[str, Any]) -> dict[str, Any]:
        from cc_bridge import BridgeError
        c = self.config.load()
        if not c["usage"]["enabled"]:
            raise BridgeError("USAGE_DISABLED: automatic task execution is disabled with usage monitoring")
        if not c["usage"]["authorization"]["authorized"]:
            raise BridgeError("USAGE_GATE: AUTH_REQUIRED; explicit user consent is required")
        data = self.refresh_usage("before_task")
        if data.get("state") != "VALID" or not data.get("current_known") or data.get("stale"):
            error = BridgeError(f"USAGE_GATE: {data.get('state', 'UNKNOWN')}; current usage is required")
            error.usage = data
            raise error
        self.check_workspace(task)
        if self.blocked_windows(data):
            raise UsageBlocked(data)
        return data

    def event(self, record: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
        from cc_bridge import utc_iso
        kind = kind or {"COMPLETED": "TASK_COMPLETED", "FAILED": "TASK_FAILED", "NEEDS_INPUT": "TASK_NEEDS_INPUT",
                        "WAITING_USAGE_RESET": "TASK_WAITING_USAGE_RESET", "CANCELLED": "TASK_CANCELLED"}.get(record["status"], "TASK_FAILED")
        identity = f"{record['task_id']}:{record.get('attempt', 0)}:{kind}:{record.get('wait_cycle', '')}"
        event_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        name = "pending/" + event_id + ".json"
        with self.bridge._state_lock():
            old = self._load(name)
            if old:
                return old
            event = {"event_id": event_id, "task_id": record["task_id"], "attempt": record.get("attempt", 0),
                     "type": kind, "created_at": utc_iso(), "status": "pending", "result_status": record["status"],
                     "report_path": record.get("report_path")}
            self._save(name, event)
        command = os.environ.get("CC_BRIDGE_WAKE_COMMAND")
        if command:
            try:
                args = json.loads(command)
                if not isinstance(args, list) or not args or not all(isinstance(x, str) for x in args):
                    raise ValueError("Expected a JSON argv array")
                subprocess.run(args + [event_id, record["task_id"]], shell=False, timeout=10,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass  # durable outbox, not the optional hook, is the delivery source of truth
        return event

    def pending(self, include_done: bool = False) -> dict[str, Any]:
        self.verify_reminders()
        # Repair the outbox if a crash occurred after saving a terminal result.
        for path in self.bridge.results.glob("*/result.json"):
            from cc_bridge import read_json
            record = read_json(path)
            if record.get("status") not in ("QUEUED", "RUNNING", "CANCELLING"):
                self.event(record)
        with self.bridge._state_lock():
            rows = [self._load("pending/" + p.name) for p in sorted((self.root / "pending").glob("*.json"))]
        return {"success": True, "pending": [r for r in rows if include_done or r["status"] != "done"]}

    def change_event(self, event_id: str, done: bool = False) -> dict[str, Any]:
        from cc_bridge import BridgeError, utc_iso
        if not isinstance(event_id, str) or len(event_id) != 32 or any(c not in "0123456789abcdef" for c in event_id):
            raise BridgeError("Invalid event ID")
        with self.bridge._state_lock():
            name = "pending/" + event_id + ".json"
            event = self._load(name)
            if not event:
                raise BridgeError("No such event")
            first = event["status"] == "pending"
            if done:
                event.update(status="done", done_at=utc_iso())
            elif first:
                event.update(status="claimed", claimed_at=utc_iso())
            self._save(name, event)
        return {"success": True, **event, "first_claim": first if not done else False}

    def schedule_reset_reminder(self, task_id: str) -> dict[str, Any]:
        from cc_bridge import BridgeError
        self.bridge._task(task_id)
        data = self.usage()
        if data.get("state") != "VALID" or data.get("stale"):
            raise BridgeError("Fresh valid usage and a sane clock are required")
        record = self.bridge.get_task(task_id, full=True)
        if record["status"] != "WAITING_USAGE_RESET":
            if not self.blocked_windows(data):
                raise BridgeError("Neither window is blocked by the configured ceilings")
            record = self.wait_task(task_id, data, before_launch=record["attempt"] == 0)
        return {"success": True, "task_id": task_id, "epoch": record["reset_epoch"],
                "resets_at_local": record["reset_local"], "local_timezone": record["local_timezone"],
                **self.arm_scheduler()}

    def reconcile_reminders(self, usage: dict[str, Any]) -> None:
        from cc_bridge import read_json, write_json_atomic
        changed = False
        waiting = False
        with self.bridge._state_lock():
            for path in self.bridge.results.glob("*/result.json"):
                record = read_json(path)
                if record.get("status") == "WAITING_USAGE_RESET" and record.get("reset_epoch"):
                    waiting = True
                    new_local = self.local_display(record["reset_epoch"])
                    new_zone = usage["timezone"]["local_timezone"]
                    changed |= record.get("reset_local") != new_local or record.get("local_timezone") != new_zone
                    record.update(reset_local=new_local, local_timezone=new_zone)
                    write_json_atomic(path, record)
        if changed or waiting:
            self.arm_scheduler()

    def verify_reminders(self, task_id: str | None = None) -> dict[str, Any]:
        from cc_bridge import BridgeError, FileLock, read_json, write_json_atomic
        # The persistent cycle + state transaction prevents duplicate dispatch;
        # the kernel runner lock remains the final single-process boundary.
        checked, resumed = [], []
        try:
            guard = FileLock(self.root / "scheduler.lock", timeout=0)
            guard.__enter__()
        except BridgeError:
            return {"success": True, "verified": [], "resumed": [], "busy": True}
        try:
            config = self.config.load()
            for path in self.bridge.results.glob("*/result.json"):
                record = read_json(path)
                if (record.get("status") != "WAITING_USAGE_RESET"
                        or record.get("next_check_epoch", float("inf")) > time.time()
                        or task_id and record["task_id"] != task_id):
                    continue
                config = self.config.load()
                if (not config["usage"]["enabled"] or not config["reset_scheduler"]["enabled"]
                        or not all(config["usage"][w]["auto_wait_for_reset"] for w in record["blocked_windows"])):
                    continue
                data = self.refresh_usage("reset_verification")
                config = self.config.load()
                if data.get("state") == "VALID" and not data.get("stale") and not self.blocked_windows(data):
                    checked.append(record["task_id"])
                    self.event(record, "USAGE_AVAILABLE")
                    windows = record["blocked_windows"]
                    auto = all(config["usage"][w]["auto_resume_after_reset"]
                        and config["usage"][w]["auto_wait_for_reset"] for w in windows)
                    if auto and config["usage"]["enabled"] and config["reset_scheduler"]["enabled"]:
                        try:
                            self.check_workspace(self.bridge._task(record["task_id"]))
                            with self.bridge._state_lock():
                                latest = self.bridge._record(record["task_id"])
                                if latest["status"] != "WAITING_USAGE_RESET" or latest.get("wait_cycle") != record["wait_cycle"]:
                                    continue
                                if not latest.get("session_id") and not latest.get("waiting_before_launch"):
                                    raise BridgeError("No saved session UUID for automatic resume")
                                result = self.bridge._dispatch(self.bridge._task(record["task_id"]), latest,
                                    latest.get("continuation") or ("Continue the existing task." if latest.get("session_id") else None))
                            self.event({**record, "status": "QUEUED"}, "TASK_RESUMED")
                            resumed.append(result["task_id"])
                            continue
                        except BridgeError:
                            pass  # retain WAITING; another task or a revoked root may block execution
                elif data.get("state") == "VALID" and self.blocked_windows(data):
                    self.wait_task(record["task_id"], data, record.get("continuation"), record.get("waiting_before_launch", False))
                    continue
                with self.bridge._state_lock():
                    latest = self.bridge._record(record["task_id"])
                    if latest["status"] == "WAITING_USAGE_RESET":
                        latest.update(last_usage=data, next_check_epoch=max(
                            time.time() + config["reset_scheduler"]["retry_seconds"], data.get("next_refresh_epoch", 0)))
                        write_json_atomic(path, latest)
        finally:
            guard.__exit__(None, None, None)
        return {"success": True, "verified": checked, "resumed": resumed, "scheduler": self.arm_scheduler()}


    def blocked_windows(self, data: dict[str, Any]) -> list[str]:
        config = self.config.load()
        return [key for key in ("five_hour", "weekly")
                if data[key]["utilization"] >= config["usage"][key]["max_utilization_pct"]]

    def wait_task(self, task_id: str, data: dict[str, Any], continuation: str | None = None,
                  before_launch: bool = False) -> dict[str, Any]:
        from cc_bridge import BridgeError, utc_iso, write_json_atomic
        config = self.config.load()
        if data.get("state") != "VALID" or data.get("stale"):
            raise BridgeError("A known reset instant and a valid usage snapshot are required")
        blocked = self.blocked_windows(data)
        # A provider limit can precede reported utilization. Still use only the
        # returned absolute window resets, never a guessed five-hour duration.
        if not blocked:
            blocked = ["five_hour", "weekly"]
        epoch = max(data[key]["resets_at_epoch"] for key in blocked)
        due = max(epoch + config["usage"]["reset_grace_seconds"],
                  time.time() + config["reset_scheduler"]["retry_seconds"] if epoch <= time.time() else epoch)
        auto_wait = all(config["usage"][k]["auto_wait_for_reset"] for k in blocked)
        auto_resume = auto_wait and all(config["usage"][k]["auto_resume_after_reset"] for k in blocked)
        with self.bridge._state_lock():
            record = self.bridge._record(task_id)
            if record.get("dispatched") and record["status"] in ("RUNNING", "QUEUED", "CANCELLING"):
                raise BridgeError("Task is already dispatched")
            if record["status"] == "CANCELLED":
                return {"success": False, **record}
            cycle = hashlib.sha256(f"{task_id}:{record['attempt']}:{epoch}:{','.join(blocked)}".encode()).hexdigest()[:24]
            record.update(status="WAITING_USAGE_RESET", stage="usage", dispatched=False, runner_pid=None,
                blocked_by=blocked[0] if len(blocked) == 1 else "multiple", blocked_windows=blocked,
                blocked_at=utc_iso(), last_usage=data, reset_epoch=epoch,
                reset_utc=datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                reset_local=self.local_display(epoch), local_timezone=self.config.detect_timezone()["local_timezone"],
                next_check_epoch=due, auto_wait=auto_wait, auto_resume=auto_resume,
                waiting_before_launch=before_launch or record.get("waiting_before_launch", False),
                wait_cycle=cycle, continuation=continuation or record.get("continuation"),
                summary="Waiting for configured usage ceilings; no Claude process is running")
            write_json_atomic(self.bridge._result_path(task_id), record)
        self.event(record, "TASK_WAITING_USAGE_RESET")
        delivery = self.arm_scheduler()
        return {"success": True, **record, "scheduler": delivery}

    def arm_scheduler(self) -> dict[str, Any]:
        from cc_bridge import FileLock
        with FileLock(self.root / "scheduler-register.lock"):
            return self._arm_scheduler()

    def _arm_scheduler(self) -> dict[str, Any]:
        from cc_bridge import read_json
        if self._load("usage.json").get("state") in ("CLOCK_SKEW", "TIMEZONE_OR_CLOCK_MISCONFIGURED"):
            return {"delivery": "blocked", "scheduled": False, "last_error": self._load("usage.json").get("state")}
        config = self.config.load()
        rows = [read_json(p) for p in self.bridge.results.glob("*/result.json")]
        due = [r["next_check_epoch"] for r in rows if r.get("status") == "WAITING_USAGE_RESET"
               and r.get("next_check_epoch") and all(config["usage"][w]["auto_wait_for_reset"]
                   for w in r.get("blocked_windows", []))]
        if not due or not config["reset_scheduler"]["enabled"] or not config["usage"]["enabled"]:
            return {"delivery": "disabled", "scheduled": False}
        epoch = min(due)
        if os.name != "nt" or config["reset_scheduler"]["backend"] == "caller_polling":
            return {"delivery": "caller_polling", "scheduled": False, "next_check_epoch": epoch}
        result = register_wakeup(self.bridge, epoch)
        self._save("scheduler.json", result)
        return result

class UsageRefresher:
    """A runner-owned refresher; idle waiting is handled by durable reset checks."""
    def __init__(self, governance: Governance):
        self.governance = governance
        self.interval = governance.policy()["refresh_seconds"]
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.latest: dict[str, Any] | None = None

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.latest = self.governance.refresh_usage("periodic")

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True, name="cc-usage-refresh")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=110)


def ensure_report(attempt_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    from cc_bridge import BridgeError, write_json_atomic
    path = attempt_dir / "report.md"
    # Only the bridge writes this canonical report, outside the child workdir.
    if path.exists() or list(attempt_dir.glob("report*.md")):
        raise BridgeError("Canonical report already exists for this attempt")
    content = str(record.get("result") or record["summary"]).strip()
    if not content:
        raise BridgeError("Final report is empty")
    path.write_text(content + "\n", encoding="utf-8")
    receipt = {"task_id": record["task_id"], "attempt": record["attempt"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    write_json_atomic(attempt_dir / "report-receipt.json", receipt)
    return receipt


def canonical_report_committed(attempt_dir: Path, record: dict[str, Any]) -> bool:
    from cc_bridge import read_json
    try:
        receipt = read_json(attempt_dir / "report-receipt.json")
        path = attempt_dir / "report.md"
        return (len(list(attempt_dir.glob("report*.md"))) == 1 and path.stat().st_size > 0
                and receipt["task_id"] == record["task_id"] and receipt["attempt"] == record["attempt"]
                and receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest())
    except Exception:
        return False
