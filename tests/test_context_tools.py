from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import context_tools as module
from test_unified_main import runtime


def browser():return SimpleNamespace(results=[],index=None,checked=0)


@pytest.fixture
def contracts(monkeypatch):
    tools=[module.spec('home.light','lights','control',target=module.enum('거실','탁상등'),action=module.enum('on','off')),
           module.spec('spotify.control','music','control',action=module.enum('play','pause','status')),
           module.spec('browser.search','search','navigate',query=module.TEXT),
           module.spec('browser.open_result','open','navigate',index={'type':'integer','minimum':1,'maximum':5}),
           module.spec('browser.back_results','back','navigate'),
           module.spec('home.device.에어컨','aircon','control',action=module.enum('on','off'))]
    monkeypatch.setattr(module,'catalog',lambda:{t['name']:t for t in tools})


def test_recent_user_turns_and_real_tool_outcomes_reach_selector(contracts):
    planner=Mock(return_value={'kind':'tool','name':'home.light','arguments':{'target':'거실','action':'off'}})
    router=module.ContextTools(planner=planner,clock=lambda:100)
    router.remember('home.light','거실 불 켜줘','완료',{'target':'거실','action':'on'})
    history=[{'role':'user','content':'거실 불 켜줘'},{'role':'assistant','content':'완료'}]
    choice=router.choose('그거 꺼줘',history,browser())
    assert choice['name']=='home.light'
    payload=planner.call_args.args[0]
    assert payload['recent_turns']==history
    assert payload['tool_results'][0]['arguments']['target']=='거실'


@pytest.mark.parametrize('decision',[
    {'kind':'tool','name':'pc.shutdown','arguments':{}},
    {'kind':'tool','name':'home.light','arguments':{'target':'컴퓨터','action':'off'}},
    {'kind':'tool','name':'home.light','arguments':{'target':'거실','action':'off','script':'x'}},
    {'kind':'tool','name':'browser.open_result','arguments':{'index':True}},
    {'kind':'tool','name':'browser.search','arguments':{'query':'Bearer SECRET'}},
])
def test_unknown_tools_and_invalid_arguments_never_become_executable(contracts,decision):
    router=module.ContextTools(planner=lambda _:decision)
    assert router.choose('그거 꺼줘',[],browser())['kind']!='tool'


@pytest.mark.parametrize('text',['거실이 어둡네','거실 불 끄면 어떻게 돼?','거실 불 꺼줘라고 말했어','거실 불 끄지 마','거실 불 꺼주지 말아줘','거실 불 끄지 말아주세요','거실 불 끄고 탁상등 켜줘','거실 불 꺼줘 그리고 음악 틀어줘'])
def test_observations_negation_quotes_and_compounds_do_not_authorize_changes(contracts,text):
    router=module.ContextTools(planner=lambda _:{'kind':'tool','name':'home.light','arguments':{'target':'거실','action':'off'}})
    assert router.choose(text,[],browser())['kind']=='clarify'


def test_unknown_or_expired_previous_result_cannot_ground_a_retry(contracts):
    now=[100]
    router=module.ContextTools(planner=lambda _:{'kind':'tool','name':'home.light','arguments':{'target':'거실','action':'off'}},clock=lambda:now[0])
    router.remember('home.light','거실 켜줘','결과를 확인하지 못했어',{'target':'거실','action':'on'})
    assert router.choose('그거 꺼줘',[],browser())['kind']=='clarify'
    router.events[-1]['result']='완료';now[0]=1000
    assert router.choose('그거 꺼줘',[],browser())['kind']=='clarify'


def test_cannot_substitute_power_for_unsupported_temperature(contracts):
    router=module.ContextTools(planner=lambda _:{'kind':'tool','name':'home.device.에어컨','arguments':{'action':'on'}})
    assert router.choose('에어컨 온도 24도로 켜줘',[],browser())['kind']=='clarify'


def test_browser_selection_requires_real_in_chat_results(contracts):
    router=module.ContextTools(planner=lambda _:{'kind':'tool','name':'browser.open_result','arguments':{'index':2}})
    state=browser()
    assert router.choose('두 번째 것도 열어줘',[],state)['kind']=='clarify'
    state.results=[{'index':2,'title':'실제 제목','url':'https://example.org'}];state.checked=router.clock()
    assert router.choose('두 번째 것도 열어줘',[],state)['name']=='browser.open_result'


