# Security remediation completion — 2026-10-24

Scope: finish the four findings against public main `164d3d0`, continuing the
prepared fixes at `b936062`. Tested implementation: `c5365c2`.
The [original audit](security-audit-2026-10-24.md) and
[earlier HOLD](security-review-remediation-2026-10-24.md) remain historical records.

**Result: S1–S4 are remediated and verified within this source-preview scope.**
This is not certification of the operating NAS, a full browser network sandbox,
native dependency closure, external models or physical-device execution.
No operating service, private configuration/database, credential or device was
changed. The executor/planner was not redesigned. Nothing was pushed or deployed.

## Disposition

| Finding | Final behavior / evidence |
| --- | --- |
| S1: advisory-affected Nix packages | Locked Nixpkgs `670a258a0d0c227a30dc267343af7881f640b6e6`; actual AnyIO 4.15.1, certifi 2026.10.19, urllib3 2.8.0; rebuilt inventory/license check; effective CA check and OSV gate pass. |
| S2: URL canonicalization / late rejection | Original eight counterexamples are rejected before MCP. Requests use `route.fetch(maxRedirects:0,maxRetries:0)` and reject HTTP redirects before the next hop. The per-page guard remains installed for delayed requests; only GET/HEAD are permitted. |
| S3: mutable Actions tags | Existing prepared checkout/setup-python full SHA pins retained and regression checked. No new Actions dependency. |
| S4: unhashed pip artifacts | Existing prepared Python 3.12/3.14 Linux x86_64 wheel hashes and `--require-hashes --only-binary=:all:` retained. Fresh installation passed; a deliberately modified wheel was rejected. |

The release gate now queries the public lockfiles and actual Nix inventory at
the fixed OSV API endpoint. Missing, incomplete or unavailable responses fail
closed. The live query covered **55 name/version pairs with zero advisories**.
It does not discover installed/private packages or send source/configuration.
This is a point-in-time Python advisory check, not proof of vulnerability absence.

## Nix hold resolved without removing tests or increasing capabilities

- Cargo created `/homeless-shelter/.cargo` in the single-user container, breaking
  the following derivation's purity check. Its cache now lives under that build's
  private TMPDIR. Nix purity checks remain enabled.
- Curio's `test_errors` expected binding port 80 without permission to fail.
  Docker's default unprivileged-port setting made it start a server instead.
  A diagnostic abort captured the blocked stack; that failed run was preserved.
  The container now sets `net.ipv4.ip_unprivileged_port_start=1024` with **all
  capabilities still dropped**. The upstream test then completed normally.
- certifi, sphinx-rtd-theme, typeguard, IPython and httpbin source archives carried
  publisher ownership IDs. Scoped unpack overrides retain file contents without
  trying to restore those IDs. No CHOWN capability or privileged container added.
- A further development-shell check caught an inherited older CA file even
  after the clean derivation passed. The shell/checks explicitly select the
  reviewed CA file/directory for certifi, stdlib TLS and Requests. This check is
  now part of `tools/ci/nix-preview.sh`, rather than a manual-only check.

No Rio test was deleted/skipped to obtain a pass; upstream dependency checks
remain as configured by the pinned Nixpkgs recipes. Nix builds were bounded to
2 CPUs / 4 GiB / 256 PIDs. Host Nix was not installed and no system profile changed.

Four Nix checks passed: **68 existing execution/safety/IPC tests**, the same-ID
read-only recovery demo, **24-distribution** license review, and actual security
input checks. After disconnecting the container network, all four were rebuilt
with `--offline --rebuild`; the CLI/demo/development-shell verification also
passed offline. The 68 tests overlap the full suite below.

Both certifi and stdlib TLS loaded **169 trusted CAs**, excluding the affected
emSign root fingerprint. Bundle SHA256:
`81ffe9877138a9e9bee56e067479a5abd59c65cdbb310d830b13108e1afe4e33`.
Custom trust overrides are outside this tested configuration.

## Real browser fixture

The prepared mock test did not model a Playwright limitation: redirect requests
can bypass a second `page.route` handler call. The final implementation uses
zero-hop fetching instead of `route.continue()` and keeps the guard after page
load. A missing routing API fails with `BROWSER_ROUTE_GUARD_UNAVAILABLE`.
References: [page.route](https://playwright.dev/docs/api/class-page#page-route),
[route.fetch](https://playwright.dev/docs/api/class-route#route-fetch).

Executed a fresh **headless Chromium 156.0.8078.4** fixture with the installed
**Playwright 1.64.0-alpha-1790635538000** driver. The fixture browser came from
official image `mcr.microsoft.com/playwright:v1.64.0-noble`, amd64 manifest
`sha256:bc72a8df40831ded821e3cbc79c318c21b44d6e51dff9fd89ace7a8249c5b462`;
its explicit executable path was selected because its browser revision differs
from the installed alpha driver's default revision. This records that exact
combination, not an assertion about all Edge/MCP versions.

The container had **network none**, loopback-only synthetic HTTPS, dropped
capabilities, 1 CPU / 2 GiB and no user profile/login or host credentials.
`public.example` resolved only to this fixture's own loopback. A one-use fixture
certificate and `ignoreHTTPSErrors` were confined to this lab; production TLS
verification was not disabled. The harness requires an explicit lab flag and
rejects non-loopback network interfaces before starting.

- Old continue-based control: synthetic private endpoint reached **once**.
- Fixed implementation: private/redirect destinations reached **zero times**.
- Normal GET, delayed public GET and gzip HTML rendered correctly.
- Delayed private requests, POST and private/public HTTP redirects were blocked.

Harness: `tools/audit/browser-route-integration.cjs`. It consumes JSON containing
the current `guarded_navigation()` bodies for `normal`, `gzip`, `redirect`, and
`redirect-public` at `https://public.example/<case>`. Run only in a disposable
network-none container with `RIO_ISOLATED_BROWSER_LAB=1`, the declared Playwright
driver, and that image's explicit `RIO_TEST_BROWSER_EXECUTABLE`. Never attach a
real profile or run this fixture server on the NAS network.

No existing Edge tab, Playwright extension approval, Google session or live MCP
connection was used. DNS rebinding, Service Workers and popup traffic still need
an independent egress boundary before adversarial browsing on a sensitive LAN.
HTTP redirects are intentionally rejected, including otherwise public ones;
the caller must choose a verified canonical URL instead of automatic following.

## Final fresh-clone release gate

`bash tools/ci/release-preview.sh --release` from committed source `c5365c2`:

- **1,157 root + 50 browser + 63 Registry = 1,270 passed**; 24 subtests;
  7 explicitly optional Docker integration tests skipped.
- Syntax compilation: **147 files**; read-only demo and same-ID reopening passed.
- Hashed wheel-only fresh installs, dependency checks and negative tamper test passed.
- Gitleaks history/metadata/checkout: no findings; public guard: **423 blobs**,
  no findings. Source/Core/Registry license checks passed; GPL-3.0-only intact.
- OSV public version gate: **55/55 responses**, zero advisories.

Raw failed attempts and successful logs are retained privately outside source.
This record does not replace them or claim new remote GitHub Actions runs.
Broader deployment/backup/physical-adapter exercises listed in the earlier
technical review remain separate work, not unresolved code changes hidden by CI.
