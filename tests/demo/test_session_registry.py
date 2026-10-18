from types import SimpleNamespace

from super_xd.demo.session.registry import SessionRegistry


async def test_idle_reaping_keeps_connected_or_running_sessions():
    closed = []

    def actor(name, **changes):
        async def close():
            closed.append(name)
        return SimpleNamespace(clients=set(), audio_clients=set(), generating=False,
            last_activity=0, tasks={}, close=close, **changes)

    registry = SessionRegistry()
    registry['idle'] = actor('idle')
    registry['connected'] = actor('connected')
    registry['connected'].clients.add('client')
    registry['working'] = actor('working')
    registry['working'].tasks['task'] = {'status': 'running'}
    assert await registry.reap(now=2000) == ['idle']
    assert closed == ['idle']
    assert await registry.remove('connected') is True
    assert await registry.remove('connected') is False
    await registry.close()
    assert set(closed) == {'idle', 'connected', 'working'} and not registry
