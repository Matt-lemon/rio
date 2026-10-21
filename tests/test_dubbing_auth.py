import pytest
from rio_dubbing_auth import dubbing_token


def test_token_file_is_shared_configuration_without_changing_env_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'secret'
    path.write_text('synthetic-file-token\n')
    monkeypatch.setenv('RIO_DUBBING_TOKEN_FILE', str(path))
    monkeypatch.delenv('RIO_DUBBING_TOKEN', raising=False)
    assert dubbing_token() == 'synthetic-file-token'
    monkeypatch.setenv('RIO_DUBBING_TOKEN', 'synthetic-env-token')
    assert dubbing_token() == 'synthetic-env-token'


@pytest.mark.parametrize('text', ['', 'two tokens', 'x' * 4097])
def test_invalid_token_file_fails_closed(tmp_path, monkeypatch, text):
    path = tmp_path / 'secret'
    path.write_text(text)
    monkeypatch.setenv('RIO_DUBBING_TOKEN_FILE', str(path))
    monkeypatch.delenv('RIO_DUBBING_TOKEN', raising=False)
    with pytest.raises(ValueError):
        dubbing_token()
