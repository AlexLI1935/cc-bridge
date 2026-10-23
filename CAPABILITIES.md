# Capability comparison

The production implementation and public releases were compared through source
inspection, CLI help, MCP stdio tool enumeration and offline behavior tests.
The original production code remains unchanged; private task history is not shipped.

| Capability | Production source | Public 1.0 | Public 1.1 |
| --- | --- | --- | --- |
| Local task lifecycle | Yes | Yes | Retained |
| Exact-session resume / cancel | Yes | Yes | Retained, with persisted resource waits |
| Subscription usage | Two-window OAuth query | Absent | Explicit consent; whitelisted two-window cache |
| Fresh start gate | Usage reporting, no mandatory fresh start gate | Absent | Required on submit, run and resume |
| Capacity reserve policy | Calling workflow decisions | Absent | Validated configurable 90% / 95% defaults |
| Stale-auth handling | CLI refresh probe | Absent | Status first; separately opt-in probe |
| Usage refresh | Running periodic and terminal | Absent | Shared persisted query interval (default/minimum 5 minutes), cache younger than 300 seconds and Retry-After backoff |
| Reset reminder | Fixed-zone OS scheduling and caller-specific wake path | Absent | Absolute instants, OS timezone rules, Windows wakeup / persisted polling fallback |
| Automatic continuation | User-controlled resume | Explicit resume | Persisted configurable auto-resume, fresh verification and cycle deduplication |
| Completion events | Persistent pending / claim / done | Absent | Per-attempt/cycle generic local outbox, optional argv wake hook |
| Canonical report | Canonical file existence, earlier-completion precedence | Per-attempt generated report | One report plus task/attempt/hash receipt; stale/conflicting reports rejected |
| Single-task lock | Exclusive PID file plus heartbeat | Kernel-backed FileLock | Kernel FileLock retained, transactional dispatch and resume guard |
| Workspace authorization | Optional deployment-specific authority contract | Write mode selection | Explicit trusted roots with revocation and resolved-path containment |
| Timezone | Deployment-specific fixed-zone helpers | UTC result timestamps | UTC/epoch scheduling plus OS local rules, DST and clock-skew checks |
| Persistent runtime config | Environment and deployment defaults | Environment | Validated non-secret config.json under runtime home |
| MCP tools | 9 enumerated | 5 | Original 5 plus 5 governance tools |
| Tests and CI | No standalone public suite in the source folder | 49 offline tests, Windows CI | Original suite retained/adapted; governance, config, DST, leakage and recovery coverage added |

The five public 1.0 MCP tool signatures are retained. Execution policy is deliberately
stricter: explicit usage consent, current known capacity and trusted write workspaces
are prerequisites. Existing task data remains readable. No production installation
is replaced by publishing this release.

Write: constrained headless dontAsk, no --tools; no implicit bypass. Time: UTC/epoch truth, explicit IANA override, OS identity, UTC fallback and DST-aware local recurrence conversion.

First-dispatch external UTC / wall-clock offset observation is cached per process; labels do not gate usage resets. Observed offset controls automatic display, never absolute reset epoch.
