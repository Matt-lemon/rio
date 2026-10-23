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

- With the container network disconnected, `nix build --offline --rebuild`
  rebuilt all three checks: 68 tests, dependency-license check and demo passed.
  The checked outputs matched their prior builds. The documented offline check
  script and app also completed. This tests this closure after fetching its inputs,
  not arbitrary future rebuilds without a cache.
- A fresh public source archive in a second empty container executed the new
  GitHub job's recipe: all Nix checks and app passed. Its initial staging attempt
  failed because a capability-dropped container could not read the host-owned
  0700 temporary archive. The recipe now makes only that credential-free archive
  readable before mounting it read-only; no host HOME or credential permission changed.
- Existing full fresh-clone release gate: **1126 root + 50 browser + 63 Registry =
  1239 passed**, **24 subtests**, **7 explicit Docker skips**. The additional
  license-review negative test accounts for the +1 versus the historical release.
  The 68-test Nix subset overlaps this full suite and must not be added to it.
- Python syntax, history/metadata/working-tree Gitleaks scans and unchanged
  license checker passed: full pip Core 46 / Registry 11 distributions. The
  public history/path/project-license guard passed with no findings.

Remote GitHub Actions and operating-service activation are not claimed here.
The 2026-10-21 release record remains historical. Full raw test logs and the
local test environment receipt are kept outside the public source tree.

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
