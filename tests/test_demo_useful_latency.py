"""Demo latency distinguishes audible output from a completed useful result."""
import pytest

from backend.services.contracts import ProviderError
from test_session import call, make_actor, wait_for


async def test_failure_template_is_spoken_but_never_a_useful_result(make_actor):
    actor, queue, _ = await make_actor([ProviderError('timeout', 'offline timeout', provider='fixture')])
    await actor.post({'type': 'input', 'text': '请处理这次请求'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    assert done['metrics']['error'] == 'timeout'
    assert done['metrics']['first_effective_ms'] is not None
    assert done['metrics']['first_speech_ms'] is not None
    assert done['metrics']['first_model_text_ms'] is None
    assert done['metrics']['first_useful_ms'] is None
    assert actor.turns[-1].metrics['output_source'] == 'error_template'


async def test_partial_model_text_then_failure_remains_audible_but_not_useful(make_actor):
    actor, queue, model = await make_actor([])

    async def partial(*args, **kwargs):
        yield {'type': 'delta', 'text': '我来看看。'}
        raise ProviderError('unavailable', 'offline disconnect', provider='fixture')

    model.stream = partial
    await actor.post({'type': 'input', 'text': '请帮我解释一下'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    assert done['metrics']['first_model_text_ms'] is not None
    assert done['metrics']['first_speech_ms'] is not None
    assert done['metrics']['first_useful_ms'] is None


@pytest.mark.parametrize('with_control', [False, True])
async def test_lookup_then_model_timeout_counts_only_the_actual_control_receipt(make_actor, with_control):
    calls = [call('get_weather', {'location': '巴黎', 'date': 'today'}, id='lookup')]
    if with_control:
        calls.insert(0, call('set_volume', {'volume': 40}, id='volume'))
    actor, queue, _ = await make_actor([{'tool_calls': calls},
        ProviderError('timeout', 'offline followup timeout', provider='fixture')])

    async def query(item):
        # Query/control classification remains authoritative even if a query
        # adapter accidentally includes an execution-shaped flag.
        return {'ok': True, 'executed': True, 'summary': {'temperature_c': 18}}

    actor.resources.query = query
    await actor.post({'type': 'input', 'text': '请处理控制和查询请求'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    metrics = done['metrics']
    assert metrics['error'] == 'timeout' and metrics['model_calls'] == 2
    assert metrics['first_speech_ms'] is not None
    if with_control:
        receipt_ms = actor.turns[-1].metrics['last_execution_receipt_ms']
        assert metrics['first_useful_ms'] == receipt_ms
        assert receipt_ms < metrics['first_speech_ms'] <= metrics['total_ms']
        assert actor.vehicle['media']['volume'] == 40
    else:
        assert metrics['first_useful_ms'] is None
        assert 'last_execution_receipt_ms' not in actor.turns[-1].metrics


async def test_successful_prefaced_control_still_counts_the_real_receipt(make_actor):
    actor, queue, _ = await make_actor([{'content': '这就调整。',
                                       'tool_calls': [call('set_volume', {'volume': 40})]}])
    await actor.post({'type': 'input', 'text': '音量调到40'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    assert done['metrics']['error'] is None
    assert done['metrics']['first_speech_ms'] < done['metrics']['first_useful_ms']
    assert done['metrics']['first_useful_ms'] == actor.turns[-1].metrics['last_execution_receipt_ms']


async def test_complete_model_answer_keeps_its_first_text_as_useful(make_actor):
    actor, queue, _ = await make_actor([{'content': '很高兴陪你出发。'}])
    await actor.post({'type': 'input', 'text': '你好'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    assert done['metrics']['error'] is None
    assert done['metrics']['first_useful_ms'] == done['metrics']['first_model_text_ms']
    assert done['metrics']['first_speech_ms'] is not None
