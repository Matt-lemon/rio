# Reproducible operation: the first slice

The goal is to rebuild and verify Rio without reconstructing an operator's
installation history. The first slice packages the **existing offline Core
preview**; it does not migrate the NAS, activate services or rebuild the executor.
Read [the philosophy](philosophy.md) and [validation record](release/reproducible-renewal-validation.md).

## What is pinned

`flake.lock` records Nixpkgs revision
`670a258a0d0c227a30dc267343af7881f640b6e6` and its NAR content hash.
That revision selects Python 3.12 and the minimal Core/test dependencies, with
source-hash-pinned certifi/urllib3 security overrides. Cargo's cache is confined
to its build directory; upstream dependency tests remain enabled as configured
by Nixpkgs. Version floors and the effective CA bundle have an offline check.
The development shell explicitly selects the reviewed CA bundle for certifi,
stdlib TLS and Requests instead of inheriting an older invoking-shell bundle.
Custom trust overrides fall outside this validation.
The flake rejects unfree Nixpkgs packages. The default app invokes the existing
`examples/offline_demo.py`; no alternate Core implementation is supplied.

This is a separate closure from the broader `requirements-preview.lock`.
Versions need not match between those environments: both are recorded, but a
PASS in one is not a claim that every optional service works in the other.
`x86_64-linux` is the only declared and tested Nix platform in this change.

## Run and verify

With Nix already installed, from a clean checkout:

```sh
nix --extra-experimental-features 'nix-command flakes' run --no-update-lock-file .
bash tools/ci/nix-preview.sh
```

The first command runs `core.status` in temporary private state and then reopens
the task in another process with the same request ID, without another dispatch.
The check script runs the existing Core/safety/IPC tests, the same demo and the
unchanged dependency license checker. Failures stop the script. It never installs
Nix, changes a user profile, enables a service or deploys to another machine.

For editing the existing modules:

```sh
nix --extra-experimental-features 'nix-command flakes' develop --no-update-lock-file
```

Initial source/dependency downloads require network access. After populating the
same store, run `bash tools/ci/nix-preview.sh --offline`. The offline flag is not
a security boundary; the local validation also disconnected the disposable
container's network and rebuilt the checks. A lost or garbage-collected closure
may require downloads again. Keep reviewed build inputs/closures in a maintained
cache if long-term recovery is required.

Nix Flakes currently require the explicit experimental feature flags above.
They are enabled per command here; no system configuration is changed. Updating
an input is a deliberate maintainer change: review the new lock, dependency and
license inventories, execute checks, then commit both definition and lock.
Normal validation uses `--no-update-lock-file` and must not move the input pin.

The original full release gate remains:

```sh
bash tools/ci/release-preview.sh --release
```

Nix checks supplement its complete root/browser/Registry tests and history,
privacy and license scans; they do not replace them. The release gate also
queries OSV for the public lock/inventory versions and fails on advisories or
an incomplete/unavailable response. This network check is separate from the
offline Nix checks and does not scan the native/container closure.

## Sources, secrets and state

The runtime source filter admits existing Core Python files, files required by
its revision fingerprint and the selected tests/checks. It excludes operator
configuration, databases, models and voice assets. A source filter is not a
secret vault: Nix may snapshot a tracked checkout before filtering the runtime
package. Never commit credentials or pass secret values as derivation inputs.
Nix store content is not where private configuration belongs.

Keep three independently managed layers:

| Layer | Contents | Recovery rule |
| --- | --- | --- |
| Code/environment | Reviewed commit, lock, executable closure | Rebuild or select a retained reviewed version |
| Private configuration | Identity/persona, endpoints, credentials, device bindings | Supply outside the store; review changes and preserve permissions |
| Durable state | Memory, task/call IDs, results, UNKNOWN history | Consistent backup/migration and compatibility checks; never reset to make code pass |

A code rollback must not erase a command that might already have reached a
physical device. Before attaching an older runtime to an existing database,
verify schema and semantic compatibility and take a consistent backup. Restore
is a separate reviewed operation, not the inverse of switching an executable.

The original v0.1 tag predates the flake. Use its documented Python environment
when reproducing that tag. For later revisions with a flake, build from that
revision's matching lock in a separate checkout. No automatic activation/rollback
manager or operational database restoration is implemented by this change.

## Isolation and external dependencies

The Nix CI job uses the digest-pinned official Nix 2.34.9 container, with bounded
resources, dropped capabilities, no host HOME, device or Docker/service socket,
and only a read-only public source archive mounted. Its nested Nix build sandbox
is disabled because the container is the CI boundary. This is not a claim that
same-user Nix processes are a general-purpose security sandbox. Native Nix users
run the commands above without this container-only sandbox override.

The container explicitly keeps ports below 1024 privileged. Docker's default
unprivileged-port setting otherwise invalidates an upstream Curio negative test
and starts a server instead of raising the expected permission error. No upstream
test is removed to work around this; the capability-free environment is corrected.

Google/SmartThings services, Google Play updates, browser sessions, GPU drivers
and hardware/firmware remain external. Pinning local code cannot freeze them or
repair their backend errors. Adapter permission checks, outcome evidence and
bounded recovery remain necessary. Models/voice assets need their own provenance,
license, version and hash before any later packaging work.

## Next slices — not implemented

Service desired-state/observed-state comparison; operator-reviewed activation;
account/socket/resource declarations; backup/restore drills; Gate/voice/model
engine closures; GPU/driver compatibility; full host/NixOS provisioning. Add these
as independently reviewed increments, without using packaging as a reason to
redesign Core/Gate/Memory/Execution.

[License scope](release/nix-license-audit.md) · [Official pinning guide](https://nix.dev/tutorials/first-steps/towards-reproducibility-pinning-nixpkgs.html)
· [Nix container documentation](https://nix.dev/manual/nix/2.34/installation/installing-docker.html)
