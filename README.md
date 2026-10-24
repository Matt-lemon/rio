# Rio

**Rio is the system, not the LLM.**

The model is a replaceable **brain slot**. Rio's application code owns identity configuration, memory access, durable state, policy checks, tool contracts and execution records. Swapping a model does not grant it authority over those components.

Rio is designed to remain a consistent local assistant as models, hardware and
integrations change. Identity, personality, memory and transient state have
separate responsibilities. Optional acceleration and individual adapters can
fail without becoming the owner of Rio's identity or execution history.

The next development direction is **reproducible operation**: describe the
environment, inspect changes, verify them independently and retain a way back.
This branch adds an optional Nix environment around the existing Core; it does
not replace the executor or claim to reproduce an entire personal deployment.

[개발 철학](docs/philosophy.md) · [Reproducible environment](docs/reproducibility.md)
· [Contributing](CONTRIBUTING.md)

**Experimental / not production-ready.** This source preview exposes an existing implementation and an offline demonstration. It is not a ready-to-deploy home controller or a new execution framework. Rio original code is licensed under **GPL-3.0-only**. See [LICENSE](LICENSE), the [license decision](docs/release/license-decision.md) and the [GPL/Apache compatibility audit](docs/release/v0.1-license-compatibility.md). Third-party terms remain in force.

```mermaid
flowchart LR
    I[Input / optional Voice] --> G[Gate: selected chat evidence checks]
    G --> C[Rio Core: identity / state / orchestration]
    I -. explicit commands .-> C
    C <--> M[Memory interfaces]
    C <--> L[Replaceable LLM brain slot]
    C --> E[Execution: Catalog / Task / ToolRunner]
    E --> T[Protected tool adapters]
    T --> W[World: external services / devices]
```

The Gate is an evidence-policy interface, not a universal authorization firewall. Explicit tools use their own contract/permission checks. See [actual routing and boundaries](docs/architecture.md).

## Offline quick start

Linux / Python 3.12 is the release-preview reference environment. No model, API key, GPU, Docker, personal NAS, browser session or appliance is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-preview.lock
python examples/offline_demo.py
```

The demo runs the **existing Task / Catalog / ToolRunner / SQLite Store**, executes the read-only `core.status` tool, reopens its private temporary state in a fresh process and prints the same request ID and recorded result. It does not call a model, start services, play audio or send device commands.

A second demonstration reuses an existing crash test with a fake backend:

```sh
python -m pytest -q tests/test_execution_ipc.py::test_actual_process_death_retains_same_key_and_never_resends
```

This tests SIGKILL/recovery and no second dispatch in the mock backend. It does not demonstrate physical-device control or a global exactly-once guarantee.

## Optional pinned Nix environment

On `x86_64-linux` with Nix installed:

```sh
nix --extra-experimental-features 'nix-command flakes' run --no-update-lock-file .
nix --extra-experimental-features 'nix-command flakes' flake check --no-update-lock-file
```

The committed `flake.lock` pins Nixpkgs and its content hash. The default app runs
the same offline demo above. Checks reuse existing execution/safety/crash tests
and the existing dependency license checker. Initial dependency downloads need
network access; the demo and tests use no external service. The Nix closure is a
separate minimal Core environment, not a replacement for `requirements-preview.lock`
or the full release gate. See [scope, verification and limitations](docs/reproducibility.md).

Environment rollback and data recovery have different rules: an older executable
must never silently restore an older memory database or erase an uncertain
command. Service migration, database compatibility and restoration are separate
operator-reviewed steps; no automatic deployment/rollback manager is added here.

## What is implemented

- Common tool discovery and bounded argument/revision checks.
- Durable tasks, request IDs, delivery phases, explicit pause/resume/cancel and same-ID result lookup.
- Single-host executor/task fencing and cooperating dispatch locks.
- UNKNOWN handling that holds uncertain effects instead of blindly replaying them.
- Existing model, memory, evidence-gate and optional voice interfaces.

[Execution](docs/execution-core.md) · [Memory](docs/memory.md) · [Gate](docs/gate.md) · [Model adapter](docs/model-adapter.md) · [Security](docs/security-model.md)

## Scope and limitations

Personal configuration, logs, databases, model weights, voice/character assets, Android deployment, unrelated game patches and full dubbing/screen/localization services are excluded. Their absence is deliberate; they are not replaced with fake implementations. Root-level client contracts can still describe optional external services, but those services are not installed or claimed as working by the demo.

The provided NAS deployment scripts are implementation references with example paths, not a portable production installer. Host account separation, external device behavior, speech synthesis, browser sessions, remote memory infrastructure and model quality require separate deployment and validation. **Not implemented:** distributed exactly-once execution, cryptographic proof of evidence truth, a fully sandboxed arbitrary-tool platform and a model-independent natural-language compatibility guarantee.

Core retains hardware/provider-specific guards. One device's unresolved legacy operation can conservatively block other operations in this frozen snapshot; that is a disclosed limitation, not a reason to redesign the executor before release. Public-preview tests do not operate real devices.

## Validation

```sh
python tools/ci/check_syntax.py
python tools/ci/run_tests.py
# Full clean-checkout sequence, including dependencies, demos and audits:
bash tools/ci/release-preview.sh
```

The release gate keeps existing core tests and the existing dependency license checker. Optional Registry Docker probes remain opt-in; the preview never starts a Docker daemon. See [validation and scope](docs/release/validation.md).

## References and release status

The execution design references [Chat On Steroids at 6d2b98a](https://github.com/totec448-spec/chat-on-steroids/tree/6d2b98a61b2125a4bbfff744cc47dd81ea0bedc9/src/main). The Python implementation does not port its Electron/ChatGPT automation or account integration. [Provenance and credits](docs/release/v0.1-third-party-provenance.md) distinguish design references, actual upstream derivatives and dependency terms.

Release: **v0.1.0-architecture-preview**, published as an experimental source preview under GPL-3.0-only. [Release notes](docs/release/v0.1.0-architecture-preview.md) · [GitHub release](https://github.com/Matt-lemon/rio/releases/tag/v0.1.0-architecture-preview). This release does not approve deployment or physical-device control.
