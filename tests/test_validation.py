import json

import pytest

from quantguard.errors import CandidateError, TaskError
from quantguard.task import Program
from quantguard.validation import validate_outputs


@pytest.mark.parametrize('text', ['{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{broken'])
def test_invalid_or_nonfinite_json_is_not_a_valid_result(tmp_path, text):
    (tmp_path / 'result.json').write_text(text)
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['result.json'])


def test_csv_requires_consistent_columns(tmp_path):
    (tmp_path / 'result.csv').write_text('one,two\n1,2,3\n')
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['result.csv'])


def test_symlink_output_is_refused(tmp_path):
    (tmp_path / 'real.json').write_text('{}')
    (tmp_path / 'result.json').symlink_to(tmp_path / 'real.json')
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['result.json', 'real.json'])


def test_undeclared_extra_files_cannot_leak_into_deliverable(tmp_path):
    (tmp_path / 'result.json').write_text('{"answer":1}')
    (tmp_path / 'raw-input.txt').write_text('input must not be copied')
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['result.json'])


def test_combined_tree_limit_applies_across_files(tmp_path):
    for name in ['a.bin', 'b.bin']:
        with (tmp_path / name).open('wb') as stream:
            stream.truncate(33 * 1024**2)
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['a.bin', 'b.bin'])


def test_canary_across_scan_chunk_boundary_is_refused(tmp_path):
    canary = 'SYNTHETIC-CONTAMINATION-MARKER'
    (tmp_path / 'result.txt').write_bytes(b'a' * (1024**2 - 10) + canary.encode())
    with pytest.raises(CandidateError):
        validate_outputs(tmp_path, ['result.txt'], canaries=[canary])


@pytest.mark.parametrize('filename', ['reward.txt', 'diagnostic.json', 'reward_summary.json',
                                     'nested/reward.json', 'nested/pytest_report.json'])
def test_generated_program_cannot_claim_verifier_artifacts(filename):
    with pytest.raises(TaskError):
        Program.parse(json.dumps({'code': 'print(1)', 'outputs': [filename]}), [])
