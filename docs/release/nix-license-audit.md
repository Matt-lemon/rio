# Nix environment extension: license and provenance

Review date: 2026-10-24. This supplements the existing
[license decision](license-decision.md) and [provenance](v0.1-third-party-provenance.md).

## Project rights remain unchanged

The root canonical GPL-3.0-only text, `.github/project-license.json`, original
copyright/third-party notices and Python license-checker implementation are
unchanged. New Rio-written packaging and documentation fall under the same
project license. The philosophy and contribution guidelines are engineering
review criteria, not added restrictions on modification or redistribution.

No Nix implementation, package-manager binary, model, voice asset, system image
or third-party source is copied into this repository. `flake.nix` is a new Rio
build declaration. The article about reproducible operations is a design
inspiration, not a source-code port:
https://entropicthoughts.com/nixos-is-making-me-less-afraid . Existing references,
including Chat On Steroids at 6d2b98a, retain their original scope and notices.

## External tools and package recipes

Nix 2.34.9 is a separately executed build tool; its LGPL-2.1-or-later terms
remain its own: https://github.com/NixOS/nix/blob/2.34.9/COPYING . The official
manual identifies that license: https://nix.dev/manual/nix/2.34/introduction.html .
Nixpkgs package recipes at the pinned revision have their own MIT notice:
https://github.com/NixOS/nixpkgs/blob/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36/COPYING .
That does not make the individual packaged programs MIT, nor relicense Rio.

CI references the official `nixos/nix:2.34.9` linux/amd64 image by digest
`sha256:f9823365cb4dbd4f1c056957d94e415b5bae73dfee33cf4da84c98a2f4ce9577`.
The image is fetched for disposable tests; this source change does not republish
it or clear redistribution of its complete native closure.

## Python dependency review

The existing checker passes the 24 Python distributions in the new minimal Nix
environment. [Inventory](audit/nix-dependency-license-inventory.json) records the
actual versions/licenses; it is separate from the full pip preview inventory.
The allowlist is not expanded and the checker implementation is not weakened.

The initial Nix check correctly rejected `ply==3.11` because metadata only said
`BSD`. The installed `ply/lex.py` contains source/binary notice preservation,
non-endorsement and warranty clauses corresponding to BSD-3-Clause. Its complete
file SHA256 is `6da6d12129c801fcc7a3b5aa2d8176a86092687d1bb4cf1ddf3b601da2b7480d`.
The reviewed upstream version is https://github.com/dabeaz/ply/tree/3.11 .
An exact-version reviewed exception records that finding. Other versions or
unreviewed packages with ambiguous BSD metadata still fail the regression test.
No PLY code or notice was removed or relicensed; PLY is downloaded separately as
an environment dependency. Its own notices remain applicable.

## Limits

The original inventory and references above describe the initial Nix preview
before the 2026-10-24 security follow-up. Its security pin and scoped Python
overrides are documented in
[security review remediation](security-review-remediation-2026-10-24.md).
Do not treat the original dependency version inventory as a current security
assertion after the follow-up.

Python metadata checks do not inspect every native dependency, attest all source
ownership or constitute a full binary SBOM. No portable binary bundle/image is
being published here. Before distributing a built closure, separately review
its complete transitive source/license/notice and corresponding-source duties.
No new proprietary model/GPU/browser/voice dependency is approved by this audit.
