"""Contract tests against an in-process fake House; these are not model scores."""
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quantguard.errors import BudgetExceeded, ConfigurationError, ModelError
from quantguard.house import Budget, HouseClient

TEST_TOKEN = 'LOCAL-TEST-ONLY-DO-NOT-USE-AS-CREDENTIAL'


@contextmanager
def server(response=None, status=200, headers=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'),
                             'payload': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            body = response(len(requests)) if callable(response) else response
            self.wfile.write(json.dumps(body or {
                'choices': [{'finish_reason': 'stop', 'message': {'content': 'program'}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 4},
            }).encode())

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}', requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def client(endpoint, budget=None):
    return HouseClient(budget or Budget(), timeout=2, environ={
        'MODEL_ENDPOINT': endpoint, 'MODEL_NAME': 'house', 'MODEL_TOKEN': TEST_TOKEN,
    })


def test_exact_official_request_shape(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    with server() as (endpoint, requests):
        c = client(endpoint)
        assert c.generate([{'role': 'user', 'content': 'test'}], seed=7) == 'program'
        assert c.budget.calls == 1
        request = requests[0]
        assert request['path'] == '/v1/chat/completions'
        assert request['authorization'] == 'Bearer ' + TEST_TOKEN
        body = request['payload']
        assert body['model'] == 'house'
        assert body['seed'] == 7 and body['temperature'] == 0
        assert body['max_tokens'] <= 4000 and body['n'] == 1
        assert 'tools' not in body and not body['stream']


def test_http_error_counts_once_without_body_leak(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    with server({'error': TEST_TOKEN}, status=500) as (endpoint, requests):
        c = client(endpoint)
        with pytest.raises(ModelError) as exc:
            c.generate([{'role': 'user', 'content': 'test'}])
        assert TEST_TOKEN not in str(exc.value)
        assert c.budget.calls == len(requests) == 1


def test_redirect_refused_without_credential_forwarding(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    with server() as (target, target_requests):
        with server(status=307, headers={'Location': target + '/stolen'}) as (source, source_requests):
            with pytest.raises(ModelError):
                client(source).generate([{'role': 'user', 'content': 'test'}])
            assert len(source_requests) == 1
            assert target_requests == []


@pytest.mark.parametrize('endpoint', ['https://example.test/v1', 'https://user:secret@example.test',
                                      'file:///etc/passwd', 'https://example.test?x=y'])
def test_reject_non_origin_endpoints(endpoint):
    with pytest.raises(ConfigurationError):
        client(endpoint)


def test_required_config_has_no_fallback():
    with pytest.raises(ConfigurationError, match='missing House'):
        HouseClient(Budget(), environ={})


def test_global_call_and_input_caps():
    budget = Budget(max_calls=25)
    for _ in range(25):
        budget.reserve([{'role': 'user', 'content': 'hi'}])
    with pytest.raises(BudgetExceeded):
        budget.reserve([])
    assert budget.calls == 25
    small = Budget(max_input_tokens=5000)
    with pytest.raises(BudgetExceeded):
        small.reserve([{'role': 'user', 'content': 'x' * 1000}])
    assert small.calls == 0


@pytest.mark.parametrize('kwargs', [{'max_calls': 26}, {'max_output_tokens': 4001},
                                   {'max_input_tokens': 1_000_001}])
def test_invalid_allowances_rejected(kwargs):
    with pytest.raises(ConfigurationError):
        Budget(**kwargs)


def test_expired_deadline_never_sends():
    with pytest.raises(BudgetExceeded):
        Budget(deadline=time.monotonic() - 1).reserve([])


def test_truncated_and_multi_choice_responses_are_not_programs(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    for payload in [
        {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]},
        {'choices': [{'message': {'content': 'a'}}, {'message': {'content': 'b'}}]},
    ]:
        with server(payload) as (endpoint, requests):
            with pytest.raises(ModelError):
                client(endpoint).generate([{'role': 'user', 'content': 'test'}])
            assert len(requests) == 1
