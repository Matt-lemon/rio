"""Search dependency/provider failures stay read-only, explicit and non-replayed."""
from unittest.mock import Mock

import pytest

from core.execution.adapters import Adapters
from core.execution.catalog import Catalog
from core.execution.runner import ToolRunner
from core.execution.store import Store
from core.execution.types import spec, TEXT
from core.research_browser import BrowserConversation
from web_search import SearchResult


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize('error,code', [
    (ModuleNotFoundError("No module named 'ddgs'"), 'SEARCH_DEPENDENCY_UNAVAILABLE'),
    (ImportError('broken dependency'), 'SEARCH_DEPENDENCY_UNAVAILABLE'),
    (RuntimeError('provider token=PRIVATE_VALUE'), 'SEARCH_PROVIDER_UNAVAILABLE'),
])
def test_safe_diagnostic_without_raw_exception_or_invented_evidence(state, monkeypatch, error, code):
    search = Mock(side_effect=error)
    monkeypatch.setattr('web_search.web_search', search)
    result = Adapters(state).call('web.search', {'query': 'term meaning'}, 'search_failure_123', None)
    assert result.status == 'UNAVAILABLE' and result.code == code
    assert result.confirmation == 'NONE' and not result.completed
    assert result.evidence == {'results': [], 'content_read': False}
    assert 'PRIVATE_VALUE' not in str(result.public())
    search.assert_called_once()


def test_no_results_is_not_success(state, monkeypatch):
    monkeypatch.setattr('web_search.web_search', Mock(return_value=[]))
    result = Adapters(state).call('web.search', {'query': 'empty'}, 'empty_search_123', None)
    assert result.code == 'SEARCH_NO_RESULTS' and result.status == 'UNAVAILABLE'


def test_success_retains_snippet_and_source_without_claiming_body_read(state, monkeypatch):
    row = SearchResult('Source', 'https://example.org/definition', 'Source excerpt')
    monkeypatch.setattr('web_search.web_search', Mock(return_value=[row]))
    result = Adapters(state).call('web.search', {'query': 'term'}, 'good_search_123', None)
    assert result.status == 'OBSERVED' and result.confirmation == 'SEARCH_SNIPPETS'
    assert result.evidence['content_read'] is False
    assert result.evidence['results'][0]['url'] == row.url


def test_same_id_failure_query_and_repeat_do_not_dispatch_again(state, monkeypatch):
    search = Mock(side_effect=ModuleNotFoundError('ddgs'))
    monkeypatch.setattr('web_search.web_search', search)
    session = 'a' * 32
    state.session(session)
    original = state.create_task(session, '/search definition')
    state.activate_executor()
    task = state.claim_task(original['id'])
    catalog = Catalog(provider=lambda: {'web.search': spec('web.search', 'Search snippets', 'read', query=TEXT)},
                      registry_provider=lambda: [])
    runner = ToolRunner(state, catalog, Adapters(state))
    tool = catalog.describe('web.search')
    args = {'query': 'definition'}
    first = runner.call(task, 'web.search', args, 'same_search_123', tool['revision'], BrowserConversation())
    again = runner.call(task, 'web.search', args, 'same_search_123', tool['revision'], BrowserConversation())
    queried = runner.status('same_search_123', session)
    assert first.public() == again.public() == queried.public()
    assert first.status == 'UNAVAILABLE'
    search.assert_called_once()
