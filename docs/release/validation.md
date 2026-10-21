# Validation scope

Reference: Linux x86_64, Python 3.12. The selected frozen implementation is the
existing integration snapshot, not a claim that every later operational fix is
included. New runtime production code: **0 lines**.

Before history normalization, all existing root tests passed: **1125 passed,
24 subtests passed**. The real-Core read-only/offline demonstration and existing
SIGKILL/no-resend test passed. Final normalized fresh-clone results are recorded
in the separate candidate validation record; these preliminary results are not
substituted for them.

The single CI sequence is `bash tools/ci/release-preview.sh --release`: fresh
clone, isolated dependency installation, existing root/browser/Registry suites,
real-Core offline demo, pinned Gitleaks history+working-tree scan, unchanged
license checker, and reachable-object privacy/root-license check. A missing root
LICENSE fails release mode. `--candidate` checks technical readiness while
explicitly retaining the publication blocker. No external CI run is claimed
until an actual CI result exists.

Seven Registry Docker probes remain opt-in, not removed. Full dubbing, screen,
localization and Android components are excluded from this source preview;
their separate tests are not represented as passing or silently deselected from
the included root suite. Windows, actual browser/model/voice, real-device actions,
Docker isolation and deployment/account separation are not validated here.

Dependency installation and downloading the pinned scanner require the network;
the demonstrations and unit tests use local mocks and temporary private state.
No device command, service restart, new credential or model generation is part
of this release gate. Scanner success is detection evidence, not proof that all
possible private information is absent. Manual scope/provenance review remains
required.

## Executed candidate results — 2026-10-21

Verified source revision: `5d6d8c0d5ff53ea689950569f6a1473f0d7f0087` (the following report-only commit
changes documentation and scope inventory, not runtime/test/checker source).

| Check | Actual result |
| --- | --- |
| Fresh clone and isolated dependency installs | Passed, locked Core and Registry environments |
| In-memory Python compilation | 141 files passed |
| Existing root suite, unchanged test membership | 1125 passed; 24 subtests passed |
| Browser contract suite | 50 passed |
| Registry suite | 63 passed; 7 Docker integration probes skipped |
| Real-Core offline status + fresh-process same-ID lookup | Passed; no new dispatch on lookup |
| Existing SIGKILL/no-resend regression | Included and passed in root suite |
| Gitleaks 8.30.1 | 69 nonempty diff commits, commit metadata and working tree: no rule findings |
| Reachable-object privacy check | 349 blobs at tested revision; no rule findings |
| Existing Python license checker | Core 46 / Registry 11 distributions passed; source artifacts passed |
| Privacy-pattern positive/negative fixtures | 17 manual assertions passed |
| Publication mode with no selected root LICENSE | Correctly failed closed |

Total existing tests: **1238 passed**, plus **24 subtests**, **7 explicit skips**.
No failed tests were deleted or deselected. An initial fresh-clone check exposed
an inherited umask mismatch in a permission fixture (1124 pass / 1 fail); the
release wrapper now uses the normal mask inside its private 0700 temporary parent.
Production code and the failing test were unchanged. A subsequent complete
fresh-clone run passed. Local raw logs remain private; no GitHub Actions run or
external deployment has been performed.

There are 183 included source/documentation files. The original 434-file scope
has 270 exclusions; additions are release docs/locks/checks/example only.
All 17 existing `core/execution/*.py` modules and `license_guard.py` are byte-for-byte
unchanged from the reviewed implementation. Existing deployment literals are
normalized, not newly generalized. New runtime production code is **0 lines**;
new verification helpers/example total **170 lines**, below the 500-line stop rule.

**Status: candidate technical checks passed; public release BLOCKED pending root
license selection and final owner approval.** Metadata PASS does not clear
embedded binary redistribution or unreviewed rights. The preview contains source,
not dependency wheels, models, browser binaries, images, operating state or media.
