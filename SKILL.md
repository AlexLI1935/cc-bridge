---
name: cc-bridge
description: Delegate coding, code review, debugging, and repository implementation tasks to a locally installed Claude Code through CC Bridge using MCP or CLI. Use when Claude Code is the selected execution backend; skip ordinary questions or code explanations that need no external coding agent.
license: MIT
metadata:
  version: "1.0.0"
  author: Alex Li
  homepage: https://github.com/AlexLI1935
  repository: https://github.com/AlexLI1935/cc-bridge
---

# CC Bridge

A local Claude Code Bridge for AI Agent delegation. The same core serves MCP
(Model Context Protocol) clients, including Codex, and agents that can run Python.

## When to use

- The user asks to delegate work to Claude Code, or Claude Code is the selected backend.
- A repository needs code review, debugging, implementation, or an authorized modification.

## When not to use

- Ordinary knowledge questions or code explanations need no external coding agent.
- Claude Code is unavailable or has not been configured for use on this computer.
- The task does not need delegation.

## Workflow

1. Choose an existing absolute `workdir` and a bounded prompt. Use `read_only`
   for inspection; choose `write` only for changes within the user's authorized scope.
2. With MCP, call `submit_cc_task(goal, workdir, prompt, mode="read_only")`.
   This starts the background runner; save the returned `task_id`.
3. Poll `get_cc_task(task_id)` at reasonable intervals. There is no notification
   channel or automatic wakeup. `QUEUED`, `RUNNING`, and `CANCELLING` are active states.
4. Once stopped, call `get_cc_task(task_id, full=true)`. Read `result`, `stage`,
   `claude_result`, and the attempt log/report paths. Tool success means a state
   read succeeded; the task itself succeeded only when `status` is `COMPLETED`.
5. Inspect changed files and verify the produced artifacts before reporting back.
   A model's completion message alone is insufficient verification.

`NEEDS_INPUT` means a permission or user decision is missing. `WAITING_USAGE_RESET`
means Claude reported a limit; no reset time is inferred. Do not automatically
retry either state. When continuation is intended, use
`resume_cc_task(task_id, prompt="Continue with these instructions: ...")`.
It uses the saved session UUID, retains the permission mode, and returns immediately.
It refuses an active task or a missing session UUID. It can also continue a completed
session. To start from an external session, submit with `prior_session_id` set to its UUID.

Use `cancel_cc_task(task_id)` to stop work. Poll through `CANCELLING` until
`CANCELLED`. Use `list_cc_tasks(limit=20)` to recover local task IDs.

## CLI fallback

From the CC Bridge checkout, using its Python interpreter:

```text
python cc_bridge.py new --goal "Review this project" --workdir <absolute-project-directory> --prompt-file examples/review.md
python cc_bridge.py submit <task_id>
python cc_bridge.py status <task_id>
python cc_bridge.py result <task_id>
python cc_bridge.py resume <task_id> --prompt "Continue the review."
python cc_bridge.py cancel <task_id>
```

`new` only creates a task. `submit` starts it in the background. `run <task_id>`
is the foreground alternative for a new, undispatched task.

## Permissions and configuration

- Default `read_only` exposes only Read/Glob/Grep in plan mode. It cannot execute tests.
- `write` uses `acceptEdits`; commands remain subject to Claude Code permissions.
  CC Bridge never enables permission bypass. MCP tools, hooks, and slash commands
  are disabled for the child session. This is not an OS sandbox: use trusted
  repositories and inspect local Claude configuration.
- One Claude task runs per `CC_BRIDGE_HOME`. There is no scheduler or automatic retry.
- `CC_BRIDGE_HOME` overrides local runtime storage. Claude is found on PATH first;
  `CC_BRIDGE_CLAUDE_PATH` is the fallback. The bridge never reads OAuth login files
  or queries account usage. Claude manages its own authentication.
- Logs and prompts may contain sensitive project data; do not publish runtime storage.

See [README.md](README.md) for installation, configuration, and tested platform limits.
