#!/usr/bin/env bash
# Optional Nix checks; never installs Nix or activates a service/profile.
set -euo pipefail
source_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
mode=()
if [ "${1:-}" = --offline ]; then mode=(--offline); shift; fi
if [ "$#" -ne 0 ]; then echo 'Usage: nix-preview.sh [--offline]' >&2; exit 2; fi
command -v nix >/dev/null
nix --extra-experimental-features 'nix-command flakes' \
  flake check --no-update-lock-file "${mode[@]}" "$source_root"
nix --extra-experimental-features 'nix-command flakes' \
  run --no-update-lock-file "${mode[@]}" "$source_root"
nix --extra-experimental-features 'nix-command flakes' \
  develop --no-update-lock-file "${mode[@]}" "$source_root" \
  --command python "$source_root/tools/ci/nix_security.py"
