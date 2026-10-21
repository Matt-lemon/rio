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
