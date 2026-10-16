import fcntl
import sys
import pytest
from core.tools.contracts import ToolError
from rio_tools.registry import Registry
from rio_tools.service import main

IMAGE='sha256:'+'a'*64

def state(tmp_path):
    root=tmp_path/'state';registry=Registry(root)
    registry.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
        ('request_001','runtime','hash','package','RUNNING','{}',None,0,0))
    registry.close();return root

def test_admin_listing_does_not_apply_crash_recovery_to_live_jobs(tmp_path,monkeypatch):
    root=state(tmp_path)
    monkeypatch.setattr(sys,'argv',['rio-tools','--state',str(root),'--image-id',IMAGE,'list'])
    main()
    registry=Registry(root)
    assert registry.db.execute('SELECT status FROM jobs').fetchone()[0]=='RUNNING'
    registry.close()

def test_second_server_is_rejected_before_any_recovery(tmp_path,monkeypatch):
    root=state(tmp_path)
    with (root/'service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        monkeypatch.setattr(sys,'argv',['rio-tools','--state',str(root),'--image-id',IMAGE,'serve'])
        with pytest.raises(ToolError,match='SERVICE_ALREADY_RUNNING'):main()
    registry=Registry(root)
    assert registry.db.execute('SELECT status FROM jobs').fetchone()[0]=='RUNNING'
    registry.close()

def test_package_directory_symlink_cannot_write_outside_state(tmp_path):
    root=tmp_path/'state';root.mkdir(mode=0o700)
    outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    (root/'packages').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ToolError,match='PACKAGE_PATH_FORBIDDEN'):Registry(root)
    assert list(outside.iterdir())==[]
