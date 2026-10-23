# Reproducible Core environment — validation

This extension starts at public main `cf50718`; it preserves the v0.1 runtime.
The root GPL-3.0-only license, project-license selection, existing provenance
notices, Core/Gate/Memory/Execution modules and production entry points are
unchanged. The additions are documentation, a Nix definition/lock, optional CI,
and an exact-version dependency metadata review plus its negative regression.

## Executed checks — 2026-10-24

- Official Nix 2.34.9 linux/amd64 container, digest pinned in the workflow.
- Nixpkgs revision/content hash generated in `flake.lock` and validation prevented
  lock updates. Minimal environment: Python 3.12.14.
- Existing execution/core/safety/IPC subset: **68 passed**. Includes actual mock
  process death, same-ID recovery and no second dispatch. No test was removed.
- Existing read-only Core demo and fresh-process same-ID lookup: passed.
- Unchanged dependency checker: **24 distributions passed**. Initial ambiguous
  PLY metadata rejection was resolved by exact-version notice review, not a
  generic BSD allowlist change.

Additional offline/fresh-clone/full release results are recorded below only
once actually executed; the 2026-10-21 results are historical and not new passes.
Remote GitHub Actions and operating-service activation are not claimed here.

## Corrected failures

The initial runtime source filter omitted files used by the existing revision
fingerprint and mock dubbing-client tests. They are now explicitly admitted;
private configuration/state remain excluded. No runtime implementation changed.

The longer temporary paths of the container's non-nested-sandbox Nix build hit
Linux's Unix-socket path limit in the existing reconnect test. The Nix check now
sets a short pytest base directory under its private build TMPDIR. The same test
and five-second bound remain unchanged. The corrected subset passed all 68 tests.

## Boundaries

Containers have bounded resources, no host HOME, credentials, Docker/service
socket or device mounts. The source is read-only. Nested Nix sandboxing is disabled
only inside this disposable container; native Nix sandbox isolation was not
validated on the NAS. No Nix daemon/profile, host OS, operating Core, home gateway,
model, appliance or music service was installed, restarted or upgraded.

Full-host reproduction, actual service rollback and production database restore
are **not implemented or tested**. Native drivers, remote services and model/voice
assets remain outside this environment. A locked local environment does not prove
external API reliability, physical action success or byte-identical builds on
all hosts.
