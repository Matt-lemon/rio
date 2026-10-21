# Execution Core

`Catalog.list/describe`, `ToolRunner.call/status` and `Task.submit/status/pause/resume/cancel/events` expose the existing flow. Tasks retain original request/completion criteria, steps, call IDs and results. Calls use stable request IDs; a duplicate ID with different arguments is rejected.

## Persistence and phases

`Store` uses a private 0700 directory and 0600 SQLite file, WAL and additive migrations. It retains legacy records instead of fabricating their history.

- **PREPARED:** durable reservation exists before adapter entry.
- **ATTEMPTED:** the adapter boundary is about to be crossed; this is not a receipt.
- **ACCEPTED:** a structured provider/bridge receipt was observed, when available.
- **RESULT_CONFIRMED:** a structured terminal outcome was recorded. This can be rejection or failure and does not itself mean success or physical confirmation.

Not every provider emits every stage. A read or a preflight rejection can finish without ACCEPTED. Old records use LEGACY_UNCLASSIFIED rather than guessed phases. UNKNOWN/DISPATCHED/IN_FLIGHT remain unresolved; opaque legacy control replies are not promoted to confirmed success.

## Recovery and interruption

A timeout/disconnect/restart is not permission to resend a change. Runner queries the same request ID through the adapter result interface; absent or inconclusive lookup remains UNKNOWN. Task resume first checks unresolved calls and holds the task when completion cannot be established. A pause/cancel affects future work; it does not retract a command already sent.

Store executor tokens and per-task fences reject stale owners. A cooperating OS dispatch lock serializes ownership takeover with a live adapter boundary; process death releases that lock. These are single-host, cooperating-writer controls, not global exactly-once delivery across arbitrary providers/apps.

The default execution slice is eight calls / 300 seconds. Reaching a limit stores progress and waits for explicit continuation. Recent conversation uses 12 messages and an at-most-800-character summary. Original requests, corrections, stops, grants and tool evidence remain separate authoritative records; summary does not add permissions or erase failures.

Tests include real subprocess ownership/takeover and SIGKILL with mock transport, stale result fencing, pause/correction races and persistent budgets. The read-only demo uses the same engine/store; it is not a replacement executor.
