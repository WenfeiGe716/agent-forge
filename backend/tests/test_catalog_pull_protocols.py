"""A pull must revalidate upstream and never succeed with a partial catalog."""
import asyncio

import httpx
import pytest

from forge import providers


PROTOCOLS = ['openai_chat', 'openai_responses', 'anthropic_messages', 'google_gemini', 'custom_http']
KEY = 'synthetic-catalog-pull-key'


def connection(protocol):
    result = {'protocol': protocol, 'base_url': 'https://catalog.example/v1', 'external': True}
    if protocol == 'custom_http':
        result['protocol_config'] = {
            'endpoint': '/generate',
            'body': {'model': '{{model}}', 'messages': '{{messages}}'},
            'response': {'text': '/answer'},
            'headers': {'Cache-Control': 'max-age=3600', 'Pragma': 'cache'},
            'discovery': {'endpoint': '/catalog', 'items': '/entries', 'id': '/id'},
        }
    return result


def payload(protocol, model_id='one'):
    if protocol == 'google_gemini':
        return {'models': [{'name': 'models/' + model_id}]}
    return {'entries' if protocol == 'custom_http' else 'data': [{'id': model_id}]}


def discover(protocol, handler):
    return asyncio.run(providers.discover(connection(protocol), KEY, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_every_catalog_pull_requests_cache_revalidation(protocol):
    def handler(request):
        assert request.method == 'GET'
        assert request.headers.get_list('cache-control') == ['no-cache']
        assert request.headers.get_list('pragma') == ['no-cache']
        return httpx.Response(200, json=payload(protocol))

    assert discover(protocol, handler) == [{'id': 'one', 'name': 'one'}]


@pytest.mark.parametrize('protocol', ['openai_chat', 'openai_responses', 'anthropic_messages'])
@pytest.mark.parametrize('cursor', [None, '', '   '])
def test_has_more_without_usable_cursor_never_returns_partial_success(protocol, cursor):
    response = payload(protocol) | {'has_more': True}
    if cursor is not None:
        response['last_id'] = cursor
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    with pytest.raises(providers.ProviderError, match='分页'):
        discover(protocol, handler)
    assert len(requests) == 1


@pytest.mark.parametrize('protocol', ['openai_chat', 'openai_responses', 'anthropic_messages'])
@pytest.mark.parametrize('has_more', [None, 0, 1, 'false', [], {}])
def test_has_more_requires_a_boolean(protocol, has_more):
    response = payload(protocol) | {'has_more': has_more}
    with pytest.raises(providers.ProviderError, match='分页'):
        discover(protocol, lambda request: httpx.Response(200, json=response))


@pytest.mark.parametrize('response', [{'unexpected': 'gateway'}, {'error': {'message': KEY}}, {'models': [], 'error': {'code': 403, 'message': KEY}}])
def test_gemini_missing_catalog_or_error_envelope_is_not_an_empty_success(response):
    with pytest.raises(providers.ProviderError) as error:
        discover('google_gemini', lambda request: httpx.Response(200, json=response))
    assert KEY not in str(error.value)


@pytest.mark.parametrize('response', [{}, {'models': None}, {'nextPageToken': None}])
def test_gemini_protojson_empty_repeated_field_may_be_omitted_or_null(response):
    # ListModelsResponse.models is repeated: ProtoJSON omits empty lists and
    # parsers treat null as an unset field, not as an invalid response.
    assert discover('google_gemini', lambda request: httpx.Response(200, json=response)) == []


def test_gemini_empty_nonfinal_page_still_fetches_following_page():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={'nextPageToken': 'next'})
        assert request.url.params['pageToken'] == 'next'
        return httpx.Response(200, json=payload('google_gemini'))

    assert discover('google_gemini', handler) == [{'id': 'one', 'name': 'one'}]
    assert len(requests) == 2


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_explicit_empty_upstream_catalog_is_valid(protocol):
    field = 'models' if protocol == 'google_gemini' else 'entries' if protocol == 'custom_http' else 'data'
    assert discover(protocol, lambda request: httpx.Response(200, json={field: []})) == []


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_discovery_never_forwards_credentials_to_a_redirected_origin(protocol):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={'location': 'https://other.example/collect'}, json={'error': KEY})

    with pytest.raises(providers.ProviderError) as error:
        discover(protocol, handler)
    assert len(requests) == 1 and requests[0].url.host == 'catalog.example'
    assert KEY not in str(error.value)


