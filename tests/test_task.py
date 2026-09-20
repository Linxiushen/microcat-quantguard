import json
from pathlib import Path

import pytest

from quantguard.errors import TaskError
from quantguard.task import Program, Task, safe_relative


def make_unit(path, instruction='Write /app/output/results.json.'):
    path.mkdir()
    (path / 'instruction.md').write_text(instruction)
    (path / 'card.toml').write_text('''[task]
id = "local-contract-test"
track = "coding"
split = "public-dev"
[agent]
timeout_sec = 30
[environment]
network = "restricted"
''')
    return path


def test_per_unit_filenames_are_not_hardcoded(tmp_path):
    a = Task.load(make_unit(tmp_path / 'a', 'Write /app/output/results.json.'))
    b = Task.load(make_unit(tmp_path / 'b', 'Write /output/summary.csv and /app/output/chart.png.'))
    assert a.expected_files == ['results.json']
    assert b.expected_files == ['chart.png', 'summary.csv']


def test_task_prompt_excludes_sealed_checks_and_solution(tmp_path):
    root = make_unit(tmp_path / 'unit')
    (root / 'checks').mkdir()
    (root / 'checks' / 'test_outputs.py').write_text('SECRET_CHECK_ANSWER')
    (root / 'solution').mkdir()
    (root / 'solution' / 'solve.sh').write_text('SECRET_SOLVE')
    task = Task.load(root)
    assert 'checks' not in [part for p in task.files for part in p.relative_to(root).parts]
    assert 'SECRET_CHECK_ANSWER' not in task.context()
    assert 'SECRET_SOLVE' not in task.context()


def test_canary_removed_from_model_context(tmp_path):
    marker = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    task = Task.load(make_unit(tmp_path / 'unit', f'{marker}\nWrite /output/a.json.'))
    assert marker not in task.context()


def test_task_symlink_cannot_escape_input(tmp_path):
    root = make_unit(tmp_path / 'unit')
    (tmp_path / 'outside.txt').write_text('not part of task')
    (root / 'input.csv').symlink_to(tmp_path / 'outside.txt')
    with pytest.raises(TaskError, match='symlink'):
        Task.load(root)


@pytest.mark.parametrize('path', ['../escape.json', '/tmp/escape.json', 'sub/../../escape.json',
                                 'quantguard-audit.json', 'reward.json', 'pytest_report.json', 'x\\y'])
def test_reject_unsafe_or_verifier_owned_outputs(path):
    with pytest.raises(TaskError):
        safe_relative(path)


def test_candidate_cannot_omit_required_deliverable():
    with pytest.raises(TaskError, match='omitted'):
        Program.parse(json.dumps({'code': 'print(1)', 'outputs': ['wrong.json']}), ['results.json'])


def test_syntax_error_is_repairable_diagnostic():
    with pytest.raises(TaskError, match='syntax error'):
        Program.parse(json.dumps({'code': 'if broken:', 'outputs': ['results.json']}), [])


def test_fenced_python_needs_real_contract():
    p = Program.parse('```python\nprint(1)\n```', ['results.json'])
    assert p.outputs == ['results.json']
    with pytest.raises(TaskError):
        Program.parse('```python\nprint(1)\n```', [])


def test_all_official_public_units_parse_without_reading_checks():
    units = Path(__file__).resolve().parents[1] / 'vendor/track1-coding-public/units'
    if not units.is_dir():
        pytest.skip('Optional official public checkout not present')
    cards = sorted(units.glob('*/card.toml'))
    assert len(cards) == 87
    errors = []
    for card in cards:
        try:
            task = Task.load(card.parent)
            assert task.timeout > 0
            assert len(task.context().encode()) <= 180_000
            assert task.check_context == ''
        except Exception as exc:
            errors.append(f'{card.parent.name}: {type(exc).__name__}: {exc}')
    assert not errors, '\n'.join(errors)
