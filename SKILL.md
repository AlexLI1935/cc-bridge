---
name: cc-bridge
description: Deploy and operate CC Bridge to delegate repository review, debugging and implementation tasks to a local Claude Code CLI through MCP or CLI, with persisted usage ceilings, authorized workspaces, reset recovery and completion events. Use when Claude Code is the selected backend; skip ordinary explanations requiring no external coding agent.
license: MIT
metadata:
  version: "1.1.2"
  author: Alex Li
  homepage: https://github.com/AlexLI1935
  repository: https://github.com/AlexLI1935/cc-bridge
---

# CC Bridge

CC Bridge orchestrates a **local Claude Code CLI**, not a direct model API client.
Require Python 3.10+, an authenticated account/provider and a callable `claude`.
Mechanically check `claude --version`; a desktop application's presence alone is
insufficient. MCP support is optional. Usage inspection currently supports the
file-based subscription credential store; unsupported auth returns unknown/stale.

## When to use

Use for authorized delegation to Claude Code: repository inspection, code review,
debugging or implementation. Do not invoke for ordinary questions or explanations,
or when no working local CLI is available.

## First installation

1. Check `claude --version`. If unavailable, ask the user to install/enable the CLI.
2. Install the bridge and optional MCP dependencies. Run `config show`,
   `config validate` and `environment` using its interpreter. Runtime config is
   **`CC_BRIDGE_HOME/config.json`**, automatically initialized and authoritative.
3. Obtain explicit one-time user consent before accessing local Claude auth:
   “Allow CC Bridge to read the local Claude Code authentication credential only
   for retrieving Claude subscription usage information.” Then run
   `usage-authorize --yes`. Never self-grant consent. No MCP authorization tool exists.
4. If the host supports persistent memory, **ask permission to save the rule below**.
   Save it only after the user agrees. A host without memory still uses the local
   config authorization as its authoritative record. Never store credentials in memory.
5. Inspect detected OS timezone/clock metadata, fresh usage and both windows.
   Do not infer timezones from model output, language, IP or current offset.
6. If authorized for a real task, verify its lifecycle and one completion event.
   For offline installation checks run the test suite with fake Claude; do not
   spend account usage just to demonstrate installation without authorization.
7. Report setup status, consent, workspace policy and any unsupported auth/scheduler.

Recommended persistent rule, after the user's separate memory approval:

> Any CC Bridge task must first inspect fresh five-hour and weekly Claude usage.
> Automatic CC Bridge work must obey the configured usage ceilings and preserve
> the configured emergency reserve. CC Bridge may inspect local Claude Code
> authentication data solely for usage retrieval after explicit user authorization.
> Credentials themselves must never be stored in memory.

Agent memory is a convenience; `config.json` is runtime authority. Do not copy
access/refresh tokens, passwords, cookies, or credential files into memory/config.

## Before every task

Inspect current five-hour and weekly usage, remaining capacity and absolute reset
times before work. `get_cc_usage` reads cache only. Call `refresh_cc_usage` (or CLI
`usage-refresh`) when a query is due; inspect `refresh_deferred`, `fetched_at` and
`next_refresh_at`. Historical/stale data must never be presented as current availability.

**Never send consecutive usage queries. Actual usage requests must be at least five
minutes apart.** `usage.refresh_interval_minutes` is the shared minimum query interval
and running refresh interval: default **5 minutes**, accepted range **5..1440**.
CLI, MCP, submit, runner, resume and scheduler share a persisted cooldown within
`CC_BRIDGE_HOME`; use the same home for the same account. Repeated refresh calls
during that interval return the existing snapshot without reading credentials or
contacting Anthropic. Reuse is allowed only while the snapshot remains valid under
`usage.fresh_seconds` (default and maximum 300 seconds). Cache age **below 300 seconds**
is eligible; at **300 seconds or later**, refresh when the cooldown permits, then
reread the saved cache and use the updated result. A failed refresh never makes old
data eligible. A crossed reset invalidates a snapshot
from before that reset. No valid snapshot means no launch until a permitted query
succeeds. Never delete the cooldown or change homes to force a query.

HTTP 429 and other failed requests also consume the query interval. Honor
`next_refresh_at`; a longer service `Retry-After` takes precedence. Do not immediately
retry failures or add usage refreshes to each task-status poll. Polling `get_cc_task`
does not query usage. No cached response is proof that a reset has restored quota.

Runtime `submit`/`run`/`resume` independently validates config, checks consent,
refreshes when due or validates the bounded cache, applies both ceilings and workspace policy, and uses the kernel
single-task lock. Remembering this skill is not the safety boundary.

