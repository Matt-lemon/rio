# Validation record — v0.1 Architecture Preview

This is the historical 2026-10-21 release record. The later optional environment
extension has its own [validation record](reproducible-renewal-validation.md);
its checks do not retroactively change these release counts or integration claims.

Reference environment: Linux x86_64, Python 3.12. This freezes selected existing
implementation, not all later operational fixes. New runtime production code:
**0 lines**. The 17 existing `core/execution/*.py` modules and the dependency
license checker are byte-for-byte unchanged from the reviewed implementation.

## Executed checks — 2026-10-21

The GPL-3.0-only release sequence completed on source revision
`220810b2d5d8bc4f8011514267f9439612217a10`. Final report/scope changes are
documentation; the release wrapper additionally unsets the opt-in Docker image
variable so the preview never inherits permission to run integration containers.
The local candidate tag identifies the final checked snapshot.

| Check | Actual result |
| --- | --- |
| Fresh clone and separate locked dependency installs | Passed; no existing NAS environment reused |
| In-memory Python compilation | 141 files passed |
| Existing complete root suite | 1125 passed; 24 subtests passed |
| Existing browser contract suite | 50 passed |
| Existing Registry suite | 63 passed; 7 Docker probes explicitly skipped |
| Real-Core offline status + fresh-process same-ID lookup | Passed; lookup made no new dispatch |
| Existing SIGKILL/no-resend regression | Included and passed in root suite |
| Gitleaks 8.30.1 | 72 nonempty diff commits, metadata and working tree: no rule findings |
| Reachable-object privacy check | 363 blobs at verified revision; no rule findings |
| Unchanged Python dependency license checker | Core 46 / Registry 11 distributions passed; source artifacts passed |
| Privacy pattern fixtures | 25 manual positive/negative assertions passed |
| Project license fixtures | Canonical text passed; missing text, wrong SPDX and tampered text rejected |
| Canonical owner-selected GPL-3.0-only release check | Passed |

Total existing tests: **1238 passed**, **24 subtests**, **7 explicit skips**.
No failed tests were removed or deselected. Full private logs and original hash
mapping remain outside this public tree. No GitHub Actions run or deployment is
claimed; this is an actual local execution of the same CI entry point.

An initial clone check returned 1124 pass / 1 fail because umask 077 changed an
explicit 0755 permission fixture. The wrapper now uses a normal mask inside a
private 0700 parent; production code and that test were unchanged. A later
privacy check mistook a relative core/home module path in the scope CSV for a
personal absolute path. Boundary tests were added to the manual audit and the
checker was corrected; actual absolute personal paths remain rejected. Both
failures were corrected and full checks passed. Before owner selection, missing
root licensing correctly blocked release mode; that blocker is now resolved.

## Reproduction and limits

`bash tools/ci/release-preview.sh --release` executes fresh clone → dependency
install → existing root/browser/Registry tests → offline demo → history/metadata/
working-tree secret scans → unchanged license checker → privacy/project-license
checks. Downloaded Gitleaks is fixed at 8.30.1 with the publisher checksum pinned.
Network is needed for dependency/scanner downloads; unit tests and demo use local
mocks and temporary private state. The wrapper disables home/CLI execution and
unsets the Docker integration opt-in. It does not start services or devices.

There are **186** source/documentation files, including the project license.
The original 434-file scope has **270** exclusions; additions are release
documentation, locks, checks and one offline example. New verification helpers
and example total **179 lines**, below the 500-line stop rule.

Seven Registry Docker tests remain available independently; skipped is not
passed. Full dubbing, screen, localization and Android components are excluded,
so their separate suites are not represented as passing. Windows, real
browser/model/voice, physical devices, Docker isolation and account deployment
are outside this preview validation. No dependency wheel, model, browser binary,
container image, private DB or media is redistributed. Metadata/scanner success
is not proof of all possible privacy or rights issues.

**Status: local GPL-3.0-only technical release gate passed. The owner approved
source-preview publication on 2026-10-21.** The publication step additionally
checks the GitHub-hosted CI run; a local PASS alone is not reported as a remote
CI result. The public Actions page records that remote result.
