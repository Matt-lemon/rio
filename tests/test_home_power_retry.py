import copy

import pytest

from core.home.tools import CommandLedger


PAYLOAD={'operation':'device_command','alias':'dev_'+'a'*24,
         'arguments':{'device_type':'OnOffPluginUnitDevice','command':'on'}}
READY={'bridge_state':'READY','available':True,'permission':'GRANTED','inflight':False}


@pytest.fixture
def ledger(tmp_path):
    tmp_path.chmod(0o700)
    value=CommandLedger(tmp_path/'commands.sqlite')
    value.reserve('previous01',PAYLOAD)
    value.record('previous01',{'status':'UNKNOWN','request_id':'previous01','sdk_error_code':19})
    yield value
    value.close()


def authorize(ledger,**overrides):
    args=dict(prior_key='previous01',next_key='explicit01',payload=PAYLOAD,
              note='사용자 직접 지시: 다시 켜봐',gateway_result={'status':'UNKNOWN'},gateway_status=READY)
    args.update(overrides)
    ledger.authorize_power_retry(**args)


def test_human_retry_only_permits_exact_new_request_without_rewriting_unknown(ledger):
    before=ledger.db.execute('SELECT * FROM light_commands').fetchone()
    authorize(ledger)
    assert ledger.reserve('otherkey01',PAYLOAD)['code']=='PREVIOUS_RESULT_UNRESOLVED'
    changed=copy.deepcopy(PAYLOAD);changed['arguments']['command']='off'
    assert ledger.reserve('explicit01',changed)['code']=='PREVIOUS_RESULT_UNRESOLVED'
    assert ledger.reserve('explicit01',PAYLOAD) is None
    assert ledger.db.execute('SELECT * FROM light_commands WHERE key=?',('previous01',)).fetchone()==before
    assert ledger.db.execute('SELECT count(*) FROM human_command_reviews').fetchone()[0]==0
    ledger.record('explicit01',{'status':'UNKNOWN','request_id':'explicit01'})
    assert ledger.reserve('automatic01',PAYLOAD)['code']=='PREVIOUS_RESULT_UNRESOLVED'
    assert ledger.reserve('explicit01',PAYLOAD)['status']=='UNKNOWN'


@pytest.mark.parametrize('status',[
    {**READY,'inflight':True},{**READY,'available':False},
    {**READY,'bridge_state':'STALE'},{**READY,'permission':'REVOKED'},
])
def test_ongoing_or_unready_gateway_cannot_be_authorized(ledger,status):
    with pytest.raises(ValueError):authorize(ledger,gateway_status=status)
    assert ledger.db.execute('SELECT count(*) FROM human_retry_authorizations').fetchone()[0]==0


@pytest.mark.parametrize('result',[{'status':'DISPATCHED'},{'status':'SUCCEEDED'},
                                {'status':'UNKNOWN','http_status':404},
                                {'status':'UNKNOWN','id':'different01'}])
def test_only_correlated_terminal_unknown_can_be_authorized(ledger,result):
    with pytest.raises(ValueError):authorize(ledger,gateway_result=result)


@pytest.mark.parametrize('command',['off','toggle','pause',True,[]])
def test_authorization_never_expands_prior_action(ledger,command):
    payload=copy.deepcopy(PAYLOAD);payload['arguments']['command']=command
    with pytest.raises(ValueError):authorize(ledger,payload=payload)


def test_another_device_or_prior_result_cannot_be_used(ledger):
    payload=copy.deepcopy(PAYLOAD);payload['alias']='dev_'+'b'*24
    with pytest.raises(ValueError):authorize(ledger,payload=payload)
    with pytest.raises(ValueError):authorize(ledger,next_key='previous01')
    # A new confirmed request is not an UNKNOWN retry candidate. The original
    # unknown stays immutable even if a later SDK result arrives for it.
    authorize(ledger,next_key='confirmed01')
    ledger.reserve('confirmed01',PAYLOAD)
    ledger.record('confirmed01',{'status':'SUCCEEDED'})
    with pytest.raises(ValueError):authorize(ledger,prior_key='confirmed01',next_key='explicit02')
    assert ledger.db.execute("SELECT status FROM light_commands WHERE key='previous01'").fetchone()[0]=='UNKNOWN'


def test_new_unknowns_still_need_a_new_direct_human_authorization(ledger):
    authorize(ledger);ledger.reserve('explicit01',PAYLOAD)
    ledger.record('explicit01',{'status':'UNKNOWN'})
    authorize(ledger,next_key='explicit02')
    assert ledger.reserve('explicit02',PAYLOAD)['code']=='PREVIOUS_RESULT_UNRESOLVED'
    authorize(ledger,prior_key='explicit01',next_key='explicit02')
    assert ledger.reserve('explicit02',PAYLOAD) is None


def test_confirmed_later_power_state_supersedes_but_does_not_rewrite_unknown(ledger):
    authorize(ledger);ledger.reserve('explicit01',PAYLOAD)
    ledger.record('explicit01',{'status':'SUCCEEDED'})
    ledger.review_power_superseded('previous01','explicit01',PAYLOAD,'직접 켜짐 확인',
        gateway_result={'status':'SUCCEEDED'},gateway_status=READY)
    assert ledger.db.execute("SELECT status FROM light_commands WHERE key='previous01'").fetchone()[0]=='UNKNOWN'
    assert ledger.db.execute('SELECT observation FROM human_command_reviews').fetchone()[0]=='SUPERSEDED_BY_HUMAN_CONFIRMED_POWER'
    off=copy.deepcopy(PAYLOAD);off['arguments']['command']='off'
    assert ledger.reserve('turnoff01',off) is None


@pytest.mark.parametrize('failure',['unknown_result','other_payload','older_success','active'])
def test_supersession_needs_latest_exact_success_and_idle_gateway(ledger,failure):
    authorize(ledger);ledger.reserve('explicit01',PAYLOAD)
    ledger.record('explicit01',{'status':'SUCCEEDED'})
    payload=copy.deepcopy(PAYLOAD);result={'status':'SUCCEEDED'};status=dict(READY)
    if failure=='unknown_result':result['status']='UNKNOWN'
    if failure=='other_payload':payload['arguments']['command']='off'
    if failure=='older_success':
        authorize(ledger,next_key='newerkey01');ledger.reserve('newerkey01',PAYLOAD)
    if failure=='active':status['inflight']=True
    with pytest.raises(ValueError):
        ledger.review_power_superseded('previous01','explicit01',payload,'켜짐 확인',gateway_result=result,gateway_status=status)
    assert ledger.db.execute('SELECT count(*) FROM human_command_reviews').fetchone()[0]==0
