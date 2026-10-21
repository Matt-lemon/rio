#!/usr/bin/env bash
# Check committed source in a disposable fresh clone. No deployment.
set -euo pipefail
umask 077
source_root=$(git rev-parse --show-toplevel)
preview_tmp=$(mktemp -d)
trap 'rm -rf "$preview_tmp"' EXIT
mode=${1:---candidate}
case "$mode" in --candidate|--release) ;; *) exit 2 ;; esac
git -c core.hooksPath=/dev/null clone --quiet --no-local --no-tags --single-branch \
  --branch "$(git branch --show-current)" "$source_root" "$preview_tmp/source"
cd "$preview_tmp/source"
git rev-parse HEAD
export HOME="$preview_tmp/home" PYTHONDONTWRITEBYTECODE=1 CI=true
export RIO_HOME_EXECUTION_ENABLED=0 RIO_HOME_BACKUP_EXECUTION_ENABLED=false
export RIO_ST_CLI_CONTROL_ENABLED=0 RIO_WEB_SEARCH_ENABLED=false
mkdir -p "$HOME"
python3 -m venv "$preview_tmp/core"
py="$preview_tmp/core/bin/python"
"$py" -m pip --disable-pip-version-check install -q -r requirements-preview.lock
"$py" -m pip check
# Explicit permission fixtures need the normal mask; the outer directory stays 0700.
umask 022
"$py" tools/ci/check_syntax.py
"$py" tools/ci/run_tests.py
"$py" tools/ci/run_tests.py --suite browser
python3 -m venv "$preview_tmp/tools"
tpy="$preview_tmp/tools/bin/python"
"$tpy" -m pip --disable-pip-version-check install -q -r services/rio-tools/requirements-preview.lock
"$tpy" -m pip check
"$tpy" tools/ci/run_tests.py --suite tools
"$py" examples/offline_demo.py
# Publisher checksum pinned; scanner exists only in the temporary tree.
test "$(uname -s)/$(uname -m)" = Linux/x86_64
curl --fail --silent --show-error --location \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  -o "$preview_tmp/gitleaks.tar.gz"
printf '%s  %s\n' 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb \
  "$preview_tmp/gitleaks.tar.gz" | sha256sum --check --status
tar -xzf "$preview_tmp/gitleaks.tar.gz" -C "$preview_tmp" gitleaks
"$preview_tmp/gitleaks" git . --log-opts='--all --full-history' \
  --redact --ignore-gitleaks-allow --no-banner
git log --all --format=fuller | "$preview_tmp/gitleaks" stdin --redact --ignore-gitleaks-allow --no-banner
"$preview_tmp/gitleaks" dir . --redact --ignore-gitleaks-allow --no-banner
"$py" tools/ci/license_guard.py
"$tpy" tools/ci/license_guard.py --deps-only
if [ "$mode" = --release ]; then
  "$py" tools/ci/public_guard.py --release
else
  "$py" tools/ci/public_guard.py
  echo 'CANDIDATE_TECHNICAL_CHECKS_PASS; external publication requires owner approval.'
fi
