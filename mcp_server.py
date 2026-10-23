#!/usr/bin/env python3
"""The five-tool stdio MCP boundary. Business logic lives in cc_bridge.py."""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from cc_bridge import Bridge, BridgeError

mcp = FastMCP("cc-bridge", instructions=(
    "Delegate local repository tasks to Claude Code. Submit starts a background runner. "
    "Poll get_cc_task; full=true includes the final output. Usage ceilings are enforced from config.json. "
    "A blocked task persists in WAITING_USAGE_RESET and follows its configured resume policy. Default mode is read_only. "
    "Select write only for authorized changes. Resume only when continuation is intended."
))


def boundary(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return func(*args, **kwargs)
        except (BridgeError, OSError, ValueError, TypeError) as exc:
            return {"success": False, "error": {"type": "bridge_error", "message": str(exc)},
                    "usage": getattr(exc, "usage", None)}
    return wrapped


@mcp.tool()
@boundary
def submit_cc_task(goal: str, workdir: str, prompt: str, mode: str = "read_only",
                   complexity: str = "normal", model: str | None = None,
                   effort: str | None = None, max_runtime_minutes: float = 120,
                   prior_session_id: str | None = None) -> dict[str, Any]:
    """Create and start a task. workdir must be absolute. Returns immediately.

    read_only exposes Read/Glob/Grep; write allows edits using acceptEdits.
    prior_session_id is an optional exact Claude UUID for an intended continuation.
    """
    bridge = Bridge()
    created = bridge.create_task(goal, workdir, prompt, mode=mode, complexity=complexity,
                                 model=model, effort=effort, max_runtime_minutes=max_runtime_minutes,
                                 prior_session_id=prior_session_id)
    try:
        return bridge.submit_task(created["task_id"])
    except (BridgeError, OSError) as exc:
        # Preserve the created ID so the caller can retry via CLI or cancel it.
        return {"success": False, "task_id": created["task_id"],
                "error": {"type": "bridge_error", "message": str(exc)}}


@mcp.tool()
@boundary
def get_cc_task(task_id: str, full: bool = False) -> dict[str, Any]:
    """Read persisted state. full=true includes final text, Claude JSON and log paths."""
    return Bridge().get_task(task_id, full)


@mcp.tool()
@boundary
def resume_cc_task(task_id: str, prompt: str = "Continue the existing task.",
                   model: str | None = None, effort: str | None = None) -> dict[str, Any]:
    """Start another attempt using the exact saved session UUID and permission mode.

    An ended task with a saved UUID is required; a running task cannot be resumed.
    """
    return Bridge().resume_task(task_id, prompt, model=model, effort=effort)


@mcp.tool()
@boundary
def cancel_cc_task(task_id: str) -> dict[str, Any]:
    """Cancel a queued task or request termination of its running child tree.

    Poll until CANCELLED; CANCELLING means the runner is still stopping the child.
    """
    return Bridge().cancel_task(task_id)


@mcp.tool()
@boundary
def list_cc_tasks(limit: int = 20) -> dict[str, Any]:
    """List the newest local tasks, including status and attempt count (limit 1..1000)."""
    return Bridge().list_tasks(limit)



@mcp.tool()
@boundary
def get_cc_usage() -> dict[str, Any]:
    """Read cached five-hour/weekly subscription usage. Stale data is historical only."""
    return Bridge().governance.usage()


@mcp.tool()
@boundary
def refresh_cc_usage() -> dict[str, Any]:
    """Refresh when due; reuse valid cache during the shared >=5 minute cooldown.

    Inspect refresh_deferred and next_refresh_at; never retry in a tight loop.
    Requires prior user consent and never grants consent itself.
    """
    return Bridge().governance.refresh_usage()


@mcp.tool()
@boundary
def get_cc_pending(include_done: bool = False) -> dict[str, Any]:
    """Read durable completion events and verify due persisted reset reminders."""
    return Bridge().governance.pending(include_done)


def _event_id(bridge: Bridge, task_id: str | None, event_id: str | None) -> str:
    if event_id:
        return event_id
    rows = [r for r in bridge.governance.pending()["pending"] if r["task_id"] == task_id]
    if len(rows) != 1:
        raise BridgeError("Specify event_id when there is not exactly one unfinished task event")
    return rows[0]["event_id"]


@mcp.tool()
@boundary
def claim_cc_pending(task_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
    """Atomically claim one event; first_claim is true once. Prefer exact event_id."""
    bridge = Bridge()
    return bridge.governance.change_event(_event_id(bridge, task_id, event_id))


@mcp.tool()
@boundary
def done_cc_pending(task_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
    """Idempotently mark a handled event done. Claimed events survive client restarts."""
    bridge = Bridge()
    return bridge.governance.change_event(_event_id(bridge, task_id, event_id), done=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
