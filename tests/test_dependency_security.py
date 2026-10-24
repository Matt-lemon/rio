"""Advisory gate fixtures never contact the network."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

source = Path(__file__).resolve().parents[1] / 'tools/ci/dependency_security.py'
spec = importlib.util.spec_from_file_location('dependency_security', source)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def response(value):
    return lambda *args, **kwargs: io.BytesIO(json.dumps(value).encode())


def test_gate_reports_an_advisory_without_treating_it_as_a_pass():
    result = guard.check([('example', '1.0')], response({'results': [
        {'vulns': [{'id': 'GHSA-fixture-only'}]}]}))
    assert result['status'] == 'FAIL' and result['matches'][0]['name'] == 'example'


@pytest.mark.parametrize('value', [{'results': []}, {'results': [None]},
                                  {'results': [{'error': 'unavailable'}]}])
def test_missing_or_malformed_results_fail_closed(value):
    with pytest.raises(ValueError):
        guard.check([('example', '1.0')], response(value))


def test_fixed_endpoint_and_only_public_inventory_are_queried():
    def opener(request, timeout):
        assert request.full_url == 'https://api.osv.dev/v1/querybatch' and timeout == 25
        assert json.loads(request.data) == {'queries': [
            {'package': {'name': 'example', 'ecosystem': 'PyPI'}, 'version': '1.0'}]}
        assert not request.has_header('Authorization')
        return io.BytesIO(b'{"results":[{}]}')
    assert guard.check([('example', '1.0')], opener)['status'] == 'PASS'
    pairs = guard.packages()
    assert ('anyio', '4.15.1') in pairs
    assert all('==' not in name and len(version) < 100 for name, version in pairs)


def test_network_failure_is_not_reported_as_pass():
    def unavailable(*args, **kwargs):
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        guard.check([('example', '1.0')], unavailable)
