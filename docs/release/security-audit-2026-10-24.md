# Security audit — merged PR #1 and public main

Audit target: `164d3d087dd1da1b1871fdd7d709bd855ece4d6f` (2026-10-24).
PR #1 introduced optional packaging/CI/documentation; runtime code remains the
published preview. This is a source/configuration audit, safe mock reproduction,
and dependency-advisory review, **not a penetration test of the operating NAS**.
No operating service, credentials, browser, device or policy was changed.

## Verdict

**Open findings: two medium-priority defects and two low-priority supply-chain
hardening gaps. Security acceptance remains pending.** Functional CI and license
checks did not cover these findings. The offline preview is not evidence of an
exploitable online deployment; exploit preconditions are recorded below.

## S1 — Medium: pinned Nix environment includes advisory-affected packages

Location: `flake.nix:11`, `flake.lock`, and
`docs/release/audit/nix-dependency-license-inventory.json:4` / `:18` / `:166`.
Introduced with the optional Nix environment in PR #1.

OSV querybatch checked 63 distinct public PyPI name/version pairs from the two
pip lockfiles and Nix inventory. Six unique advisories matched three Nix packages:

| Nix package | Advisory | Impact / fixed upstream version |
| --- | --- | --- |
| AnyIO 4.13.0 | [GHSA-5p39-cfhj-2xmp](https://github.com/agronholm/anyio/security/advisories/GHSA-5p39-cfhj-2xmp) | Process-pool stderr deadlock; 4.14.2 |
| AnyIO 4.13.0 | [GHSA-82r6-8w77-94w6](https://github.com/agronholm/anyio/security/advisories/GHSA-82r6-8w77-94w6) | IDNA TLS-name confusion under redirected connections; 4.14.2 |
| certifi 2026.1.4 | [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) | emSign CA trust issue enabling on-path TLS impersonation; PyPI fix 2026.10.19 |
| urllib3 2.7.0 | [GHSA-8988-9cw3-xx77](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77) | HTTPS proxy TLS-policy mixing; 2.8.0 |
| urllib3 2.7.0 | [GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g) | Chunked-deflate streaming CPU loop; 2.8.0 |
| urllib3 2.7.0 | [GHSA-vxq7-64xx-v4gw](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw) | Unbounded chunk-size buffering; 2.8.0 |

PYSEC aliases were deduplicated; this is six advisories, not twelve. No advisory
matched the versions in either pip preview lockfile in this query. That does not
prove absence of undisclosed vulnerabilities or cover native/container packages.

Pinned Nixpkgs source review found upstream AnyIO/urllib3 versions without
backported patches in their package recipes. Certifi needs special treatment:
Nix replaces its bundled PEM with `cacert`, so metadata alone is insufficient.
The pinned `cacert` recipe selects NSS 3.126. Its fetched certdata source exactly
matched the recipe SHA256 and still marks `emSign ECC Root CA - G3` as
`CKT_NSS_TRUSTED_DELEGATOR` for TLS server authentication. This confirms the
problematic trust input at source level; a generated Nix bundle/TLS exploit was
not tested in this audit. `NIX_SSL_CERT_FILE` overrides can change actual trust.

References at pinned Nixpkgs revision `da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36`:
[AnyIO recipe](https://github.com/NixOS/nixpkgs/blob/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36/pkgs/development/python-modules/anyio/default.nix),
[urllib3 recipe](https://github.com/NixOS/nixpkgs/blob/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36/pkgs/development/python-modules/urllib3/default.nix),
[certifi substitution](https://github.com/NixOS/nixpkgs/blob/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36/pkgs/development/python-modules/certifi/default.nix),
[cacert recipe](https://github.com/NixOS/nixpkgs/blob/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36/pkgs/by-name/ca/cacert/package.nix),
[NSS input](https://github.com/nss-dev/nss/blob/NSS_3_126_RTM/lib/ckfw/builtins/certdata.txt).

The shipped demo/checks are offline, and application reachability of each API
above was not established. Do not label these as successful attacks on Rio.
Network use of the dev shell creates different exposure and should wait for
reviewed dependency/CA updates. Update the pin or scoped package overrides,
verify the effective CA bundle, regenerate inventory, rerun tests/licenses and
add advisory scanning. Editing inventory versions alone is not remediation.

## S2 — Medium: browser URL parser disagreement bypasses public-only filtering

Location: `core/browser_url.py:34-55`; navigation boundary
`services/rio-browser-research/rio_browser_research/service.py:168-174`.
Pre-existing in the published runtime; PR #1 did not change this code.

`ipaddress.ip_address()` rejects noncanonical IPv4 spellings and the exception
branch accepts them as hostnames. WHATWG URL parsers normalize them differently:

| Accepted input | WHATWG destination |
| --- | --- |
| `https://127.1/`, `https://0x7f.1/`, `https://0177.0.0.1/` | `https://127.0.0.1/` |
| `https://10.1/` | `https://10.0.0.1/` |
| `https://192.168.1/` | `https://192.168.0.1/` |
| `https://%31%32%37.0.0.1/` | `https://127.0.0.1/` |
| `https://accounts%2egoogle.com/`, Unicode-dot variant | Denied `accounts.google.com` host |

Offline reproduction:

```sh
python tools/audit/security-audit-20261024.py
```

Node's WHATWG URL implementation performs canonicalization without opening any
URL. A mocked MCP boundary confirms `page.goto("https://127.1/")` is generated
before the final normalized URL is rejected. **Zero actual navigation/network
requests were made.** A late rejection does not prevent a request that already
crossed the navigation boundary. Public-to-private redirects have the same
post-navigation-check limitation.

An attacker-controlled result URL or an explicit URL request is needed to reach
this path. Actual access depends on browser/TLS/network protections; no internal
endpoint read, state change, cookie leak or browser exploit was demonstrated.
See [WHATWG IPv4 parsing](https://url.spec.whatwg.org/#ipv4-parser).

Remediation: validate browser-equivalent canonical hosts before dispatch, reject
ambiguous numeric/encoded host forms, and add negative regressions for these
cases. Validate redirects before following them. If a complete private-network
boundary is required, enforce DNS/connection destinations at the browser's
network/egress boundary; a string predicate alone does not provide that sandbox.

## S3 — Low: Actions references remain mutable

Locations: `.github/workflows/nix-preview.yml:10`, `ci.yml:10,14`.
The new workflow inherits `actions/checkout@v4`; the prior workflow also uses
`actions/setup-python@v5`. Docker/Nix inputs are pinned, these action tags are not.
Upstream tag compromise/movement could change code running on the CI host.
No compromise was detected. Pin reviewed full commit SHAs and retain version
comments/update checks. [Official GitHub guidance](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions).

## S4 — Low: pip version pins lack artifact hash enforcement

Locations: both `requirements-preview.lock` files and
`tools/ci/release-preview.sh:21,30`. Pre-existing before PR #1.
Exact versions are present, but there are no wheel/sdist hashes or
`--require-hashes` enforcement. The gate authenticates the version selection,
not a previously reviewed artifact. No altered artifact was found. Add reviewed
artifact hashes and hash-mode installs; review whether source builds are needed.
This is independent of Nix's existing content hashes.

## Checks actually executed

- Fresh Python 3.12 venv using the existing Core pip lockfile: **151 passed**
  across execution/Core/safety/IPC/isolation/adapters/context/license tests.
- Browser scope suite: **50 passed**. Together **201 existing tests passed**;
  their success does not cover the eight new URL counterexamples above.
- Offline parser/mock-navigation reproduction: all eight inputs accepted;
  generated navigation precedes rejection. This is reproduction, not a PASS gate.
- Gitleaks: reachable history, commit metadata and checkout; **zero findings**.
- Public guard: **389 blobs**, no findings; canonical GPL selection intact.
- Source-license guard: passed; runtime production paths unchanged by PR #1.
- OSV API returned all **63/63** name/version queries; six unique advisories.
- Pinned Nix recipes/NSS source examined; NSS bytes matched recipe hash.

An initial exploratory run under system Python 3.14 lacked `python-dotenv`:
141 passed, 8 failed, 2 setup errors. Its failures are preserved separately.
The fresh locked Python 3.12 run above passed without editing/removing tests.
No live browser, physical device, administrator install or operating database
was used. No new remote CI was triggered; no change was pushed or deployed.

## Verified boundaries and remaining limits

Read-only CI token, no `pull_request_target`, checkout credentials not retained;
Nix image digest and Nixpkgs content hash pinned. The disposable container drops
capabilities, bounds CPU/RAM/PIDs and mounts no host credentials/Docker socket.
Nested Nix sandbox is disabled as documented. It permits download egress and is
not a VM/hard boundary against all host-kernel vulnerabilities.

Core socket owner/peer checks, private DB validation, payload/revision checks,
fencing, same-ID reconciliation, model explicit-flag rejection and UNKNOWN
preservation have relevant passing regressions. This does not prove global
exactly-once behavior, prompt-injection immunity or independent authentication
of a human request against a fully compromised authorized Core/operator UID.
Gate remains a stateless evidence classifier, not authorization; its example
network binding should not be exposed as a trusted public security endpoint.

Excluded: operating NAS/firewall/Android deployment audit, native transitive
SBOM/CVE inventory, browser/OS/kernel exploits, full repository formal analysis,
live TLS interception or denial-of-service attempts. **The operating V20 bridge
and private runtime were not inspected or certified by this public-source audit.**
