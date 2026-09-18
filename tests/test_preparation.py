import json
import httpx
import pytest

def configuration():
    return {'actions': {'seed': {
        'method': 'POST', 'url': 'http://127.0.0.1:8766/seed',
        'parameters': {'tasks': {'type': 'array', 'required': True}},
        'success': {'path': ['ready'], 'equals': True},
    }}, 'checks': {'health': {
        'method': 'GET', 'url': 'http://127.0.0.1:8766/health',
        'success': {'path': ['ready'], 'equals': True},
    }}}

def call(config=None, payload=None, response=None):
    from preparation.service import PreparationService
    requests = []
    def handle(request):
        requests.append(request)
        return response or httpx.Response(200, json={'ready': True})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    result = PreparationService(config or configuration(), client=client).prepare_test_data(
        'seed', {'tasks': [{'title': 'demo'}]} if payload is None else payload)
    return result, requests

def test_prepares_typed_json_and_returns_no_payload_or_secret():
    result, requests = call(payload={'tasks': [{'title': 'PRIVATE_INPUT_MARKER'}]})
    assert result['status'] == 'success'
    assert result['request_sent'] is True
    assert len(requests) == 1
    assert json.loads(requests[0].content)['tasks'][0]['title'] == 'PRIVATE_INPUT_MARKER'
    assert 'PRIVATE_INPUT_MARKER' not in json.dumps(result)

@pytest.mark.parametrize('payload', [{}, {'tasks': '[]'}, {'tasks': [], 'url': 'https://example.org'}])
def test_invalid_parameters_make_zero_requests(payload):
    result, requests = call(payload=payload)
    assert result['status'] == 'failure'
    assert result['request_sent'] is False
    assert not requests

@pytest.mark.parametrize('body,status', [({'ready': False}, 'failure'), ({}, 'unknown'), ({'ready': 1}, 'failure')])
def test_http_200_is_not_completion(body, status):
    result, _ = call(response=httpx.Response(200, json=body))
    assert result['status'] == status

def test_timeout_is_unknown_and_never_retried():
    from preparation.service import PreparationService
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('PRIVATE_RESPONSE_MARKER', request=request)
    result = PreparationService(configuration(), client=httpx.Client(transport=httpx.MockTransport(handle))).prepare_test_data('seed', {'tasks': []})
    assert result['status'] == 'unknown'
    assert len(calls) == 1
    assert 'PRIVATE_RESPONSE_MARKER' not in json.dumps(result)

def test_check_uses_registered_request_only():
    from preparation.service import PreparationService
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'ready': True})))
    service = PreparationService(configuration(), client=client)
    assert service.check_environment('health')['status'] == 'success'
    assert service.check_environment('https://example.org')['request_sent'] is False

@pytest.mark.parametrize('url', ['file:///tmp/data', 'https://user:pass@example.org', 'https://example.org/#secret'])
def test_invalid_configuration_rejected(url):
    from preparation.service import PreparationService
    config = configuration()
    config['actions']['seed']['url'] = url
    with pytest.raises(ValueError):
        PreparationService(config)

def test_tools_use_object_schema_and_real_handler_forwarding(monkeypatch):
    import asyncio
    from preparation import tool
    class Service:
        def prepare_test_data(self, action, parameters):
            assert (action, parameters) == ('seed', {'tasks': []})
            return {'status': 'success', 'request_sent': True}
    monkeypatch.setattr(tool, 'load_service', lambda: Service())
    schema = tool.prepare_test_data.input_schema
    assert schema['properties']['parameters']['type'] == 'object'
    result = asyncio.run(tool.prepare_test_data.handler({'action': 'seed', 'parameters': {'tasks': []}}))
    assert json.loads(result['content'][0]['text'])['status'] == 'success'

