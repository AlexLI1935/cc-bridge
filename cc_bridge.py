#!/usr/bin/env python3
"""CC Bridge: a local Claude Code task runner, shared by CLI and MCP.

Task contracts and per-attempt logs are ordinary files. OS-backed locks protect
state updates and enforce one active Claude process per runtime directory.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    sys.modules.setdefault("cc_bridge", sys.modules[__name__])

__version__ = "1.1.1"
SCHEMA_VERSION = 1
MODES = ("read_only", "write")
EFFORTS = ("low", "medium", "high")
COMPLEXITY_ROUTING = {
    "normal": ("sonnet", "medium"),
    "complex": ("opus", "medium"),
    "hard": ("opus", "high"),
}
ACTIVE_STATUSES = ("QUEUED", "RUNNING", "CANCELLING")
RESUMABLE_STATUSES = ("COMPLETED", "FAILED", "NEEDS_INPUT", "WAITING_USAGE_RESET", "CANCELLED")
TASK_ID_RE = re.compile(r"CC-\d{8}-[a-f0-9]{12}\Z")
SESSION_ID_RE = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
DEFAULT_MAX_RUNTIME_MINUTES = 120


class BridgeError(RuntimeError):
    """An actionable validation or lifecycle error."""


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_home() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "cc-bridge"
    return Path.home() / ".cc-bridge"


def runtime_home() -> Path:
    return Path(os.environ.get("CC_BRIDGE_HOME") or default_home()).expanduser().resolve()


def discover_claude_exe() -> Path:
    """Prefer the PATH installation, then an explicit executable path."""
    found = shutil.which("claude")
    if found:
        return Path(found).resolve()
    override = os.environ.get("CC_BRIDGE_CLAUDE_PATH")
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise BridgeError("CC_BRIDGE_CLAUDE_PATH must name an existing executable")
    raise BridgeError("Claude Code not found: install claude on PATH or set CC_BRIDGE_CLAUDE_PATH")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        raise BridgeError(f"Cannot read {path.name}: {exc}") from exc


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class FileLock:
    """Kernel locks release on process exit; persistent lock files are harmless."""

    def __init__(self, path: Path, timeout: float = 10):
        self.path, self.timeout = path, timeout
        self.handle: Any = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        # msvcrt locks one byte, even when that byte lies past end of file.
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise BridgeError(f"Bridge busy ({self.path.name}); retry after the active operation") from exc
                time.sleep(0.05)

    def __exit__(self, *args: Any) -> None:
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: conservatively live
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise BridgeError("Invalid task_id (expected the exact CC-YYYYMMDD-hex ID returned by new)")
    return task_id


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise BridgeError("session_id must be an exact Claude session UUID")
    return session_id


def route_model(complexity: str, model: str | None = None,
                effort: str | None = None) -> tuple[str, str]:
    if complexity not in COMPLEXITY_ROUTING:
        raise BridgeError(f"complexity must be one of {tuple(COMPLEXITY_ROUTING)}")
    defaults = COMPLEXITY_ROUTING[complexity]
    chosen_model, chosen_effort = model or defaults[0], effort or defaults[1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", chosen_model):
        raise BridgeError("model must be a Claude model alias or model ID")
    if chosen_effort not in EFFORTS:
        raise BridgeError(f"effort must be one of {EFFORTS}")
    return chosen_model, chosen_effort


def validate_task(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("schema_version") != SCHEMA_VERSION:
        raise BridgeError("Unsupported task schema")
    validate_task_id(task.get("task_id", ""))
    for key in ("goal", "prompt"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise BridgeError(f"{key} must not be empty")
    if task.get("mode") not in MODES:
        raise BridgeError(f"mode must be one of {MODES}")
    workdir = Path(task.get("workdir") or "")
    if not workdir.is_absolute() or not workdir.is_dir():
        raise BridgeError("workdir must be an existing absolute directory")
    route_model(task.get("complexity", ""), task.get("model"), task.get("effort"))
    duration = task.get("max_runtime_minutes")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 480:
        raise BridgeError("max_runtime_minutes must be a finite number in (0, 480]")
    if task.get("prior_session_id") is not None:
        validate_session_id(task["prior_session_id"])
    return task


def build_claude_env(task: dict[str, Any]) -> dict[str, str]:
    # Preserve the user's existing Claude provider, login and proxy configuration.
    env = dict(os.environ)
    env["CC_BRIDGE_TASK"] = task["task_id"]
    return env


def build_command(task: dict[str, Any], resume_session_id: str | None = None) -> list[str]:
    """Use tool availability as well as permission modes; never bypass permissions."""
    cmd = [str(discover_claude_exe()), "-p", "Follow the task instructions supplied on stdin.",
           "--input-format", "text", "--output-format", "json",
           "--model", task["model"], "--effort", task["effort"],
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disable-slash-commands", "--settings", '{"disableAllHooks":true}',
           "--disallowedTools", "mcp__*"]
    if resume_session_id:
        cmd += ["--resume", validate_session_id(resume_session_id)]
    elif task.get("_new_session_id"):
        cmd += ["--session-id", validate_session_id(task["_new_session_id"])]
    if task["mode"] == "read_only":
        cmd += ["--permission-mode", "plan", "--tools", "Read,Glob,Grep"]
    else:
        cmd += ["--permission-mode", "acceptEdits", "--tools", "Read,Glob,Grep,Edit,Write,NotebookEdit,Bash"]
    return cmd


def build_prompt(task: dict[str, Any], continuation: str | None = None) -> str:
    mode = ("Inspect files only. Do not modify files or execute commands."
            if task["mode"] == "read_only" else
            "Modify only the selected workdir. Do not deploy, push to a remote, or change services. "
            "If a command needs permission, report it instead of trying to bypass the denial.")
    instructions = task["prompt"] if continuation is None else continuation
    return (f"[CC Bridge task {task['task_id']}]\nGoal: {task['goal']}\n"
            f"Working directory: {task['workdir']}\n{mode}\n"
            "Return findings and changed files in your final response. If blocked by a permission "
            "or missing user decision, begin the response with NEEDS_INPUT and explain what is needed.\n\n"
            f"{instructions}")


def parse_result_json(raw_stdout: str) -> dict[str, Any] | None:
    """Keep the original tolerant parser, but reject arrays and scalar payloads."""
    raw = (raw_stdout or "").strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    for line in reversed(raw.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    return None


def classify_outcome(payload: dict[str, Any] | None, raw: str, exit_code: int) -> tuple[str, str | None]:
    # Inspect API error output, never a successful review quoting a rate-limit message.
    if payload is None or exit_code or payload.get("is_error") or payload.get("subtype") != "success":
        if (payload or {}).get("api_error_status") == 429 or re.search(
                r"(?i)(?:usage|rate) limit (?:reached|exceeded)|you(?:'ve| have) hit your limit", raw):
            return "WAITING_USAGE_RESET", "usage"
        return "FAILED", "result_parse" if payload is None else "claude"
    if payload.get("permission_denials"):
        return "NEEDS_INPUT", "permission"
    if re.search(r"^\s*NEEDS_INPUT\b", str(payload.get("result", "")), re.MULTILINE):
        return "NEEDS_INPUT", "decision"
    return "COMPLETED", None


def stop_process(proc: subprocess.Popen[Any]) -> None:
    """Stop the child tree that this runner owns; never kill a saved arbitrary PID."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        killed = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if killed.returncode:
            # Killing just the parent would falsely claim its descendants stopped.
            raise BridgeError("Cannot terminate the child process tree: " +
                              killed.stderr.decode("utf-8", errors="replace").strip())
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=15)