- Default five-hour ceiling: **90%**, aiming to leave 10% for manual work.
- Default weekly ceiling: **95%**, aiming to leave 5% for emergencies.
- Utilization must be strictly below **both** configured ceilings. These defaults
  live in config and are adjustable; do not recreate independent reserve settings.
- `usage.enabled=false` blocks automatic work and credential reads; it is not a bypass.
- UNKNOWN, STALE, AUTH_REQUIRED, AUTH_STALE, ENDPOINT_ERROR and CLOCK_SKEW do not
  authorize starting work. Explain the reason; never infer availability.
- `read_only` exposes Read/Glob/Grep in plan mode. It cannot execute tests.
- `write` uses dontAsk with pre-approved coding tools and requires an
  explicitly trusted root. Use `trust-workspace <absolute-root> --yes` only within
  user authorization. Revoke via `revoke-workspace`; do not trust arbitrary disks.
- Workdir policy and tool restrictions are not an OS sandbox. Use trusted repositories.

## Submit, inspect and verify

Use `submit_cc_task(goal, workdir, prompt, mode="read_only")`. The workdir must be
an existing absolute directory. It launches the runner or returns persisted
`WAITING_USAGE_RESET`. Keep `task_id`. Do not start a second runner command.

Poll `get_cc_task(task_id)` at reasonable intervals. QUEUED/RUNNING/CANCELLING are
active. For a stopped task read `get_cc_task(task_id, full=true)`: status, stage,
result, `report_path`, `report_validated`, logs and usage/wait metadata.
`success=true` on a read only means the read worked, not that the task completed.

Each attempt has one canonical report and a receipt binding its task ID, attempt
and hash. Reject missing, changed or previous-attempt reports. This validates
the report artifact, not the correctness of model-produced code. Inspect changed
files and run appropriate checks before reporting completion.

Consume events via `get_cc_pending`, `claim_cc_pending(event_id=...)`, then
`done_cc_pending(event_id=...)` after handling. `first_claim=true` occurs once.
Claimed events persist across client restarts; recover interrupted processing
explicitly. Task-ID forms are supported only when one unfinished event matches.

`list_cc_tasks` recovers recent IDs. `cancel_cc_task` stops active/waiting work;
poll through CANCELLING. It does not undo edits. Intended manual continuation uses
`resume_cc_task(task_id, prompt=...)`, retaining the exact session and mode.
No saved UUID means no replay of interrupted work. A task blocked before first
launch can be started later without a prior session.

## Persisted waiting and automatic continuation

Each window has `auto_wait_for_reset` and `auto_resume_after_reset`, default true.
Threshold-blocked tasks persist their blocked windows, latest usage, session UUID,
attempt, cycle ID, reset epoch/UTC, local display, continuation and next check.
State survives bridge/agent/system restarts.

The candidate is **max(actual reset instants of all blocking windows)**, followed
by `usage.reset_grace_seconds` (default 60). Never use “now + five hours”. At due
time, obtain a snapshot fetched after the reset and run the gates again. If the
query cooldown has not elapsed, keep waiting until a permitted refresh; if blocked,
use the latest response to wait again. Scheduler/caller duplicates cannot reserve two runners
for a cycle. The runner rechecks gates before invoking Claude.

Events trace the sequence: TASK_WAITING_USAGE_RESET → USAGE_AVAILABLE → TASK_RESUMED,
then task completion/failure/cancellation events. Five-minute running checks are
configurable through `usage.refresh_interval_minutes`; a ceiling observed during
execution stops the child for later continuation. Delayed provider reporting and
work between checks can overshoot reserves; do not promise exact billing caps.

Windows registers a per-home one-shot Task Scheduler wakeup with StartWhenAvailable.
Check the reported scheduler delivery. If registration is refused, or on Linux/macOS,
state remains durable but requires `get_cc_pending`, `scheduler-tick`, or a host-owned
scheduler to wake it. Do not promise autonomous wakeup in caller-polling mode.
Disable both window auto-resume policies when the user wants manual-only continuation.

Scheduling uses OS clock, OS timezone and response absolute timestamps. Reset
UTC/epoch is authoritative; local text is display/OS representation. Convert using
the target instant's rules, including DST. Startup/refresh update timezone metadata
and pending displays; changed zones rebuild scheduling when needed. CLOCK_SKEW
blocks creating a schedule until the system clock is corrected.

## Configuration and security

Use `config show`, `config validate`, or `config set <dotted-key> <JSON-value>`.
Direct edits must pass validation. Default paths: Windows LOCALAPPDATA/cc-bridge,
otherwise ~/.cc-bridge. `CC_BRIDGE_HOME` overrides storage. PATH discovery takes
precedence over `CC_BRIDGE_CLAUDE_PATH`. Runtime data never belongs in Git.

