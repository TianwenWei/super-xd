"""API and worker must observe the same settings after explicit local bootstrap."""
import os
import subprocess
import sys

import pytest


def test_bootstrap_does_not_freeze_database_before_loading_environment(tmp_path):
    database = tmp_path / 'custom.sqlite3'
    config = tmp_path / '.env'
    resources = tmp_path / 'resources'
    (resources / 'prompts').mkdir(parents=True)
    (resources / 'config').mkdir()
    config.write_text(f'DATABASE_PATH={database}\nSUPER_XD_WORKSPACE={tmp_path}\nSUPER_XD_RESOURCE_ROOT={resources}\n')
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHON_DOTENV_DISABLED', 'DATABASE_PATH', 'SUPER_XD_WORKSPACE', 'SUPER_XD_RESOURCE_ROOT'}}
    code = '''
import os, sys
from pathlib import Path
from super_xd.demo.environment import load_local_environment
assert 'super_xd.demo.settings' not in sys.modules
assert 'super_xd.nlp.resources' not in sys.modules
load_local_environment(Path(sys.argv[1]))
from super_xd.demo.settings import DATABASE_PATH, ROOT
assert DATABASE_PATH == os.environ['DATABASE_PATH'] == sys.argv[2]
assert ROOT == Path(os.environ['SUPER_XD_WORKSPACE'])
from super_xd.nlp.resources import RESOURCE_ROOT
assert RESOURCE_ROOT == Path(os.environ['SUPER_XD_RESOURCE_ROOT'])
'''
    subprocess.run([sys.executable, '-c', code, str(config), str(database)],
                   env=environment, check=True, capture_output=True, text=True)


@pytest.mark.parametrize('configuration', ['workspace', 'dotenv', 'cli'])
def test_worker_entrypoint_uses_shared_database_configuration(tmp_path, configuration):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    expected = workspace / 'data' / 'super_xd.sqlite3'
    arguments = []
    if configuration in {'dotenv', 'cli'}:
        expected = workspace / 'configured.sqlite3'
        (workspace / '.env').write_text(f'DATABASE_PATH={expected}\n')
    if configuration == 'cli':
        expected = workspace / 'explicit.sqlite3'
        arguments = ['--database', str(expected)]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHON_DOTENV_DISABLED', 'DATABASE_PATH', 'SUPER_XD_WORKSPACE'}}
    environment['SUPER_XD_WORKSPACE'] = str(workspace)
    code = '''
import asyncio, sys
from super_xd.demo.environment import load_local_environment
load_local_environment()
from super_xd.demo.settings import DATABASE_PATH
from super_xd.demo.tasks import database, worker
expected = sys.argv.pop(1)
captured = []
class DatabaseCaptured(Exception):
    pass
def capture_database(path):
    captured.append(str(path))
    raise DatabaseCaptured
database.Database = capture_database
try:
    asyncio.run(worker._main())
except DatabaseCaptured:
    pass
assert captured == [expected], captured
if '--database' not in sys.argv:
    assert captured == [DATABASE_PATH]
'''
    subprocess.run([sys.executable, '-c', code, str(expected), *arguments], cwd=tmp_path,
                   env=environment, check=True, capture_output=True, text=True)