class Bridge:
    def __init__(self, home: Path | None = None):
        self.home = (home or runtime_home()).expanduser().resolve()
        self.tasks = self.home / "tasks"
        self.results = self.home / "results"
        self.runtime = self.home / "runtime"
        from cc_governance import Governance
        self.governance = Governance(self)

    def _state_lock(self) -> FileLock:
        return FileLock(self.runtime / "state.lock")

    def _task_path(self, task_id: str) -> Path:
        return self.tasks / validate_task_id(task_id) / "task.json"

    def _result_path(self, task_id: str) -> Path:
        return self.results / validate_task_id(task_id) / "result.json"

    def _task(self, task_id: str) -> dict[str, Any]:
        path = self._task_path(task_id)
        if not path.is_file():
            raise BridgeError(f"No such task: {task_id}")
        task = read_json(path)
        if task.get("task_id") != task_id:
            raise BridgeError("Task ID disagrees with its directory")
        return task

    def _record(self, task_id: str) -> dict[str, Any]:
        record = read_json(self._result_path(task_id))
        if record.get("task_id") != task_id:
            raise BridgeError("Result ID disagrees with its directory")
        pid = record.get("runner_pid")
        if record["status"] in ACTIVE_STATUSES and pid and not pid_alive(pid):
            record.update(status="FAILED", stage="runner_exit", ended_at=utc_iso(),
                          summary="Runner exited unexpectedly; inspect the attempt logs before resuming")
            write_json_atomic(self._result_path(task_id), record)
        return record

    def create_task(self, goal: str, workdir: str, prompt: str, *, mode: str = "read_only",
                    complexity: str = "normal", model: str | None = None, effort: str | None = None,
                    max_runtime_minutes: float = DEFAULT_MAX_RUNTIME_MINUTES,
                    prior_session_id: str | None = None) -> dict[str, Any]:
        model, effort = route_model(complexity, model, effort)
        task_id = f"CC-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:12]}"
        task = validate_task({"schema_version": SCHEMA_VERSION, "task_id": task_id,
                              "created_at": utc_iso(), "goal": goal, "workdir": workdir,
                              "prompt": prompt, "mode": mode, "complexity": complexity,
                              "model": model, "effort": effort, "max_runtime_minutes": max_runtime_minutes,
                              "prior_session_id": prior_session_id})
        task["workdir"] = str(Path(workdir).resolve())
        with self._state_lock():
            write_json_atomic(self._task_path(task_id), task)
            record = {"schema_version": SCHEMA_VERSION, "task_id": task_id, "status": "QUEUED",
                      "attempt": 0, "session_id": prior_session_id, "runner_pid": None,
                      "dispatched": False, "summary": "Task created", "mode": mode,
                      "model": model, "effort": effort, "created_at": task["created_at"]}
            write_json_atomic(self._result_path(task_id), record)
        return {"success": True, **record}

    def get_task(self, task_id: str, full: bool = False) -> dict[str, Any]:
        with self._state_lock():
            self._task(task_id)
            record = self._record(task_id)
        if full:
            if record.get("report_path"):
                from cc_governance import canonical_report_committed
                record["report_validated"] = canonical_report_committed(Path(record["report_path"]).parent, record)
            return {"success": True, **record}
        keys = ("task_id", "status", "attempt", "session_id", "model", "effort", "mode",
                "summary", "stage", "started_at", "ended_at", "report_path")
        return {"success": True, **{key: record[key] for key in keys if key in record}}

    def list_tasks(self, limit: int = 20) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise BridgeError("limit must be an integer in [1, 1000]")
        with self._state_lock():
            paths = sorted(self.tasks.glob("*/task.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            records = [self._record(p.parent.name) for p in paths[:limit]]
        return {"success": True, "tasks": [{k: r.get(k) for k in (
            "task_id", "status", "attempt", "model", "mode", "summary")} for r in records]}

    def _guard_active(self, task_id: str) -> None:
        for path in self.results.glob("*/result.json"):
            if path.parent.name == task_id:
                continue
            other = self._record(path.parent.name)
            if other.get("cleanup_required"):
                raise BridgeError(f"Task {other['task_id']} requires manual process cleanup; inspect its result")
            if other["status"] in ACTIVE_STATUSES and other.get("dispatched"):
                raise BridgeError(f"One task at a time: {other['task_id']} is active; wait or cancel it")

    def _dispatch(self, task: dict[str, Any], record: dict[str, Any], continuation: str | None) -> dict[str, Any]:
        task_id = task["task_id"]
        self._guard_active(task_id)
        record.update(status="QUEUED", dispatched=True, cancel_requested=False,
                      launch_id=uuid.uuid4().hex, continuation=continuation, runner_pid=None,
                      summary="Queued for execution", stage=None, ended_at=None, exit_code=None,
                      result=None, claude_result=None, report_path=None)
        write_json_atomic(self._result_path(task_id), record)
        out_dir = self.results / task_id
        runner = [sys.executable, str(Path(__file__).resolve()), "run", task_id,
                  "--launch-id", record["launch_id"]]
        env = dict(os.environ, CC_BRIDGE_HOME=str(self.home))
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        try:
            with (out_dir / "runner.log").open("ab") as log:
                proc = subprocess.Popen(runner, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        creationflags=flags, start_new_session=os.name != "nt", close_fds=True)
            record["runner_pid"] = proc.pid
            write_json_atomic(self._result_path(task_id), record)
        except OSError as exc:
            record.update(status="FAILED", stage="runner_launch", summary=str(exc), dispatched=False)
            write_json_atomic(self._result_path(task_id), record)
            raise BridgeError(f"Cannot start runner: {exc}") from exc
        return {"success": True, "task_id": task_id, "status": "QUEUED", "runner_pid": proc.pid}

    def submit_task(self, task_id: str) -> dict[str, Any]:
        from cc_governance import UsageBlocked
        with self._state_lock():
            task = validate_task(self._task(task_id))
            record = self._record(task_id)
            if record["status"] != "QUEUED" or record.get("dispatched"):
                raise BridgeError("Task already dispatched or finished; use resume for a saved session")
        try:
            usage = self.governance.gate(task)
        except UsageBlocked as exc:
            return self.governance.wait_task(task_id, exc.usage, before_launch=True)
        with self._state_lock():
            record = self._record(task_id)
            if record["status"] != "QUEUED" or record.get("dispatched"):
                raise BridgeError("Task is no longer available for submission")
            result = self._dispatch(task, record, None)
            return {**result, "usage": usage}

    def resume_task(self, task_id: str, prompt: str = "Continue the existing task.", *,
                    model: str | None = None, effort: str | None = None) -> dict[str, Any]:
        from cc_governance import UsageBlocked
        if not isinstance(prompt, str) or not prompt.strip():
            raise BridgeError("Resume prompt must not be empty")
        with self._state_lock():
            task = validate_task(self._task(task_id))
            record = self._record(task_id)
            if record.get("cleanup_required"):
                raise BridgeError("Manual process cleanup is required before resuming this task")
            if record["status"] not in RESUMABLE_STATUSES:
                raise BridgeError(f"Cannot resume a task in {record['status']}")
            if record.get("session_id"):
                validate_session_id(record["session_id"])
            elif not record.get("waiting_before_launch"):
                raise BridgeError("session_id must be an exact saved Claude session UUID")
            task["model"], task["effort"] = route_model(task["complexity"], model or task["model"], effort or task["effort"])
            write_json_atomic(self._task_path(task_id), task)
        try:
            usage = self.governance.gate(task)
        except UsageBlocked as exc:
            return self.governance.wait_task(task_id, exc.usage, prompt, record.get("waiting_before_launch", False))
        with self._state_lock():
            record = self._record(task_id)
            if record["status"] not in RESUMABLE_STATUSES or record.get("dispatched"):
                raise BridgeError("Task is no longer available for resume")
            result = self._dispatch(task, record, prompt if record.get("session_id") else None)
        self.governance.event({**record, "status": "QUEUED"}, "TASK_RESUMED")
        return {**result, "usage": usage}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._state_lock():
            self._task(task_id)
            record = self._record(task_id)
            if record["status"] == "CANCELLED":
                return {"success": True, "task_id": task_id, "status": "CANCELLED"}
            if record["status"] not in (*ACTIVE_STATUSES, "WAITING_USAGE_RESET"):
                raise BridgeError(f"Cannot cancel a task in {record['status']}")
            # A queued child has not entered run_task; its launch ID cannot revive a cancellation.
            state = "CANCELLING" if record["status"] in ("RUNNING", "CANCELLING") else "CANCELLED"
            record.update(status=state, cancel_requested=True, auto_resume=False, auto_wait=False, summary="Cancellation requested")
            if state == "CANCELLED":
                record["ended_at"] = utc_iso()
            write_json_atomic(self._result_path(task_id), record)
        if state == "CANCELLED":
            self.governance.refresh_usage("cancel")
            self.governance.event(record)
        return {"success": True, "task_id": task_id, "status": state}

    def run_task(self, task_id: str, launch_id: str | None = None) -> dict[str, Any]:
        validate_task_id(task_id)
        with FileLock(self.runtime / "runner.lock", timeout=0):
            with self._state_lock():
                task = validate_task(self._task(task_id))
                record = self._record(task_id)
                if record["status"] != "QUEUED":
                    raise BridgeError(f"Cannot run a task in {record['status']}")
                if record.get("dispatched") and record.get("launch_id") != launch_id:
                    raise BridgeError("Task already reserved by another runner")
                if launch_id is not None and record.get("launch_id") != launch_id:
                    raise BridgeError("Runner launch ID is no longer valid")
                self._guard_active(task_id)
                continuation = record.pop("continuation", None)
                session_id = record.get("session_id")
                record.update(status="RUNNING", dispatched=True, runner_pid=os.getpid(),
                              started_at=utc_iso(), ended_at=None, stage=None, exit_code=None,
                              attempt=int(record["attempt"]) + 1, model=task["model"], effort=task["effort"])
                write_json_atomic(self._result_path(task_id), record)
            proc: subprocess.Popen[Any] | None = None
            attempt_dir = self.results / task_id / f"attempt-{record['attempt']}"
            payload = None
            from cc_governance import UsageBlocked, UsageRefresher, ensure_report, canonical_report_committed
            refresher = UsageRefresher(self.governance)
            try:
                attempt_dir.mkdir(parents=True)
                record["pre_usage"] = self.governance.gate(task)
                if not session_id:
                    record["session_id"] = str(uuid.uuid4())
                    task["_new_session_id"] = record["session_id"]
                    with self._state_lock():
                        write_json_atomic(self._result_path(task_id), record)
                command = build_command(task, session_id)
                refresher.start()
                with tempfile.TemporaryFile() as input_file, (attempt_dir / "stdout.log").open("wb") as out, (attempt_dir / "stderr.log").open("wb") as err:
                    input_file.write(build_prompt(task, continuation).encode("utf-8"))
                    input_file.seek(0)
                    # Serialize the final permission check with consent revocation.
                    with FileLock(self.runtime / "usage.lock"):
                        current = self.governance.config.load()
                        if not current["usage"]["enabled"] or not current["usage"]["authorization"]["authorized"]:
                            raise BridgeError("Usage authorization was revoked before launch")
                        self.governance.check_workspace(task)
                        if self.governance.blocked_windows(record["pre_usage"]):
                            raise UsageBlocked(record["pre_usage"])
                        proc = subprocess.Popen(command, cwd=task["workdir"], env=build_claude_env(task),
                                                stdin=input_file, stdout=out, stderr=err,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                                start_new_session=os.name != "nt")
                    deadline = time.monotonic() + task["max_runtime_minutes"] * 60
                    cancelled = timed_out = usage_paused = False
                    while proc.poll() is None:
                        with self._state_lock():
                            cancelled = self._record(task_id).get("cancel_requested", False)
                        timed_out = time.monotonic() >= deadline
                        snapshot = refresher.latest
                        usage_paused = bool(snapshot and snapshot.get("state") == "VALID"
                                            and self.governance.blocked_windows(snapshot))
                        if cancelled or timed_out or usage_paused:
                            stop_process(proc)
                            break
                        time.sleep(0.1)
                raw = (attempt_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
                stderr = (attempt_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")
                payload = parse_result_json(raw)
                state, stage = classify_outcome(payload, raw + "\n" + stderr, proc.returncode)
                if cancelled:
                    state, stage = "CANCELLED", "cancel"
                elif timed_out:
                    state, stage = "FAILED", "timeout"
                elif usage_paused:
                    state, stage = "WAITING_USAGE_RESET", "usage"
                    record["blocked_usage"] = refresher.latest
                candidate = (payload or {}).get("session_id")
                if candidate:
                    record["session_id"] = validate_session_id(candidate)
                record.update(status=state, stage=stage, exit_code=proc.returncode, waiting_before_launch=False,
                              summary=str((payload or {}).get("result") or stderr or "No result returned")[:4000],
                              result=(payload or {}).get("result"), claude_result=payload)
            except UsageBlocked as exc:
                if proc is None:
                    record["session_id"] = session_id
                record.update(status="WAITING_USAGE_RESET", stage="usage", blocked_usage=exc.usage,
                              waiting_before_launch=True, summary="Usage ceiling reached before launch")
            except Exception as exc:
                if proc is None:
                    record["session_id"] = session_id
                record.update(status="FAILED", stage="bridge", summary=f"{type(exc).__name__}: {exc}")
            finally:
                # A failure after spawning must not leave an untracked paid child running.
                refresher.stop()
                record["post_usage"] = self.governance.refresh_usage("terminal")
                cleanup_error = None
                try:
                    if proc is not None:
                        stop_process(proc)
                except (BridgeError, OSError, subprocess.SubprocessError) as exc:
                    cleanup_error = str(exc)
                finally:
                    with self._state_lock():
                        latest = self._record(task_id)
                        if cleanup_error:
                            record.update(status="FAILED", stage="cleanup", summary=cleanup_error,
                                          child_pid=proc.pid if proc else None, cleanup_required=True)
                        elif latest.get("cancel_requested"):
                            record.update(status="CANCELLED", stage="cancel", summary="Task cancelled")
                        record.update(ended_at=utc_iso(), dispatched=False, runner_pid=None,
                                      stdout_path=str(attempt_dir / "stdout.log"),
                                      stderr_path=str(attempt_dir / "stderr.log"))
                        report_path = attempt_dir / "report.md"
                        try:
                            ensure_report(attempt_dir, record)
                            if not canonical_report_committed(attempt_dir, record):
                                raise BridgeError("Current-attempt report validation failed")
                            record["report_path"] = str(report_path)
                            record["report_validated"] = True
                        except (BridgeError, OSError) as exc:
                            record.update(status="FAILED", stage="report", report_path=None,
                                          report_validated=False, summary=str(exc))
                        write_json_atomic(attempt_dir / "result.json", record)
                        write_json_atomic(self._result_path(task_id), record)
            if record["status"] == "WAITING_USAGE_RESET":
                usage = record.pop("blocked_usage", None) or record["post_usage"]
                if usage.get("state") == "VALID":
                    return self.governance.wait_task(task_id, usage, continuation, record.get("waiting_before_launch", False))
            self.governance.event(record)
            return {"success": record["status"] == "COMPLETED", **record}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc_bridge", description="CC Bridge: local Claude Code delegation")
    parser.add_argument("--home", help="override runtime storage for this invocation")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a task without starting Claude")
    new.add_argument("--goal", required=True)
    new.add_argument("--workdir", required=True)
    new.add_argument("--prompt-file", required=True)
    new.add_argument("--mode", choices=MODES, default="read_only")
    new.add_argument("--complexity", choices=tuple(COMPLEXITY_ROUTING), default="normal")
    new.add_argument("--model")
    new.add_argument("--effort", choices=EFFORTS)
    new.add_argument("--max-runtime-minutes", type=float, default=DEFAULT_MAX_RUNTIME_MINUTES)
    new.add_argument("--prior-session-id", help="explicitly continue an existing Claude session UUID")
    for name in ("submit", "status", "result", "cancel"):
        node = sub.add_parser(name)
        node.add_argument("task_id")
    runner = sub.add_parser("run", help="run a newly created task in the foreground")
    runner.add_argument("task_id")
    runner.add_argument("--launch-id", help=argparse.SUPPRESS)
    resume = sub.add_parser("resume", help="continue a saved session in the background")
    resume.add_argument("task_id")
    resume.add_argument("--prompt", default="Continue the existing task.")
    resume.add_argument("--model")
    resume.add_argument("--effort", choices=EFFORTS)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    for name in ("usage", "usage-refresh", "usage-revoke", "environment", "scheduler-tick"):
        sub.add_parser(name)
    for name in ("usage-authorize",):
        node = sub.add_parser(name)
        node.add_argument("--yes", action="store_true", help="record explicit user consent already obtained")
    for name in ("trust-workspace", "revoke-workspace"):
        node = sub.add_parser(name)
        node.add_argument("path")
        node.add_argument("--yes", action="store_true")
    node = sub.add_parser("pending")
    node.add_argument("--include-done", action="store_true")
    for name in ("pending-claim", "pending-done"):
        node = sub.add_parser(name)
        node.add_argument("event_id")
    for name in ("schedule-reset-reminder", "usage-reset-notify"):
        node = sub.add_parser(name)
        node.add_argument("task_id")
    config = sub.add_parser("config", help="inspect or validate non-secret runtime configuration")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    config_sub.add_parser("show")
    config_sub.add_parser("validate")
    setting = config_sub.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value", help="JSON value, e.g. 95 or false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bridge = Bridge(Path(args.home) if args.home else None)
        gov = bridge.governance
        if args.command == "config":
            if args.config_action == "set":
                result = gov.config.set_value(args.key, json.loads(args.value))
                gov.arm_scheduler()
            else:
                result = gov.config.load()
            result = {"success": True, "config": result}
        elif args.command == "scheduler-tick":
            result = gov.verify_reminders()
        elif args.command == "usage":
            result = gov.usage()
        elif args.command == "usage-refresh":
            result = gov.refresh_usage()
        elif args.command == "usage-revoke":
            result = gov.revoke()
        elif args.command == "usage-authorize":
            confirmed = args.yes
            if not confirmed and sys.stdin.isatty():
                confirmed = input("Allow local Claude authentication access solely for subscription usage? [y/N] ").lower() == "y"
            result = gov.authorize(confirmed)
        elif args.command in ("trust-workspace", "revoke-workspace"):
            result = gov.trust(args.path, revoke=args.command == "revoke-workspace", confirmed=args.yes)
        elif args.command == "pending":
            result = gov.pending(args.include_done)
        elif args.command in ("pending-claim", "pending-done"):
            result = gov.change_event(args.event_id, done=args.command == "pending-done")
        elif args.command == "schedule-reset-reminder":
            result = gov.schedule_reset_reminder(args.task_id)
        elif args.command == "usage-reset-notify":
            validate_task_id(args.task_id)
            result = gov.verify_reminders(args.task_id)
        elif args.command == "environment":
            from cc_governance import timezone_info
            result = {"success": True, "home": str(bridge.home), **timezone_info()}
        elif args.command == "new":
            result = bridge.create_task(args.goal, args.workdir, Path(args.prompt_file).read_text(encoding="utf-8-sig"),
                                        mode=args.mode, complexity=args.complexity, model=args.model, effort=args.effort,
                                        max_runtime_minutes=args.max_runtime_minutes, prior_session_id=args.prior_session_id)
        elif args.command == "submit":
            result = bridge.submit_task(args.task_id)
        elif args.command == "run":
            result = bridge.run_task(args.task_id, args.launch_id)
        elif args.command == "resume":
            result = bridge.resume_task(args.task_id, args.prompt, model=args.model, effort=args.effort)
        elif args.command == "cancel":
            result = bridge.cancel_task(args.task_id)
        elif args.command == "list":
            result = bridge.list_tasks(args.limit)
        else:
            result = bridge.get_task(args.task_id, full=args.command == "result")
        result.setdefault("success", True)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("success") else 1
    except (BridgeError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"success": False, "error": {"type": "bridge_error", "message": str(exc)},
                          "usage": getattr(exc, "usage", None)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
