"""Root upgrade orchestration in private temp paths; no privileged operations."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def install(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/'tools/rio-runtime/nas/rio-core-accounts.py'
    spec=importlib.util.spec_from_file_location('upgrade_candidate',path)
    app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
    state=tmp_path/'root-state';state.mkdir();units=tmp_path/'units';units.mkdir()
    backups=tmp_path/'backups';backups.mkdir();policy_path=tmp_path/'peers.json'
    old=tmp_path/'old-release';old.mkdir();new=tmp_path/'new-release';new.mkdir()
    op=SimpleNamespace(pw_uid=1000,pw_gid=1000,pw_dir=str(tmp_path/'operator'))
    core=SimpleNamespace(pw_uid=991,pw_gid=990,pw_dir='/var/lib/rio-core',pw_shell='/usr/sbin/nologin')
    monkeypatch.setattr(app,'ROOT_STATE',state);monkeypatch.setattr(app,'SYSTEM_UNITS',units)
    monkeypatch.setattr(app,'BACKUPS',backups);monkeypatch.setattr(app,'POLICY',policy_path)
    monkeypatch.setattr(app.pwd,'getpwnam',lambda name:core if name==app.CORE else op)
    monkeypatch.setattr(app.grp,'getgrnam',lambda name:SimpleNamespace(gr_gid=990 if name==app.CORE else 115))
    monkeypatch.setattr(app.os,'getgrouplist',lambda *args:[990])
    monkeypatch.setattr(app.os,'chown',lambda *args:None)
    marker={'status':'ISOLATED_CORE_ACTIVE','core_uid':991,'operator_uid':1000,
            'commit':'a'*40,'release':str(old),'backup':str(tmp_path/'initial-migration'),'user_core_enabled':True}
    (state/'current.json').write_text(json.dumps(marker))
    policy={'version':1,'core_uid':991,'operator_uid':1000,'release':str(old),
            'operator_project':str(app.PROJECT),'core_socket':app.CORE_SOCKET,'bridge_socket':app.BRIDGE_SOCKET}
    policy_path.write_text(json.dumps(policy))
    for name in (app.CORE_UNIT,app.BRIDGE_UNIT):(units/name).write_text('old '+name)
    monkeypatch.setattr(app,'read_root_file',lambda path:Path(path).read_bytes())
    def write(path,data,mode=0o644):Path(path).write_bytes(data);Path(path).chmod(mode)
    monkeypatch.setattr(app,'root_file',write)
    monkeypatch.setattr(app,'root_directory',lambda path,mode=0o755:Path(path))
    idle=Mock();monkeypatch.setattr(app,'idle_check',idle)
    release=Mock(return_value=new);monkeypatch.setattr(app,'source_release',release)
    account=Mock(return_value={'uid':991,'docker_file_access':False,'docker_connect':'DENIED','docker_api_requests':0})
    monkeypatch.setattr(app,'inspect_core_account',account)
    dbs=[]
    for name in ('live-core.sqlite3','live-bridge.sqlite3'):
        db=tmp_path/name;c=sqlite3.connect(db);c.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,state TEXT)')
        c.execute("INSERT INTO calls VALUES('old','OBSERVED')");c.commit();c.close();db.chmod(0o600);dbs.append(db)
    snapshot=app.snapshot_database;snapshots=[]
    def backup(source,target,uid,gid):
        db=dbs[0] if str(source).startswith('/var/lib/rio-core') else dbs[1]
        snapshots.append((db,Path(target)));snapshot(db,target,os.getuid(),os.getgid())
    monkeypatch.setattr(app,'snapshot_database',backup)
    system=Mock();monkeypatch.setattr(app,'system_unit',system)
    report=Mock();monkeypatch.setattr(app,'save_operator_report',report)
    context={'identity':'same','memory':'same','model':'same'}
    def operator(code,*,release=None,data=None):
        if 'load_policy' in code:return SimpleNamespace(stdout=json.dumps(policy).encode())
        if code==app.SMOKE:
            for db in dbs:
                c=sqlite3.connect(db);c.execute("INSERT INTO calls VALUES('latest','UNKNOWN')");c.commit();c.close()
            if env.fail_smoke:raise RuntimeError('SMOKE_FAILED_AFTER_RESULT_RESERVATION')
            return SimpleNamespace(stdout=json.dumps({'health':{'process_uid':991,'isolation_audit':{'operator_context':context}}}).encode())
        return SimpleNamespace(stdout=json.dumps(context).encode())
    monkeypatch.setattr(app,'operator_python',operator)
    env=SimpleNamespace(app=app,state=state,units=units,policy=policy_path,old=old,new=new,
        marker=marker,dbs=dbs,idle=idle,release=release,system=system,report=report,
        snapshots=snapshots,account=account,fail_smoke=False)
    return env


def rows(path):
    c=sqlite3.connect(path)
    try:return c.execute('SELECT * FROM calls ORDER BY id').fetchall()
    finally:c.close()


def test_upgrade_preserves_accounts_history_and_original_legacy_backup(install):
    x=install;x.app.upgrade('b'*40)
    marker=json.loads((x.state/'current.json').read_text())
    assert marker['status']=='ISOLATED_CORE_ACTIVE' and marker['commit']=='b'*40
    assert marker['backup']==x.marker['backup'] and marker['last_upgrade']['status']=='APPLIED'
    assert json.loads(x.policy.read_text())['release']==str(x.new)
    assert all(rows(db)==[('latest','UNKNOWN'),('old','OBSERVED')] for db in x.dbs)
    assert all(rows(target)==[('old','OBSERVED')] for _,target in x.snapshots)
    assert all(target not in x.dbs for _,target in x.snapshots)
    assert x.idle.call_count==2
    x.system.assert_any_call('stop',x.app.CORE_UNIT,x.app.BRIDGE_UNIT)


def test_failed_upgrade_restores_code_but_keeps_latest_unknown_records(install):
    x=install;x.fail_smoke=True
    with pytest.raises(RuntimeError,match='SMOKE_FAILED'):x.app.upgrade('b'*40)
    marker=json.loads((x.state/'current.json').read_text())
    assert marker['commit']=='a'*40 and marker['status']=='ISOLATED_CORE_ACTIVE'
    assert marker['last_upgrade']['status']=='PREVIOUS_RELEASE_RESTORED_LATEST_HISTORY_PRESERVED'
    assert json.loads(x.policy.read_text())['release']==str(x.old)
    assert (x.units/x.app.CORE_UNIT).read_text()=='old '+x.app.CORE_UNIT
    assert all(rows(db)==[('latest','UNKNOWN'),('old','OBSERVED')] for db in x.dbs)
    assert len(x.snapshots)==2


def test_failed_recovery_is_recorded_without_automatic_retry(install):
    x=install;x.fail_smoke=True;starts=[]
    def system(*args):
        if args[0]=='start':
            starts.append(args)
            if len(starts)>1:raise OSError('RECOVERY_FAILED')
    x.system.side_effect=system
    with pytest.raises(OSError,match='RECOVERY_FAILED'):x.app.upgrade('b'*40)
    marker=json.loads((x.state/'current.json').read_text())
    assert marker['status']=='UPGRADE_RECOVERY_REQUIRED'
    assert marker['last_upgrade']['status']=='RECOVERY_REQUIRED_NO_AUTOMATIC_RETRY'
    assert len(starts)==2 and all(('latest','UNKNOWN') in rows(db) for db in x.dbs)


def test_active_client_blocks_upgrade_before_any_release_or_service_change(install):
    x=install;x.idle.side_effect=ValueError('CLOSE_CORE_CLIENT_FIRST')
    with pytest.raises(ValueError,match='CLOSE_CORE_CLIENT_FIRST'):x.app.upgrade('b'*40)
    x.release.assert_not_called();x.system.assert_not_called();assert not x.snapshots


def test_work_appearing_during_preparation_blocks_before_service_stop(install):
    x=install;x.idle.side_effect=[None,ValueError('WAIT_FOR_ACTIVE_TASKS')]
    with pytest.raises(ValueError,match='WAIT_FOR_ACTIVE_TASKS'):x.app.upgrade('b'*40)
    x.system.assert_not_called();assert not x.snapshots
    assert json.loads((x.state/'current.json').read_text())['commit']=='a'*40


def test_installed_policy_disagreement_blocks_before_release_creation(install):
    x=install;(x.state/'current.json').write_text(json.dumps({**x.marker,'release':'/different/release'}))
    with pytest.raises(ValueError,match='INSTALLED_POLICY_CHANGED'):x.app.upgrade('b'*40)
    x.release.assert_not_called();x.system.assert_not_called()


def test_host_docker_denial_failure_prevents_stopping_services(install):
    x=install;x.account.side_effect=ValueError('ACCOUNT_DOCKER_DENIAL_NOT_PROVEN')
    with pytest.raises(ValueError,match='ACCOUNT_DOCKER_DENIAL_NOT_PROVEN'):x.app.upgrade('b'*40)
    x.system.assert_not_called();assert not x.snapshots


@pytest.mark.parametrize('change',[{'status':'UPGRADE_RECOVERY_REQUIRED'},{'commit':'b'*40}])
def test_nonactive_and_same_commit_upgrade_do_not_change_services(install,change):
    x=install;(x.state/'current.json').write_text(json.dumps({**x.marker,**change}))
    with pytest.raises(ValueError):x.app.upgrade('b'*40)
    x.release.assert_not_called();x.system.assert_not_called()


def test_changed_account_group_rejects_upgrade(install,monkeypatch):
    x=install;monkeypatch.setattr(x.app.os,'getgrouplist',lambda *args:[990,115])
    with pytest.raises(ValueError,match='CORE_ACCOUNT_BOUNDARY_CHANGED'):x.app.upgrade('b'*40)
    x.release.assert_not_called();x.system.assert_not_called()


def test_nonroot_upgrade_never_attempts_privileged_operation(install,monkeypatch):
    import sys
    x=install;monkeypatch.setattr(x.app.os,'getuid',lambda:1000)
    monkeypatch.setattr(sys,'argv',['rio-core-accounts.py','--upgrade','--commit','b'*40])
    with pytest.raises(SystemExit,match='관리자 인증'):x.app.main()
    x.system.assert_not_called();x.release.assert_not_called()


def test_recovery_idle_check_uses_latest_isolated_database(install,monkeypatch):
    x=install;(x.state/'current.json').write_text(json.dumps({**x.marker,'status':'UPGRADE_RECOVERY_REQUIRED'}))
    # Use the actual guard, rather than the fixture's orchestration stand-in.
    path=Path(__file__).resolve().parents[1]/'tools/rio-runtime/nas/rio-core-accounts.py'
    spec=importlib.util.spec_from_file_location('guard_candidate',path)
    app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
    monkeypatch.setattr(app,'ROOT_STATE',x.state)
    original_path=app.Path
    monkeypatch.setattr(app,'Path',lambda value:SimpleNamespace(iterdir=lambda:iter(())) if value=='/proc' else original_path(value))
    connection=Mock()
    connection.execute.side_effect=lambda sql:SimpleNamespace(fetchone=lambda:(1 if 'IN_FLIGHT' in sql else 0,))
    connect=Mock(return_value=connection);monkeypatch.setattr(app.sqlite3,'connect',connect)
    with pytest.raises(ValueError,match='WAIT_FOR_DISPATCH'):app.idle_check()
    assert '/var/lib/rio-core/core.sqlite3?mode=ro' in connect.call_args.args[0]
    connection.close.assert_called_once()
