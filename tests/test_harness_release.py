"""Release identity is content based, cached and separate from runtime secrets."""
from pathlib import Path

import pytest

from backend.harness import release
from backend.harness.contracts import PreparedRun, RunIdentity
from backend.harness.run import AgentRun, RunProtocolError


def fixture_release(root):
    for directory in release.SOURCE_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    paths = {*release.SOURCE_FILES, *release.REQUIRED_PROMPTS, 'config/services.toml',
             'prompts/talker/tools.toml', 'backend/harness/engine.py',
             'backend/services/contracts.py', 'backend/cockpit/tools.py'}
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'fixture content for {relative}\n', encoding='utf-8')
    return root


def test_release_identity_is_independent_of_root_and_mtime(tmp_path):
    first = fixture_release(tmp_path / 'first')
    second = fixture_release(tmp_path / 'second')
    (second / 'prompts/talker.md').touch()
    assert release.build_release_manifest(first) == release.build_release_manifest(second)


@pytest.mark.parametrize('relative', [
    'prompts/talker.md', 'prompts/reasoner_foreground.md', 'prompts/reasoner_background.md',
    'prompts/talker/tools.toml', 'prompts/talker/examples.json',
    'config/services.toml', 'backend/catalog.py', 'backend/nlp_app.py', 'backend/harness/engine.py',
    'backend/services/contracts.py', 'backend/cockpit/tools.py',
])
def test_changed_runtime_input_changes_release_id(tmp_path, relative):
    root = fixture_release(tmp_path)
    before = release.build_release_manifest(root)
    (root / relative).write_text('changed deployment content\n', encoding='utf-8')
    after = release.build_release_manifest(root)
    assert before['release_id'] != after['release_id']
    assert {key for key in before['files'] if before['files'][key] != after['files'][key]} == {relative}


def test_new_and_removed_prompt_modules_change_release_identity(tmp_path):
    root = fixture_release(tmp_path)
    original = release.build_release_manifest(root)
    path = root / 'prompts/talker/extra.md'
    path.write_text('new module', encoding='utf-8')
    assert release.build_release_manifest(root)['release_id'] != original['release_id']
    path.unlink()
    assert release.build_release_manifest(root) == original


def test_only_declared_release_inputs_are_read_and_manifest_contains_hashes(tmp_path, monkeypatch):
    root = fixture_release(tmp_path)
    for relative in ('.env', 'config/.env', 'prompts/.env', 'prompts/README.md', 'private.json'):
        (root / relative).write_text('private fixture content', encoding='utf-8')
    actual_read = Path.read_bytes
    observed = []

    def read(path):
        relative = path.relative_to(root).as_posix()
        assert relative not in {'.env', 'config/.env', 'prompts/.env', 'prompts/README.md', 'private.json'}
        observed.append(relative)
        return actual_read(path)

    monkeypatch.setattr(Path, 'read_bytes', read)
    first = release.build_release_manifest(root)
    monkeypatch.setenv('ARK_API_KEY', 'changed-offline-fixture-key')
    monkeypatch.setenv('TALKER_MODEL', 'changed-runtime-model')
    assert release.build_release_manifest(root) == first
    assert set(observed) == set(first['files'])
    assert all(len(value) == 64 and set(value) <= set('0123456789abcdef') for value in first['files'].values())


def test_cached_getters_are_independent_copies_and_perform_no_file_io(monkeypatch):
    expected = release.get_release_manifest()

    def unexpected_io(*args, **kwargs):
        pytest.fail('Release getters must not read files on request paths')

    monkeypatch.setattr(Path, 'open', unexpected_io)
    monkeypatch.setattr(Path, 'read_bytes', unexpected_io)
    returned = release.get_release_manifest()
    returned['files'].clear()
    returned['release_id'] = 'mutated'
    assert release.get_release_manifest() == expected
    assert release.get_release_id() == expected['release_id']


def test_missing_required_source_fails_without_silent_release_fallback(tmp_path):
    root = fixture_release(tmp_path)
    (root / 'config/services.toml').unlink()
    with pytest.raises(FileNotFoundError):
        release.build_release_manifest(root)


def test_symlinked_prompt_cannot_read_a_private_file(tmp_path):
    root = fixture_release(tmp_path / 'project')
    private = tmp_path / 'private.env'
    private.write_text('private fixture content', encoding='utf-8')
    (root / 'prompts/talker/linked.toml').symlink_to(private)
    with pytest.raises(ValueError):
        release.build_release_manifest(root)


def test_checkpoint_from_previous_release_requires_explicit_migration(tmp_path):
    root = fixture_release(tmp_path)
    prepared = PreparedRun(
        identity=RunIdentity(run_id='run', request_id='request', turn_id='turn', context_revision=1),
        messages=[{'role': 'user', 'content': 'offline fixture'}],
        release_id=release.build_release_manifest(root)['release_id'])
    checkpoint = AgentRun(prepared, None).checkpoint()
    assert AgentRun(prepared, None, checkpoint=checkpoint).checkpoint() == checkpoint
    (root / 'prompts/talker.md').write_text('new policy', encoding='utf-8')
    changed = prepared.model_copy(update={'release_id': release.build_release_manifest(root)['release_id']})
    with pytest.raises(RunProtocolError):
        AgentRun(changed, None, checkpoint=checkpoint)


async def test_task_trace_uses_cached_release_and_preserves_provider_metadata():
    from backend.harness.task_runner import TaskRunner

    class Sink:
        async def emit(self, session_id, kind, data, **fields):
            return data

    runner = object.__new__(TaskRunner)
    runner.trace_sink = Sink()
    metadata = {'model': 'actual-provider-model', 'usage': {'input_tokens': 7}}
    data = await runner._trace({'id': 'task', 'session_id': 'session', 'revision': 1},
                              'model.completed', {'meta': metadata, 'release_id': 'untrusted-override'})
    assert data['release_id'] == release.get_release_id()
    assert data['meta'] == metadata


async def test_planner_model_trace_includes_same_release_as_manifest():
    from backend.catalog import initial_vehicle_state
    from backend.harness.vehicle_planner import VehicleControlPlanner
    from test_vehicle_planner import FakeArk, response, temperature_plan

    plan = await VehicleControlPlanner(FakeArk([response(temperature_plan())])).plan(
        '把所有温区设为23度。', initial_vehicle_state(), ['climate'])
    assert plan['ok'] is True
    started = next(event for event in plan['trace_events'] if event['kind'] == 'model.started')
    assert started['data']['release_id'] == release.get_release_id()
    assert started['data']['model'] == plan['provenance']['model']


async def test_talker_model_trace_includes_cached_release(tmp_path):
    from backend.db import Database
    from backend.session import SessionActor
    from test_session import FakeArk, wait_for
    from vehicle_fakes import FakeVehiclePlanner

    actor = SessionActor('release-fixture', Database(tmp_path / 'trace.sqlite3'),
                         FakeArk([{'content': '你好'}]), vehicle_planner=FakeVehiclePlanner())
    try:
        queue = await actor.attach()
        await queue.get()
        await actor.post({'type': 'input', 'text': '你好'})
        await wait_for(queue, 'stream.done')
        await actor.trace_queue.join()
        started = next(event for event in actor.db.traces_after(actor.id) if event['kind'] == 'model.started')
        assert started['data']['release_id'] == release.get_release_id()
    finally:
        await actor.close()