def test_revalidate_changed_catalog_before_dispatch(contracts,monkeypatch):
    router=module.ContextTools()
    monkeypatch.setattr(module,'catalog',lambda:{})
    callback=Mock(side_effect=AssertionError('must not call'))
    reply=router.execute({'name':'home.light','arguments':{'target':'거실','action':'off'}},browser(),{'home':callback})
    assert '실행하지 않았어' in reply
    callback.assert_not_called()


def test_selection_stays_inside_existing_handler_contracts(contracts):
    router=module.ContextTools()
    home=Mock(return_value='legacy result, physically unverified')
    reply=router.execute({'name':'home.light','arguments':{'target':'탁상등','action':'off'}},browser(),{'home':home})
    home.assert_called_once_with('/home 탁상등 off')
    assert reply=='legacy result, physically unverified'


@pytest.mark.parametrize('followup',['그거 꺼줘','그 조명 꺼줘'])
def test_main_records_direct_tool_turn_then_uses_context_for_followup(runtime,monkeypatch,capsys,contracts,followup):
    monkeypatch.setattr(runtime,'RIO_CONTEXT_TOOLS_ENABLED',True)
    monkeypatch.setenv('RIO_MEMORY_ENABLED','0')
    monkeypatch.setattr(runtime,'print_status',lambda *a:None)
    planner=Mock(return_value={'kind':'tool','name':'home.light','arguments':{'target':'거실','action':'off'}})
    original=module.ContextTools
    monkeypatch.setattr(module,'ContextTools',lambda:original(planner=planner))
    home=Mock(side_effect=lambda text:'완료' if text in ('거실 불 켜줘','/home 거실 off') else None)
    monkeypatch.setattr(runtime,'handle_home_command',home)
    for name in ('call_rio_gate','stream_completion'):
        monkeypatch.setattr(runtime,name,lambda *a,**k:pytest.fail('unexpected chat call'))
    inputs=iter(['거실 불 켜줘',followup,'/exit'])
    monkeypatch.setattr(runtime,'read_user_input',lambda:next(inputs))
    runtime.main(['--text-only'])
    assert planner.call_count==1
    assert planner.call_args.args[0]['recent_turns'][0]['content']=='거실 불 켜줘'
    assert home.call_args_list[-1].args[0]=='/home 거실 off'
    assert followup not in [call.args[0] for call in home.call_args_list]
    assert capsys.readouterr().out.count('Rio: 완료')==2


def test_simple_conversation_cannot_implicitly_invoke_a_tool(contracts):
    planner=Mock(return_value={'kind':'chat'})
    router=module.ContextTools(planner=planner)
    assert router.choose('오늘도 수고했어',[],browser())=={'kind':'chat','planned':True}


def test_compact_context_does_not_send_link_query_values_to_model(contracts):
    planner=Mock(return_value={'kind':'chat'})
    router=module.ContextTools(planner=planner)
    b=browser();b.checked=router.clock();b.results=[{'index':1,'title':'확인된 제목','url':'https://example.org/a?private_tracking=VALUE'}]
    router.choose('그 기사 말이야',[{'role':'assistant','content':'기사: https://example.org/a?private_tracking=VALUE'}],b)
    payload=planner.call_args.args[0]
    assert payload['browser_results'][0]['host']=='example.org'
    assert 'VALUE' not in str(payload)


def test_model_timeout_never_becomes_claim_of_execution(contracts):
    import httpx
    planner=Mock(side_effect=httpx.ReadTimeout('bounded model timeout'))
    router=module.ContextTools(planner=planner)
    choice=router.choose('그거 열어줄래?',[],browser())
    assert choice['kind']=='clarify' and choice['planner_error']=='ReadTimeout'


def test_same_action_can_apply_to_explicit_second_light_only_with_real_context(contracts):
    router=module.ContextTools(planner=lambda _:{'kind':'tool','name':'home.light','arguments':{'target':'탁상등','action':'on'}})
    router.remember('home.light','거실 불 켜줘','완료',{'target':'거실','action':'on'})
    assert router.choose('탁상등도 그렇게 해줘',[],browser())['kind']=='tool'
    router.clear()
    assert router.choose('탁상등도 그렇게 해줘',[],browser())['kind']=='clarify'
