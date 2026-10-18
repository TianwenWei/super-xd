"""The actor consumes the same normalized native messages as the NLP engine."""
from copy import deepcopy

import pytest

from tests.support.session import call, make_actor, wait_for


@pytest.mark.parametrize('encoding', ['', 'missing', '{}'])
async def test_empty_arguments_reach_actor_and_next_turn_history(make_actor, encoding):
    proposed = call('close_all_windows')
    opaque = {'google': {'thought_signature': 'offline-provider-signature+/=='}}
    proposed['extra_content'] = deepcopy(opaque)
    if encoding == 'missing':
        proposed['function'].pop('arguments')
    else:
        proposed['function']['arguments'] = encoding
    actor, queue, model = await make_actor([
        {'content': '正在关闭车窗。', 'tool_calls': [proposed]}, {'content': '收到。'}])
    await actor.post({'type': 'input', 'text': '关闭所有车窗'})
    done, events = await wait_for(queue, 'stream.done', ack_actor=actor)
    assert done['metrics']['error'] is None
    assert len([event for event in events if event['type'] == 'vehicle.command']) == 1
    stored = next(row for row in actor.current.messages if row.get('tool_calls'))
    assert stored['tool_calls'][0]['function']['arguments'] == '{}'
    assert stored['tool_calls'][0]['extra_content'] == opaque
    await actor.post({'type': 'input', 'text': '谢谢'})
    next_done, _ = await wait_for(queue, 'stream.done', ack_actor=actor)
    assert next_done['metrics']['error'] is None
    historical = next(row for row in model.requests[-1]['messages'] if row.get('tool_calls')
                      and row['tool_calls'][0]['id'] == proposed['id'])
    assert historical['tool_calls'][0]['function']['arguments'] == '{}'
    assert historical['tool_calls'][0]['extra_content'] == opaque
