# Architecture — frozen v0.1 preview

Rio is application-owned orchestration around a replaceable model endpoint. `rio_core.py` builds identity prompts and model payloads; it is not an all-powerful runtime class. `main.py` handles presentation and explicit command routing. `core/execution` owns durable goal execution. These existing modules remain separate.

Input/optional Voice → Gate → Core → Memory / Execution → Tool adapters → World is the conceptual responsibility chain. The actual code has explicit branches: `main.py` calls the evidence Gate for selected chat, while explicit tools route to protected adapters; `core/execution/planner.py` consults Gate for ordinary replies with no tool results when enabled. The HTTP Gate is **not** on every tool call.

| Boundary | Existing implementation | Authority and limits |
| --- | --- | --- |
| Input/presentation | `main.py`, `core/execution/console.py`, `rio_voice.py` | Text/audio presentation. No permission is created by phrasing a reply. Voice depends on optional external synthesis/playback. |
| Identity/model | `rio_core.py`, `config/identity.example.yaml` | Application configuration is separate from model weights. Real persona assets are not shipped. |
| Gate | `services/rio-gate/gate.py` | Rule-based evidence decisions for selected chat; caller supplies availability flags. No independent fact verification. |
| Memory | `core/memory/*`, `rio_memory_context.py` | Local JSON memory plus an HTTP client interface. External vector/relational servers and private data are not shipped. |
| Execution | `core/execution/{catalog,engine,runner,store}.py` | Schema/revision checks, durable task/call records and fenced dispatch. Model plans remain untrusted proposals. |
| Adapters | `core/execution/adapters.py`, existing provider modules | Recheck provider-specific permission/configuration; return structured results and confirmation levels. |
| World | External services, browser, players, devices | May fail, time out, report stale state or remain physically unconfirmed. |

The default service uses private Unix IPC and SQLite on one POSIX host. Optional separate-account Core/operator-bridge support exists, but the offline demo does not install accounts or services. A same-user runtime is not an OS security boundary.

Implemented boundaries are exercised by existing core and adapter tests. Browser/voice/device/model integrations are not implied by an offline test pass. Not implemented: a new planner, a generic worker framework, a UI rewrite, a distributed scheduler, universal permissions across all providers, or proof that a reported state equals physical reality.
