# Contributing to Rio

Start with [the development philosophy](docs/philosophy.md),
[actual architecture](docs/architecture.md) and [security boundaries](docs/security-model.md).
Prefer a small, reviewable change to an existing module. The model proposes;
application contracts and operator policy authorize execution.

## Before submitting

- Describe the concrete problem and the resulting behavior. Distinguish an
  implementation goal from something that was actually tested.
- Preserve identity/memory/model boundaries, original results and uncertain
  request IDs. Do not introduce automatic replay of a possibly sent action.
- Pin new build inputs and document how to reproduce the relevant check.
  Environment changes must not imply account, service or device permission.
- Keep credentials, personal settings, memory/state databases, logs, model
  weights and voice assets outside the source tree and Nix derivations.
- Retain copyright/license notices. Document actual upstream code separately
  from design references. New dependencies require license review; keep the
  existing license gate enabled.
- Run the relevant tests and the existing release gate. The optional Nix checks
  supplement that gate; they do not replace full regression/privacy review.
- State rollback and data-compatibility limits for deployment-affecting changes.
  Never fix a test run by deleting a failing test or clearing an UNKNOWN record.

Use [the isolated environment](docs/reproducibility.md) for the Core preview.
Real browser, model, audio, account isolation and device integration checks must
remain explicitly scoped. Default checks must not operate appliances or start
unrelated services.

These are contribution/review guidelines, not additional restrictions on the
rights granted by [GPL-3.0-only](LICENSE). Third-party terms and the existing
[provenance records](docs/release/v0.1-third-party-provenance.md) remain in force.