@pytest.mark.parametrize('protocol', ['openai_chat', 'openai_responses', 'anthropic_messages', 'google_gemini'])
def test_multiple_pages_return_every_unique_model_and_revalidate_each_page(protocol):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers['cache-control'] == 'no-cache'
        if len(requests) == 1:
            pagination = {'nextPageToken': 'next'} if protocol == 'google_gemini' else {'has_more': True, 'last_id': 'next'}
            return httpx.Response(200, json=payload(protocol) | pagination)
        cursor_key = 'pageToken' if protocol == 'google_gemini' else 'after_id' if protocol == 'anthropic_messages' else 'after'
        assert request.url.params[cursor_key] == 'next'
        return httpx.Response(200, json=payload(protocol, 'two'))

    assert [model['id'] for model in discover(protocol, handler)] == ['one', 'two']
    assert len(requests) == 2


@pytest.mark.parametrize('protocol', ['openai_chat', 'anthropic_messages', 'google_gemini'])
@pytest.mark.parametrize('repeated', [True, False])
def test_repeated_cursor_and_page_limit_fail_instead_of_returning_partial_catalog(protocol, repeated):
    requests = []

    def handler(request):
        requests.append(request)
        cursor = 'same' if repeated else str(len(requests))
        pagination = {'nextPageToken': cursor} if protocol == 'google_gemini' else {'has_more': True, 'last_id': cursor}
        return httpx.Response(200, json=payload(protocol, str(len(requests))) | pagination)

    with pytest.raises(providers.ProviderError):
        discover(protocol, handler)
    assert len(requests) == (2 if repeated else 10)


@pytest.mark.parametrize('metadata', [
    {'has_more': True, 'last_id': 'page-two'}, {'hasMore': True},
    {'nextPageToken': 'page-two'}, {'next_page_token': 'page-two'},
    {'next_page': 2}, {'nextPage': 2}, {'next': 'https://other.example/second'},
    {'next_cursor': 'page-two'}, {'nextCursor': 'page-two'},
    {'pagination': {'has_more': True}}, {'meta': {'next': 'page-two'}},
    {'error': {'message': KEY}},
])
def test_custom_discovery_rejects_pagination_and_error_envelopes(metadata):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload('custom_http') | metadata)

    with pytest.raises(providers.ProviderError, match='分页|错误') as error:
        discover('custom_http', handler)
    assert len(requests) == 1 and KEY not in str(error.value)


@pytest.mark.parametrize('metadata', [
    {'has_more': False, 'last_id': 'one'}, {'hasMore': False},
    {'nextPageToken': None}, {'next_page': None}, {'next': None},
    {'next_cursor': ''}, {'pagination': {'has_more': False, 'next': None}},
    {'error': None}, {'error': {}},
])
def test_custom_discovery_accepts_explicit_terminal_page_markers(metadata):
    assert discover('custom_http', lambda request: httpx.Response(200, json=payload('custom_http') | metadata)) == [
        {'id': 'one', 'name': 'one'}]


def test_custom_discovery_rejects_a_next_link_without_following_it():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload('custom_http'),
            headers={'Link': '<https://other.example/page-two>; rel="next"'})

    with pytest.raises(providers.ProviderError, match='分页'):
        discover('custom_http', handler)
    assert len(requests) == 1


@pytest.mark.parametrize('metadata', [{'has_more': True}, {'error': {'message': KEY}}])
def test_custom_discovery_checks_envelopes_along_the_configured_items_path(metadata):
    conn = connection('custom_http')
    conn['protocol_config']['discovery']['items'] = '/payload/entries'
    response = {'payload': payload('custom_http') | metadata}
    with pytest.raises(providers.ProviderError, match='分页|错误'):
        asyncio.run(providers.discover(conn, KEY, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response))))
