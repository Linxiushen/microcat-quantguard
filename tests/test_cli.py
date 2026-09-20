"""Real subprocess execution with synthetic model responses, never real scores."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_house import TEST_TOKEN, server
from test_task import make_unit

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.environ.get('QUANTGUARD_ISOLATION') == 'container', reason='Writable fixtures require native Linux kernel isolation; the container helper instead validates the real CLI with read-only input mounts')


def response(code, outputs=None):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
        'code': code, 'outputs': outputs or ['results.json'],
    })}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 100}}


GOOD_CODE = '''import os, json
from pathlib import Path
root = Path(os.environ['TASK_DIR'])
source = json.loads((root / 'values.json').read_text())
value = sum(source['values'])
assert value == 6
(Path(os.environ['OUT_DIR']) / 'results.json').write_text(json.dumps({'total': value}))
'''


def launch(unit, output, endpoint=None, extra=(), timeout=30):
    env = {k: v for k, v in os.environ.items() if k not in {
        'MODEL_ENDPOINT', 'MODEL_NAME', 'MODEL_TOKEN', 'HTTP_PROXY', 'HTTPS_PROXY',
        'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'}}
    env.update({'PYTHONPATH': str(ROOT / 'src'), 'NO_PROXY': '127.0.0.1', 'QFBENCH_SEED': '0'})
    if endpoint:
        env.update({'MODEL_ENDPOINT': endpoint, 'MODEL_NAME': 'house', 'MODEL_TOKEN': TEST_TOKEN})
    run = subprocess.run([sys.executable, '-m', 'quantguard.cli', 'solve', '--task-dir', str(unit),
                          '--out', str(output), '--wall-time', '20', '--candidate-timeout', '5',
                          '--model-timeout', '3', *extra], env=env, capture_output=True, text=True,
                         timeout=timeout)
    audit = json.loads(run.stdout)
    assert TEST_TOKEN not in run.stdout + run.stderr
    if output.exists():
        assert TEST_TOKEN not in ''.join(p.read_text(errors='ignore') for p in output.rglob('*') if p.is_file())
    return run, audit


@pytest.fixture
def unit(tmp_path):
    root = make_unit(tmp_path / 'unit')
    (root / 'values.json').write_text('{"values":[1,2,3]}')
    return root


def test_real_candidate_executes_and_publishes_only_valid_output(unit, tmp_path):
    out = tmp_path / 'out'
    with server(response(GOOD_CODE)) as (endpoint, requests):
        run, audit = launch(unit, out, endpoint)
    assert run.returncode == 0, audit
    assert json.loads((out / 'results.json').read_text()) == {'total': 6}
    assert audit['official_score'] is None
    assert audit['status'] == 'locally_validated'
    assert len(requests) == 1
    assert sorted(p.name for p in out.iterdir()) == ['quantguard-audit.json', 'results.json']


def test_failed_program_is_repaired_under_same_budget(unit, tmp_path):
    out = tmp_path / 'out'
    with server(lambda n: response('raise ValueError("numerical convergence failed")' if n == 1 else GOOD_CODE)) as (endpoint, requests):
        run, audit = launch(unit, out, endpoint)
    assert run.returncode == 0, audit
    assert len(requests) == audit['house']['requests_reserved'] == 2
    assert any('Repair this single candidate' in x['content'] for x in requests[1]['payload']['messages'])


def test_no_repair_baseline_uses_one_request(unit, tmp_path):
    with server(response('raise ValueError("failure")')) as (endpoint, requests):
        run, audit = launch(unit, tmp_path / 'out', endpoint, ['--no-repair'])
    assert run.returncode == 2
    assert len(requests) == 1
    assert not (tmp_path / 'out/results.json').exists()


def test_missing_model_configuration_never_reports_success(unit, tmp_path):
    run, audit = launch(unit, tmp_path / 'out')
    assert run.returncode == 2
    assert audit['house']['requests_reserved'] == 0
    assert audit['status'] == 'failed'


def test_candidate_does_not_inherit_model_token(unit, tmp_path):
    code = '''import os, json
from pathlib import Path
assert 'MODEL_TOKEN' not in os.environ
assert 'MODEL_ENDPOINT' not in os.environ
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'isolated':True}))
'''
    with server(response(code)) as (endpoint, _):
        run, audit = launch(unit, tmp_path / 'out', endpoint)
    assert run.returncode == 0, audit


def test_candidate_cannot_mutate_read_only_task(unit, tmp_path):
    code = '''import os
from pathlib import Path
(Path(os.environ['TASK_DIR'])/'values.json').write_text('modified')
'''
    with server(response(code)) as (endpoint, _):
        run, audit = launch(unit, tmp_path / 'out', endpoint, ['--no-repair'])
    assert run.returncode == 2
    assert json.loads((unit / 'values.json').read_text()) == {'values': [1, 2, 3]}


def test_timeout_and_exhausted_call_budget_fail_without_artifacts(unit, tmp_path):
    with server(response('while True: pass')) as (endpoint, requests):
        run, audit = launch(unit, tmp_path / 'out', endpoint,
                            ['--candidate-timeout', '0.2', '--max-calls', '1'])
    assert run.returncode == 2
    assert len(requests) == 1
    assert not (tmp_path / 'out/results.json').exists()
    assert audit['elapsed_seconds'] < 10


def test_stale_outputs_are_preserved_and_refused(unit, tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'results.json').write_text('{"old":true}')
    with server(response(GOOD_CODE)) as (endpoint, requests):
        run, audit = launch(unit, out, endpoint)
    assert run.returncode == 2
    assert requests == []
    assert (out / 'results.json').read_text() == '{"old":true}'
