"""Keep root and service tests away from the operator's private user state."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_user_home(monkeypatch, tmp_path):
    # DispatchGuard also reads secondary ledgers relative to Path.home().
    # A fresh Python venv alone does not isolate those paths on a live NAS.
    test_home = tmp_path / "user-home"
    test_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(test_home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(test_home))
