# Security remediation and independent technical-review follow-up

Scope: disposable public-source worktree based on audit `e4f7b01`, after
GitHub PR #1 was merged at `164d3d0`. This is **not** a production NAS rollout
or a certification of the private V20 bridge or physical devices.

The original [security audit](security-audit-2026-10-24.md) remains unchanged
as a record of findings. This document records follow-up actions separately.

The HOLD below records the status at `b936062`. The later
[completion record](security-completion-2026-10-24.md) supersedes the Nix/test
hold for the four scoped audit findings, while retaining the deployment limits.

## Security audit follow-up

| Finding | Implemented mitigation | Verification / remaining limitation |
| --- | --- | --- |
| S1 / medium: Nix AnyIO, certifi/cacert, urllib3 | Pin Nixpkgs to immutable reviewed 2026-10-24 revision; use scoped, source-hash-pinned certifi 2026.10.19 and urllib3 2.8.0 overrides; require AnyIO >=4.14.2 and updated NSS cacert >=3.131 at Nix evaluation | Re-run Nix checks, capture effective versions and trust inputs. This does not prove every transitive/native component free of vulnerabilities. |
| S2 / medium: URL parser disagreement | Reject non-ASCII/encoded hostnames, numeric shorthands, and IP literals before dispatch; install a browser request route before navigation and abort redirects/nonpublic requests | Offline Python + Node mocked-Playwright regressions. Real browser routing availability and DNS rebinding/Service Worker egress require separate isolated integration review. The browser fails closed if routing is unavailable. |
| S3 / low: mutable Actions refs | Pin checkout v4.2.2 and setup-python v5.6.0 to verified full commit SHAs | Both CI YAML files use immutable references. |
| S4 / low: unhashed pip artifacts | Record SHA-256 of reviewed Python 3.12 and 3.14 Linux x86_64 wheels, require `--require-hashes --only-binary=:all:` in both release-gate environments | Offline reinstall using local wheelhouse and `pip check` passed on Python 3.12; the fresh-clone gate also checks Python 3.14. These hashes do not cover every OS/architecture. |

Security acceptance is **conditional**, not an assertion of a complete browser
SSRF sandbox. DNS resolution and network egress must be isolated/enforced before
allowing adversarial remote pages to interact with an internal network.

## Technical-review findings

The external technical review (2026-10-24, static public-source inspection)
identified P0 memory durability and device/UNKNOWN recovery exercises, plus
P1 Gate/model/operations boundaries. The review was not an exploitation report.

### P0 memory concurrent-write/durability remediation

- All `remember`/`forget` read-modify-write transactions take an adjacent
  `flock` lock on a stable inode, rather than locking the replaceable JSON.
- Each write uses its own private 0600 temporary file, flushes and `fsync`s it,
  atomically replaces the JSON and `fsync`s the parent directory.
- Multi-process concurrent inserts, corruption rejection, interrupted staging
  and a SIGKILL immediately before rename are exercised in tests.
- Scope: cooperating POSIX writers through this API on a local filesystem.
  This is **not** a power-loss, NFS locking, filesystem durability, backup or
  end-to-end database-restore proof. Existing JSON must remain backed up.

### Device/side-effect recovery

Existing `tests/test_execution_ipc.py`, `tests/test_execution_core.py` and
`tests/test_execution_safety.py` cover mock SIGKILL, UNKNOWN, fencing and
same-ID status reconciliation without duplicate dispatch. That is valuable
fault injection, but **not** physical-device confirmation or a distributed
exactly-once guarantee. Do not turn on a new physical adapter on the strength
of these tests; it needs a provider-observation/physical-state test matrix and
an operator-controlled release procedure.

### Follow-up required before broader deployment

1. **Browser:** run isolated Playwright integration with real redirects,
   service workers, DNS rebinding and network-policy tests; use an egress
   firewall/namespace for access to sensitive LAN resources. Do not test
   these against live NAS/device addresses.
2. **Memory:** rehearse backup/restoration and abnormal power removal on a
   disposable filesystem, not the operating personal memory store.
3. **Devices:** use a sandbox service emulator for command receipt/UNKNOWN and
   manual release, then individually authorize real-device testing.
4. **Gate/models:** separately collect false-allow/false-deny metrics and test
   three model adapters' response/error contracts. Caller evidence flags are
   not independent proof or execution authorization.
5. **Operations:** document distinct OS principals, secret-rotation and
   credential-redaction process before public deployment of a service.

No operating NAS service, real browser, credential, device, local memory DB,
or private source worktree was changed for this follow-up.

## Independent verification receipt (2026-10-24)

- Fresh-clone release candidate on the NAS's system Python 3.14: **1,149**
  root tests, **50** browser tests, **63** tool tests passed; **7** explicitly
  skipped Docker-integration tests and **24** separately reported subtests.
  Syntax, offline read-only demo, same-request-ID recovery (zero additional
  dispatches), history/metadata/worktree Gitleaks, source/dependency-license
  checker and 410-blob public guard passed. No live operations were performed.
- Python 3.12 Linux wheel hashes also passed local offline installation,
  `pip check`, and 133 focused existing/added tests. The 3.14 release gate
  verified the additional architecture-specific hashes.
- Original eight WHATWG parser mismatch cases are now all rejected before MCP
  navigation; Node-simulated route interception rejects a private redirect.
- OSV querybatch (AnyIO 4.15.1, certifi 2026.10.19, urllib3 2.8.0) returned
  zero known advisories for those three reviewed versions at the time of check.
  This is not an all-dependencies SBOM or a proof of vulnerability absence.
- Nix flake **evaluation and minimum-version assertions passed** with locked
  Nixpkgs rev `670a258a0d0c227a30dc267343af7881f640b6e6` and NSS cacert
  3.131.1. The first build needed a capability-safe certifi sdist unpack
  override. Further disposable, 2-CPU/4-GiB container build attempts reached
  upstream Sphinx/Curio test builds; a Cargo-cache purity error was diagnosed
  in an earlier attempt. The final indirect-dependency test was taking too long
  and was **stopped without a green Nix flake check** to protect NAS resources.
  No release-security PASS or Nix package closure attestation is claimed.

**Release decision: HOLD for full Nix validation, real browser egress scope
and real-device operating checks.** All source patches remain in a local
review branch; neither the public GitHub main nor the operating Rio was updated.
