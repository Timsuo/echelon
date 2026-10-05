import ast
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.onebot.actions import PRIVATE_OUTPUT_ACTIONS, READ_ONLY_ACTIONS
from scripts.check_tracked_files import forbidden, main

ROOT = Path(__file__).resolve().parents[1]


def test_tracked_files_exclude_runtime_data():
    main()
    for path in ('.env', '.env.local', 'data/messages.db', 'logs/app.log', 'data/attachments/file.txt', 'attachments/a.bin'):
        assert forbidden(path)
    assert not forbidden('.env.example') and not forbidden('app/attachments/worker.py')


def test_ci_workflow_has_both_triggers_and_offline_checks():
    # BaseLoader avoids YAML 1.1 interpreting the Actions key `on` as a boolean.
    document = yaml.load((ROOT/'.github/workflows/ci.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert set(document['on']) == {'push', 'pull_request'}
    assert document['permissions'] == {'contents': 'read'}
    job = document['jobs']['test']
    assert job['runs-on'] == 'windows-latest'
    assert set(job['strategy']['matrix']['python-version']) == {'3.12', '3.14'}
    commands = '\n'.join(step.get('run', '') for step in job['steps'])
    for required in ('requirements.lock.txt', '-m pytest', '-m ruff check app tests', '-m compileall -q app', '-m pip check', 'check_tracked_files.py'):
        assert required in commands
    assert not any('upload-artifact' in step.get('uses', '') for step in job['steps'])
    assert 'secrets.' not in str(document)


def test_ci_test_step_works_without_existing_temp_directory(tmp_path):
    document = yaml.load((ROOT/'.github/workflows/ci.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    step = next(step for step in document['jobs']['test']['steps']
                if step.get('name') == 'Tests (mock services only)')
    checkout = tmp_path / 'clean-checkout'
    checkout.mkdir()
    (checkout / 'test_smoke.py').write_text(
        'def test_temp_directory(tmp_path):\n    assert tmp_path.is_dir()\n', encoding='utf-8')
    assert not (checkout / '.pytest_tmp').exists()
    # Execute the workflow's actual Python commands against a tiny tmp_path test.
    # This catches missing parent directories before any application fixture runs.
    for line in step['run'].splitlines():
        arguments = shlex.split(line)
        assert arguments[0] == 'python'
        if arguments[1:3] == ['-m', 'pytest']:
            arguments.append('test_smoke.py')
        result = subprocess.run([sys.executable, *arguments[1:]], cwd=checkout,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


def test_external_network_and_dns_guard():
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match='External network'):
            sock.connect(('1.1.1.1', 443))
    with pytest.raises(AssertionError, match='External DNS'):
        socket.getaddrinfo('api.deepseek.com', 443)


def test_firewall_and_only_one_websocket_writer():
    assert READ_ONLY_ACTIONS == {'get_group_file_url', 'get_group_msg_history', 'get_group_info', 'get_group_list'}
    assert PRIVATE_OUTPUT_ACTIONS == {'send_private_msg', 'upload_private_file'}
    writers = []
    seed_readers = []
    for path in (ROOT/'app').rglob('*.py'):
        source = path.read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'send_json':
                writers.append(path.relative_to(ROOT).as_posix())
        if 'groups.allowed' in source:
            seed_readers.append(path.relative_to(ROOT).as_posix())
    assert writers == ['app/onebot/actions.py']
    assert seed_readers == ['app/application.py']
