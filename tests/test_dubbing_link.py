from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from core.dubbing_link import Supervisor,classify_ssh,probe,save,private_root


class Backend:
    def __init__(self):
        self.peer=Mock(return_value=(False,'PEER_NO_ROUTE'))
        self.occupied=Mock(return_value=False)
        self.health=Mock(return_value=('WAITING_FOR_API','API_UNAVAILABLE'))
        self.child=SimpleNamespace(pid=123,poll=Mock(return_value=None),terminate=Mock(),wait=Mock())
        self.start=Mock(return_value=self.child)
        self.diagnostic=Mock(return_value='SSH_LINK_LOST')


def test_unreachable_peer_is_wait_not_restart_and_never_power_observation():
    b=Backend();now=[0];s=Supervisor(b,lambda:now[0]);assert s.tick()
    assert s.snapshot['state']=='WAITING_FOR_PEER' and s.snapshot['peer_power']=='NOT_OBSERVED'
    for _ in range(100):s.tick()
    b.peer.assert_called_once();b.start.assert_not_called()
    assert s.snapshot['retry_after_seconds']==60
    now[0]=60;s.tick();assert s.snapshot['retry_after_seconds']==120
    assert not s.snapshot['automatic_pc_start'] and not s.snapshot['automatic_api_start']


def test_live_ssh_with_absent_api_is_waiting_without_restarting_tunnel():
    b=Backend();b.peer.return_value=(True,'TCP_REACHABLE');now=[0];s=Supervisor(b,lambda:now[0]);s.tick()
    b.occupied.return_value=True;now[0]=1;s.tick()
    assert s.snapshot['state']=='WAITING_FOR_API'
    now[0]=31;b.health.return_value=('READY','API_READY');s.tick()
    assert s.snapshot['state']=='READY';b.start.assert_called_once();b.child.terminate.assert_not_called()


@pytest.mark.parametrize('reason',['SSH_AUTH_REJECTED','SSH_HOST_KEY_REJECTED','LOCAL_PORT_OCCUPIED'])
def test_configuration_errors_stop_automatic_retries(reason):
    b=Backend();b.peer.return_value=(True,'TCP_REACHABLE');now=[0];s=Supervisor(b,lambda:now[0]);s.tick()
    now[0]=1;b.child.poll.return_value=255;b.diagnostic.return_value=reason;s.tick()
    assert s.fatal and s.snapshot['state']=='CONFIGURATION_ERROR'
    now[0]=1000;assert not s.tick();b.start.assert_called_once()


def test_unknown_existing_listener_is_never_killed_or_replaced():
    b=Backend();b.peer.return_value=(True,'TCP_REACHABLE');b.occupied.return_value=True
    s=Supervisor(b);s.tick();assert s.fatal;b.start.assert_not_called();b.child.terminate.assert_not_called()


def test_link_death_backoff_and_only_own_child_shutdown():
    b=Backend();b.peer.return_value=(True,'TCP_REACHABLE');now=[0];s=Supervisor(b,lambda:now[0]);s.tick()
    b.child.poll.return_value=255;now[0]=1;s.tick()
    assert s.snapshot['state']=='WAITING_FOR_PEER' and s.snapshot['retry_after_seconds']==60
    now[0]=61;b.child.poll.return_value=None;s.tick();s.close();b.child.terminate.assert_called_once()


@pytest.mark.parametrize('text,expected',[('No route to host','PEER_NO_ROUTE'),('Permission denied (publickey)','SSH_AUTH_REJECTED'),('Host key verification failed','SSH_HOST_KEY_REJECTED'),('Address already in use','LOCAL_PORT_OCCUPIED'),('secret custom error','SSH_LINK_LOST')])
def test_only_public_diagnostic_categories_escape(text,expected):
    assert classify_ssh(255,text)==expected


def test_probe_only_reads_and_preserves_unhealthy_api_distinction():
    b=Backend();b.peer.return_value=(True,'TCP_REACHABLE');b.occupied.return_value=True;b.health.return_value=('API_UNHEALTHY','API_WORKER_NOT_READY')
    assert probe(b)['state']=='API_UNHEALTHY';b.start.assert_not_called()


def test_status_is_written_privately_and_atomically(tmp_path):
    root=private_root(tmp_path/'state');save(root,{'state':'WAITING_FOR_PEER'})
    assert (root/'status.json').stat().st_mode & 0o077==0
