"""Behavior reports count actual attempts and never turn unknown receipts into a pass."""
import json
import pytest

from evals import gateway_driver
from test_gateway_contract import call, ScriptedModel
from backend.services.contracts import ProviderError
from backend.harness.run import RunProtocolError


class OfflineReads:
    def info(self): return {'configured': True, 'execution_location': 'cloud'}
    async def aclose(self): pass


@pytest.fixture
def make_gateway(monkeypatch):
    monkeypatch.setattr(gateway_driver, 'create_search', OfflineReads)
    monkeypatch.setattr(gateway_driver, 'ManualSearchClient', OfflineReads)
    return lambda model, **case: gateway_driver.EvaluationGateway(
        {'id': 'offline-driver', **case}, model, maps=OfflineReads())


async def test_failed_attempt_is_counted_and_generic_provider_error_is_in_trace(make_gateway):
    class BrokenModel:
        async def stream(self, *args, **kwargs):
            raise RuntimeError('offline transport failure')
            yield
    gateway = make_gateway(BrokenModel())
    try:
        result = await gateway.turn({'input': '你好'}, 'warm', 'driver')
        assert result['model_calls'] == result['talker_model_calls'] == 1
        assert result['error'] == 'model_or_protocol_error'
        assert result['model_errors'][0]['data']['message'] == 'offline transport failure'
        assert result['usage'] == {}
    finally:
        await gateway.close()


@pytest.mark.parametrize('partial', ['', '已经收到你的请求'])
async def test_error_presentation_is_not_model_text_or_a_useful_result(make_gateway, partial):
    from evals.harness import latency_summary
    class BrokenModel:
        async def stream(self, *args, **kwargs):
            if partial:
                yield {'type': 'delta', 'text': partial}
            raise ProviderError('upstream_error' if partial else 'timeout', 'offline provider failure', provider='fixture')
    gateway = make_gateway(BrokenModel())
    try:
        result = await gateway.turn({'input': '你好'}, 'warm', 'driver')
        assert result['text'] and result['first_effective_ms'] is not None
        assert result['presentation_source'] == 'error_template'
        assert result['first_useful_ms'] is None
        assert not result['commands'] and result['model_calls'] == 1
        assert (result['first_model_text_ms'] is not None) == bool(partial)
        if partial:
            assert result['text'].startswith(partial)
            assert result['first_model_text_ms'] == result['first_effective_ms']
        summary = latency_summary([{'turns': [result], 'traces': result['trace']}])['all']
        assert summary['first_text_samples'] == summary['error_turns'] == 1
        assert summary['model_text_samples'] == int(bool(partial))
        assert summary['no_model_text_turns'] == int(not partial)
        assert summary['model_text_unknown_turns'] == 0
        assert summary['cache_groups']['unknown']['turns'] == 1
    finally:
        await gateway.close()


async def test_failed_continuation_after_query_receipt_has_no_executed_result_latency(make_gateway):
    model = ScriptedModel([{'tool_calls': [call('get_weather', {'location': '巴黎', 'date': 'today'})]}])
    gateway = make_gateway(model, lookup_fixtures={'get_weather': [{'ok': True, 'summary': {'temperature_c': 18}}]})
    try:
        result = await gateway.turn({'input': '巴黎今天天气怎么样'}, 'warm', 'driver')
        assert result['error'] and result['lookups'] == ['get_weather']
        assert result['lookup_results'][0]['result']['ok'] is True
        assert result['first_effective_ms'] is not None
        assert result['first_model_text_ms'] is None and result['first_useful_ms'] is None
        assert result['presentation_source'] == 'error_template'
    finally:
        await gateway.close()


async def test_usage_aliases_preserve_unreported_counts_as_unknown(make_gateway):
    class Model:
        async def stream(self, *args, **kwargs):
            yield {'type': 'delta', 'text': '你好'}
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': '你好'},
                   'meta': {'usage': {'input_tokens': 4, 'total_tokens': 8}}}
    gateway = make_gateway(Model())
    try:
        result = await gateway.turn({'input': '你好'}, 'warm', 'driver')
        assert result['model_calls'] == 1
        assert result['usage'] == {'prompt_tokens': 4, 'total_tokens': 8}
    finally:
        await gateway.close()


async def test_unknown_lookup_stops_and_reports_reconciliation_without_second_model(make_gateway):
    class Model:
        async def stream(self, *args, **kwargs):
            yield {'type': 'message', 'message': {'role': 'assistant', 'tool_calls': [
                call('get_weather', {'location': '巴黎', 'date': 'today'})]}}
    gateway = make_gateway(Model(), lookup_fixtures={'get_weather': [{'ok': False, 'outcome_unknown': True}]})
    try:
        result = await gateway.turn({'input': '巴黎今天天气怎么样'}, 'warm', 'driver')
        assert result['model_calls'] == 1 and result['lookups'] == ['get_weather']
        assert result['error'] == 'outcome_unknown' and result['outcome_unknown'] is True
        assert result['first_useful_ms'] is None
    finally:
        await gateway.close()


