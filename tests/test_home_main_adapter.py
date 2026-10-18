import json
from unittest.mock import Mock
import pytest

from core.home.main_adapter import handle_home_command, registry_from_environment
from core.home.tools import parse_light_request, CommandLedger, LocalHomeTransport


@pytest.mark.parametrize('text', ['거실 불 켜줘', '리오야 주방 전등 꺼주세요', '/home 거실 off'])
def test_google_home_primary_only_no_backup_dispatch(text):
    r=Mock();r.targets={'거실':{},'주방':{}};r.call.return_value={'status':'UNKNOWN','request_id':'request01'}
    reply=handle_home_command(text,lambda:r)
    assert '중복 실행하지' in reply
    r.call.assert_called_once()
    assert r.call.call_args.args[0]=='home_set_light'
    r.close.assert_called_once()


def test_no_registered_light_never_calls_control():
    r=Mock();r.targets={}
    assert '등록된 조명이 없어' in handle_home_command('거실 불 켜줘',lambda:r)
    r.call.assert_not_called()


@pytest.mark.parametrize('text', ['컴퓨터 꺼줘','거실 불 켜줘?','거실 불 켜지 마','거실 불 켜줘라고 말해','거실 불 켜고 컴퓨터 꺼줘'])
def test_ambiguous_requests_do_not_create_registry(text):
    factory=Mock()
    assert handle_home_command(text,factory) is None
    factory.assert_not_called()


def test_status_hides_remote_secrets_and_does_not_control():
    r=Mock();r.call.return_value={'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':False,'token':'secret'}
    reply=handle_home_command('/home status',lambda:r)
    assert '잠겨' in reply and 'secret' not in reply
    r.call.assert_called_once_with('home_get_status',{})


def test_failure_never_automatically_falls_back():
    def fail():raise RuntimeError('secret')
    reply=handle_home_command('거실 불 켜줘',fail)
    assert '자동 재전송하지' in reply and 'secret' not in reply


def test_private_targets_and_preview_default(tmp_path,monkeypatch):
    path=tmp_path/'targets.json';path.write_text(json.dumps({'schema_version':1,'targets':{'거실':{'alias':'dev_'+'a'*24,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True}}}));path.chmod(0o600)
    monkeypatch.setenv('RIO_HOME_TARGET_FILE',str(path));monkeypatch.delenv('RIO_HOME_EXECUTION_ENABLED',raising=False)
    monkeypatch.setenv('RIO_HOME_LEDGER',str(tmp_path/'ledger/commands.sqlite3'))
    monkeypatch.setenv('RIO_HOME_POLICY_FILE',str(tmp_path/'missing-policy'))
    r=registry_from_environment()
    assert not r.allow_execution and '거실' in r.targets
    r.close()
    path.chmod(0o644)
    import os
    if os.name=='posix':
        with pytest.raises(Exception):registry_from_environment()


def test_home_ledger_rejects_symlink_and_preserves_original(tmp_path):
    source=tmp_path/'original';source.write_bytes(b'original-bytes');source.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(source)
    with pytest.raises(OSError):CommandLedger(link)
    assert source.read_bytes()==b'original-bytes'


def test_light_chat_collects_terminal_failure_without_repeating_power(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from core.home.tools import HomeToolRegistry
    alias='dev_'+'a'*24
    targets={'거실':{'alias':alias,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True}}
    t=Mock()
    t.catalog.return_value={'bridge':{'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':True},
        'devices':[{'alias':alias,'lighting':True,'commands':{'on':True},
                    'control_binding':{'status':'SELECTED_EXPLICIT_TYPE','device_type':'OnOffPluginUnitDevice'}}]}
    t.set_power.return_value={'status':'DISPATCHED','id':'request01'}
    t.result.return_value={'status':'UNKNOWN','result':{'code':'SDK_EXECUTION_UNCERTAIN',
        'sdk_error_code':19,'sdk_unwrapped_error_code':5}}
    monkeypatch.setattr('core.home.tools.uuid.uuid4',lambda:SimpleNamespace(hex='request01'))
    r=HomeToolRegistry(t,targets,tmp_path/'private/ledger.sqlite',allow_execution=True)
    reply=handle_home_command('거실 불 켜줘',lambda:r)
    assert 'NOT_FOUND, 5' in reply and '중복 실행하지' in reply
    t.set_power.assert_called_once_with(alias,True,'request01')
    t.result.assert_called_once_with('request01')


def test_transport_invalid_path_never_reads_secret(tmp_path):
    transport=LocalHomeTransport(tmp_path/'missing-token')
    assert transport.request('/v1/status/../../commands')['code']=='GATEWAY_LOCAL_OR_RESPONSE_INVALID'


def test_private_policy_only_allows_approved_registered_aliases(tmp_path,monkeypatch):
    targets={'주방':{'alias':'dev_'+'a'*24,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True},
             '거실':{'alias':'dev_'+'b'*24,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True}}
    cfg=tmp_path/'targets.json';cfg.write_text(json.dumps({'schema_version':1,'targets':targets}));cfg.chmod(0o600)
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'schema_version':1,'enabled':True,'allowed_aliases':[targets['주방']['alias']]}));policy.chmod(0o600)
    monkeypatch.setenv('RIO_HOME_TARGET_FILE',str(cfg));monkeypatch.setenv('RIO_HOME_POLICY_FILE',str(policy))
    monkeypatch.delenv('RIO_HOME_EXECUTION_ENABLED',raising=False)
    r=registry_from_environment();assert r.allow_execution and list(r.targets)==['주방'];r.close()
    monkeypatch.setenv('RIO_HOME_EXECUTION_ENABLED','false')
    r=registry_from_environment();assert not r.allow_execution;r.close()
    monkeypatch.delenv('RIO_HOME_EXECUTION_ENABLED')
    policy.write_text(json.dumps({'schema_version':1,'enabled':True,'allowed_aliases':['dev_'+'c'*24]}))
    with pytest.raises(ValueError):registry_from_environment()
    policy.write_text(json.dumps({'schema_version':1,'enabled':'true','allowed_aliases':[]}))
    with pytest.raises(ValueError):registry_from_environment()
