"""Non-secret runtime configuration. No credential values belong here."""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any


def defaults() -> dict[str, Any]:
    return {"schema_version": 1,
        "usage": {"enabled": True,
            "authorization": {"authorized": False, "authorized_at": None, "scope": "claude_subscription_usage"},
            "five_hour": {"max_utilization_pct": 90, "auto_wait_for_reset": True, "auto_resume_after_reset": True},
            "weekly": {"max_utilization_pct": 95, "auto_wait_for_reset": True, "auto_resume_after_reset": True},
            "refresh_interval_minutes": 5, "fresh_seconds": 300, "reset_grace_seconds": 60,
            "allow_cli_refresh_probe": False},
        "timezone": {"mode": "system", "name": None, "detected_name": None, "detected_at": None, "last_offset": None},
        "clock": {"max_skew_seconds": 300, "last_check": None},
        "reset_scheduler": {"enabled": True, "backend": "system", "retry_seconds": 300},
        "workspace": {"trusted_roots": []}}


def validate(config: Any) -> dict[str, Any]:
    from cc_bridge import BridgeError
    def fail() -> None:
        raise BridgeError("Invalid config.json: use config.example.json and valid non-secret settings")
    def shape(value: Any, model: Any) -> None:
        if isinstance(model, dict):
            if not isinstance(value, dict) or set(value) != set(model):
                fail()
            for k in model:
                shape(value[k], model[k])
    shape(config, defaults())
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        fail()
    u = config["usage"]
    def number(value: Any, low: float, high: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            fail()
    for window in ("five_hour", "weekly"):
        number(u[window]["max_utilization_pct"], 0, 100)
        for key in ("auto_wait_for_reset", "auto_resume_after_reset"):
            if type(u[window][key]) is not bool:
                fail()
    for value in (u["enabled"], u["allow_cli_refresh_probe"], u["authorization"]["authorized"], config["reset_scheduler"]["enabled"]):
        if type(value) is not bool:
            fail()
    number(u["refresh_interval_minutes"], 5, 1440)
    number(u["fresh_seconds"], 1, 300)
    number(u["reset_grace_seconds"], 0, 3600)
    number(config["clock"]["max_skew_seconds"], 30, 3600)
    number(config["reset_scheduler"]["retry_seconds"], 30, 86400)
    auth = u["authorization"]
    if auth["scope"] != "claude_subscription_usage" or config["timezone"]["mode"] not in ("system", "explicit"):
        fail()
    zone = config["timezone"]["name"]
    if zone is not None:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(zone)
        except (KeyError, ValueError, TypeError):
            fail()
    if config["timezone"]["mode"] == "explicit" and not zone:
        fail()
    if config["reset_scheduler"]["backend"] not in ("system", "caller_polling"):
        fail()
    for value in (auth["authorized_at"], config["timezone"]["detected_at"]):
        if value is not None:
            try:
                from cc_governance import instant
                instant(value)
            except (ValueError, TypeError, AttributeError):
                fail()
    if auth["authorized"] and not auth["authorized_at"]:
        fail()
    roots = config["workspace"]["trusted_roots"]
    if not isinstance(roots, list) or not all(isinstance(r, str) and Path(r).is_absolute() for r in roots):
        fail()
    for value in (config["timezone"]["detected_name"], config["timezone"]["last_offset"]):
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            fail()
    clock = config["clock"]["last_check"]
    if clock is not None and (not isinstance(clock, dict) or set(clock) != {"system_utc_time", "system_local_time", "local_utc_offset_now"}):
        fail()
    return config


def _upgrade_persisted(config: Any) -> Any:
    """Tighten legacy cache lifetime without losing consent or workspace settings."""
    candidate = copy.deepcopy(config)
    if isinstance(candidate, dict) and candidate.get("schema_version") == 1:
        if isinstance(candidate.get("timezone"), dict):
            candidate["timezone"].setdefault("name", None)
        usage = candidate.get("usage")
        if isinstance(usage, dict):
            lifetime = usage.get("fresh_seconds")
            if (not isinstance(lifetime, bool) and isinstance(lifetime, (int, float))
                    and math.isfinite(lifetime) and 300 < lifetime <= 86400):
                # Migrate the paired v1.1.0 defaults to the new five-minute policy.
                if lifetime == 420 and usage.get("refresh_interval_minutes") == 6:
                    usage["refresh_interval_minutes"] = 5
                usage["fresh_seconds"] = 300
    return validate(candidate)


class ConfigStore:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "config.json"

    def load(self) -> dict[str, Any]:
        from cc_bridge import FileLock, read_json, write_json_atomic
        with FileLock(self.home / "runtime" / "config.lock"):
            if not self.path.exists():
                write_json_atomic(self.path, defaults())
            stored = read_json(self.path)
            config = _upgrade_persisted(stored)
            if config != stored:
                write_json_atomic(self.path, config)
            return config

    def update(self, change: Any) -> dict[str, Any]:
        from cc_bridge import FileLock, read_json, write_json_atomic
        with FileLock(self.home / "runtime" / "config.lock"):
            config = _upgrade_persisted(read_json(self.path)) if self.path.exists() else defaults()
            change(config)
            validate(config)
            write_json_atomic(self.path, config)
            return config

    def detect_timezone(self) -> dict[str, Any]:
        from cc_governance import timezone_info
        zone = self.load()["timezone"]
        info = timezone_info(zone["name"]) if zone["mode"] == "explicit" else timezone_info()
        def change(c: dict[str, Any]) -> None:
            c["timezone"].update(detected_name=info["local_timezone"], detected_at=info["timezone_checked_at"],
                                 last_offset=info["local_utc_offset_now"])
            c["clock"]["last_check"] = {k: info[k] for k in ("system_utc_time", "system_local_time", "local_utc_offset_now")}
        self.update(change)
        return info

    def set_value(self, dotted: str, value: Any) -> dict[str, Any]:
        from cc_bridge import BridgeError
        # Consent and workspace trust use dedicated explicit-approval commands.
        if not (dotted.startswith("usage.five_hour.") or dotted.startswith("usage.weekly.") or dotted in (
                "usage.enabled", "usage.refresh_interval_minutes", "usage.fresh_seconds", "usage.reset_grace_seconds",
                "usage.allow_cli_refresh_probe", "timezone.mode", "timezone.name", "clock.max_skew_seconds",
                "reset_scheduler.enabled", "reset_scheduler.backend", "reset_scheduler.retry_seconds")):
            raise BridgeError("Use usage-authorize/revoke or trust-workspace/revoke-workspace for authorization changes")
        def change(c: dict[str, Any]) -> None:
            keys = dotted.split(".")
            target = c
            try:
                for key in keys[:-1]:
                    target = target[key]
                if keys[-1] not in target:
                    raise KeyError(keys[-1])
                target[keys[-1]] = copy.deepcopy(value)
            except (KeyError, TypeError):
                raise BridgeError("Unknown config key") from None
        return self.update(change)