async def test_query_provider_error_is_completed_failure_observation_and_keeps_model_metrics(make_gateway):
    model = ScriptedModel([{'tool_calls': [call('search_manual', {'query': '维护说明'})]},
                           {'content': '这次没查到车书资料，暂时无法确认。'}])
    gateway = make_gateway(model)
    async def failed_read(**arguments):
        error = ProviderError('upstream_error', 'offline embedding rejected', provider='fixture',
                              details={'request_id': 'fixture-request', 'api_key': 'synthetic-secret'})
        error.trace_partial = {'attempts': [{'status': 503}]}
        raise error
    gateway.manual.search_manual = failed_read
    try:
        result = await gateway.turn({'input': '查一下维护说明'}, 'professional', 'driver')
        assert result['error'] is None and result['outcome_unknown'] is False
        assert result['model_calls'] == result['talker_model_calls'] == 2
        assert result['text'] == '这次没查到车书资料，暂时无法确认。'
        receipt = result['lookup_results'][0]['result']
        assert result['lookup_results'][0]['arguments'] == {'query': '维护说明'}
        assert receipt['ok'] is False and receipt['executed'] is False and receipt['code'] == 'upstream_error'
        assert receipt['diagnostics'] == {'request_id': 'fixture-request', 'api_key': '[redacted]'}
        assert receipt['partial'] == {'attempts': [{'status': 503}]}
        observations = [record for record in gateway.diagnostics if record['kind'] == 'observations.received']
        assert observations[0]['observations'][0]['status'] == 'completed'
        assert 'synthetic-secret' not in json.dumps(result)
        assert json.loads(model.requests[1]['messages'][-1]['content'])['ok'] is False
    finally:
        await gateway.close()


async def test_eval_consumes_core_presentation_without_adding_a_model_call(make_gateway):
    model = ScriptedModel([{'tool_calls': [call()]}])
    gateway = make_gateway(model)
    try:
        result = await gateway.turn({'input': '把音量调到40'}, 'professional', 'driver')
        terminal = next(record['event'] for record in gateway.diagnostics
                        if (record.get('event') or {}).get('type') == 'run.completed')
        assert result['text'] == terminal['data']['presentation']['speech']
        assert '40' in result['text'] and result['model_calls'] == 1
        assert result['first_useful_ms'] is not None
        assert result['first_model_text_ms'] is None
        assert result['presentation_source'] == 'verified_receipts'
        assert result['first_effective_ms'] is not None
        assert gateway.history[-1].records[-1].content == result['text']
    finally:
        await gateway.close()


async def test_resume_failure_after_real_execution_retains_full_failed_turn_statistics(make_gateway, monkeypatch):
    from evals import harness
    secret = 'synthetic-private-adapter-key'
    suffix = 'complete diagnostic tail ' + 'x' * 300
    preface = '正在把音量调到40。'

    class MeteredModel:
        secrets = (secret,)
        calls = 0
        async def stream(self, *args, **kwargs):
            self.calls += 1
            yield {'type': 'delta', 'text': preface}
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': preface,
                'tool_calls': [call()]}, 'meta': {'usage': {'input_tokens': 11, 'output_tokens': 3,
                'total_tokens': 14, 'prompt_tokens_details': {'cached_tokens': 2}}}}

    model = MeteredModel()
    gateway = make_gateway(model)
    resumes = []
    def refuse_resume(request):
        resumes.append(request)
        # The independent device already executed before this boundary failed.
        assert request.observations[0].result['executed'] is True
        assert request.observations[0].result['vehicle_state']['media']['volume'] == 40
        raise RunProtocolError(f'continuation refused {secret}: {suffix}',
            code='checkpoint_size_exceeded', details={'api_key': secret, 'phase': 'after_receipt'})
    monkeypatch.setattr(gateway.service, 'resume', refuse_resume)
    monkeypatch.setattr(harness, 'EvaluationGateway', lambda *args, **kwargs: gateway)
    result = await harness.run_case({'id': 'failed-after-device-receipt', 'turns': [
        {'input': '把音量调到40', 'expect': {'commands': [{'name': 'set_volume', 'arguments': {'volume': 40}}]}}]}, model)
    turn = result['turns'][0]
    assert not result['passed'] and not turn['passed']
    assert len(resumes) == model.calls == turn['model_calls'] == turn['talker_model_calls'] == 1
    assert turn['model_calls_by_role'] == {'talker': 1}
    assert turn['usage'] == {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14, 'cached_tokens': 2}
    assert turn['commands'] == [{'name': 'set_volume', 'arguments': {'volume': 40}, 'source': 'talker'}]
    assert turn['text'] == turn['preface'] == preface
    assert turn['first_effective_ms'] is not None and turn['first_useful_ms'] is not None
    assert turn['error'] == 'checkpoint_size_exceeded' and turn['error_type'] == 'RunProtocolError'
    assert turn['outcome_unknown'] is True and turn['diagnostic'].endswith(suffix)
    assert turn['exception']['details'] == {'api_key': '[redacted]', 'phase': 'after_receipt'}
    assert secret not in json.dumps(result)
    kinds = [row['kind'] for row in result['traces']]
    assert kinds.index('model.started') < kinds.index('vehicle.receipt') < kinds.index('turn.failed')
    assert kinds.count('vehicle.dispatched') == kinds.count('vehicle.receipt') == kinds.count('turn.failed') == 1
    assert harness.summarize([result])['latency']['all']['model_calls_mean'] == 1
    # Observer reads never resume, replay an action, or append another failure.
    snapshot = gateway.observe_turn()
    assert snapshot['vehicle_before']['media']['volume'] == 30 and snapshot['vehicle']['media']['volume'] == 40
    again = gateway.failed_turn(RuntimeError('do not overwrite the original failure'))
    assert again == snapshot and len(resumes) == model.calls == 1
    snapshot['trace'].clear()
    assert gateway.observe_turn()['trace']


async def test_failed_observer_retains_malformed_model_proposals_without_parsing_them_as_actions(make_gateway):
    malformed = call()
    malformed['function']['arguments'] = '{bad json'
    model = ScriptedModel([{'tool_calls': [malformed]}])
    gateway = make_gateway(model)
    try:
        result = await gateway.turn({'input': '音量调到40'}, 'professional', 'driver')
        assert result['error'] == 'invalid_tool_arguments' and result['model_calls'] == 1
        assert result['talker_calls'][0]['arguments'] == '{bad json'
        assert result['commands'] == []
    finally:
        await gateway.close()
