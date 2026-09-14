import pytest

from forge.codex_gateway import gateway_token_for
from forge.codex_task import CodexTaskError
from forge.config import settings


@pytest.fixture
def gateway_settings():
    fields = ('opencodex_gateway_url', 'opencodex_api_auth_token')
    previous = {field: getattr(settings, field) for field in fields}

    def configure(url='http://gateway.internal:8877/v1', token=' gateway-secret '):
        object.__setattr__(settings, 'opencodex_gateway_url', url)
        object.__setattr__(settings, 'opencodex_api_auth_token', token)

    configure()
    try:
        yield configure
    finally:
        for field, value in previous.items():
            object.__setattr__(settings, field, value)


def test_unconfigured_gateway_does_not_attach_token(gateway_settings):
    gateway_settings(url='')
    assert gateway_token_for('http://gateway.internal:8877/v1') is None


@pytest.mark.parametrize('configured_url,target_url', [
    ('http://gateway.internal:8877/v1', 'http://gateway.internal:8877/v1'),
    ('http://gateway.internal:8877/v1/', 'http://gateway.internal:8877/v1'),
    ('http://gateway.internal:8877/v1', 'http://gateway.internal:8877/v1/'),
])
def test_matching_gateway_returns_stripped_token(gateway_settings, configured_url, target_url):
    gateway_settings(url=configured_url)
    assert gateway_token_for(target_url) == 'gateway-secret'


@pytest.mark.parametrize('target_url', [
    'https://gateway.internal:8877/v1',
    'http://gateway.internal:8878/v1',
    'http://gateway.internal/v1',
    'http://gateway.internal:8877/v2',
    'http://gateway.internal:8877/v1/other',
    'http://sub.gateway.internal:8877/v1',
    'http://gateway.internal.attacker.invalid:8877/v1',
    'http://gateway.internal:8877/v1?key=value',
    'http://gateway.internal:8877/v1#fragment',
    'http://user:password@gateway.internal:8877/v1',
    'http://gateway.internal:8877@attacker.invalid/v1',
])
def test_other_destinations_do_not_receive_gateway_token(gateway_settings, target_url):
    assert gateway_token_for(target_url) is None


@pytest.mark.parametrize('token', ['', '  \t\n'])
def test_matching_gateway_requires_token(gateway_settings, token):
    gateway_settings(token=token)
    with pytest.raises(CodexTaskError):
        gateway_token_for('http://gateway.internal:8877/v1')


def test_missing_gateway_token_does_not_block_direct_provider(gateway_settings):
    gateway_settings(token='')
    assert gateway_token_for('https://provider.example/v1') is None