Usage access is opt-in. Token lifecycle: local Claude file → process memory →
fixed Anthropic HTTPS endpoint → discarded; redirects are refused. No raw request,
token, response dump or exception text is saved. Failed refresh preserves last-valid
percentages as stale, with `last_valid_at`. The endpoint is not a documented stable
public API contract and may change. Authentication refresh belongs to Claude CLI:
try non-generative auth status, then reread. The optional model-based refresh probe
is disabled by default and may use quota when separately enabled.

Optional `CC_BRIDGE_WAKE_COMMAND` is a local JSON argv array, not a shell string.
It receives event/task IDs. Hooks are best effort; the event outbox remains durable.
No chat service or agent-specific callback is built in. Keep prompts/logs/results private.

## CLI fallback

```text
python cc_bridge.py usage-refresh
python cc_bridge.py new --goal "Review" --workdir <absolute-project> --prompt-file examples/review.md
python cc_bridge.py submit <task_id>
python cc_bridge.py status <task_id>
python cc_bridge.py result <task_id>
python cc_bridge.py pending
python cc_bridge.py pending-claim <event_id>
python cc_bridge.py pending-done <event_id>
python cc_bridge.py resume <task_id> --prompt "Continue the investigation."
python cc_bridge.py cancel <task_id>
python cc_bridge.py schedule-reset-reminder <task_id>
python cc_bridge.py usage-reset-notify <task_id>
python cc_bridge.py scheduler-tick
```

See [README.md](README.md), [中文说明](README.zh-CN.md) and
[config.example.json](config.example.json). Windows offline behavior and scheduler
XML are tested; real-account/live-model execution and OS wakeups are not part of
the offline suite. Linux/macOS are not officially verified.

## Headless coding and compatibility

Approving a workspace lets write tasks read, edit and run ordinary development
commands without a person approving each command. Write mode uses `dontAsk` with
`--allowedTools Read Glob Grep Edit Write NotebookEdit Bash`, and omits `--tools`.
Anything requiring approval outside those grants is denied. There is no automatic
bypass mode. Deployment, remote pushes and system changes remain outside the task
contract. Broad Bash authorization is not an OS sandbox or a directory confinement
guarantee; only trust projects and instructions you intend to execute.

Read-only retains the Read/Glob/Grep tool set and cannot execute Bash or edit.
Managed/user deny rules still take precedence; report denials rather than changing
global permissions. `environment` reports the actual installed Claude CLI version.
Windows CLI 2.1.286 was tested: Bash remained visible in a clean `--tools` comparison,
so the reported disappearance was not reproduced as a universal version defect.
Write avoids that option for compatibility. `acceptEdits` without Bash grants did
not provide the required unattended command contract. No fragile exact-version block
is imposed. See [permissions](https://code.claude.com/docs/en/permissions) and
[CLI reference](https://code.claude.com/docs/en/cli-reference).

## Time baseline: absolute scheduling, observed local display

On the first real task dispatch in each Bridge process, an unauthenticated HTTPS
HEAD request obtains a trusted external UTC Date. The bridge reads local wall time
and records their difference as `observed_utc_offset_minutes` in
`runtime/time-baseline.json`. This is not a subscription usage query. Subsequent
tasks in that process reuse the baseline. Restart, detected OS timezone/offset
change, a clock discontinuity or `config set timezone.refresh_observed_offset true`
causes a new observation. A failed probe is UNVERIFIED and falls back to OS/UTC
display; it does not invent an offset or grant permission to start tasks.

Reset, WAITING, cache age, cooldown and one-shot wakeups retain absolute UTC/epoch
truth. “In five hours” means `now_epoch + 18000`; running timeouts use a monotonic
clock. Automatic local display uses the observed current offset. A wrong OS label
alone does not block quota recovery. Actual system UTC skew against trustworthy UTC
still reports CLOCK_SKEW: an inaccurate absolute clock cannot safely drive epoch
scheduling. Neither probe nor diagnostics guesses geography or changes the OS.

IANA identity is optional for local recurrences and DST, not a reset prerequisite.
Explicit `timezone.name` plus `timezone.mode=explicit` can select IANA display rules;
default `system` display uses the observed offset when available. Install tzdata
for IANA rules (included in MCP dependencies). Daily 09:00 rules retain timezone,
local hour/minute and recurrence, then resolve each occurrence anew using
`cc_time.next_local_occurrence`; nonexistent times are skipped and repeated times
use the first fold once. This helper does not add a recurring execution service.
An observed offset describes the current wall clock, not future DST rules.
