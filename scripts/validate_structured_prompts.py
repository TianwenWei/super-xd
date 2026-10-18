"""Capture and replay first-decision cases using configured model providers.

Capture performs no network requests or tool execution. Replay preserves every
attempt; it never retries a failed case. Structural checks are automatic while
free-text meaning and goal preservation remain explicitly marked for review.
Replay keeps the captured model/options, selecting credentials and endpoints
from the current service configuration. It never substitutes a role's model.
"""
from __future__ import annotations
from super_xd.nlp.contracts.providers import ModelRequest

if __name__ == "__main__":
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def model_role(case_role):
    # Quick lookup deliberately uses the Talker model, matching TaskRunner's
    # non-reasoning fast path. Planner/finalizer must retain separate profiles.
    roles = {'talker': 'talker', 'thinker_quick': 'talker', 'thinker_research': 'thinker',
             'thinker': 'thinker', 'vehicle_planner': 'planner', 'planner': 'planner',
             'finalizer': 'finalizer'}
    if case_role not in roles:
        raise ValueError('Unknown capture role; cannot choose a model profile')
    return roles[case_role]


def import_runtime():
    return SimpleNamespace(task_runner=importlib.import_module('super_xd.nlp.orchestration.tasks.runner'),
                           research=importlib.import_module('super_xd.nlp.prompts.research'),
                           gateway=importlib.import_module('super_xd.nlp.orchestration.gateway'),
                           wire=importlib.import_module('super_xd.nlp.contracts.gateway_v1'),
                           policy=importlib.import_module('super_xd.nlp.policy.product'),
                           fixtures=importlib.import_module('super_xd.demo.devices.simulator'),
                           resources=importlib.import_module('super_xd.demo.resources.runtime'),
                           local_gateway=importlib.import_module('super_xd.demo.gateway'),
                           catalog=importlib.import_module('super_xd.nlp.catalog.vehicle'),
                           persona=importlib.import_module('super_xd.nlp.prompts.persona'),
                           models=importlib.import_module('super_xd.nlp.adapters.models.service'),
                           settings=importlib.import_module('super_xd.nlp.defaults'))


class CaptureOnlyModel:
    async def stream(self, *args, **kwargs):
        raise AssertionError('Offline capture must compile without invoking a model')
        yield


def set_path(state, path, value):
    parts = path.split('.')
    node = state
    for key in parts[:-1]:
        node = node[key]
    node[parts[-1]] = deepcopy(value)


def capture_start_request(runtime, case, fixed_time):
    """Build a factual Gateway input from explicit, entirely offline fixtures."""
    vehicle = runtime.fixtures.initial_vehicle_state()
    for path, value in case.get('vehicle_overrides', {}).items():
        set_path(vehicle, path, value)
    meta = vehicle['_meta']
    tasks = deepcopy(case.get('tasks', []))
    for task in tasks:
        task.setdefault('created_at', fixed_time)
    tasks.sort(key=lambda task: task['created_at'])
    tasks = [{key: value for key, value in task.items() if key in runtime.wire.TaskReference.model_fields}
             | {'context': {**task.get('context', {}), 'constraints': task.get('constraints', [])}} for task in tasks]
    history = []
    for index, previous in enumerate(case.get('history', [])):
        messages = deepcopy(previous.get('messages') or [
            {'role': 'user', 'content': previous['user']},
            {'role': 'assistant', 'content': previous['assistant']}])
        records = runtime.local_gateway.history_records(messages)
        pending = {call['id'] for record in records if record['role'] == 'assistant' for call in record.get('tool_calls', [])}
        pending.difference_update(record['tool_call_id'] for record in records if record['role'] == 'tool')
        history.append({'turn_id': f'history-{index}', 'records': records,
                        'speaker_seat': previous.get('speaker_seat', 'driver'), 'pending_tool_group': bool(pending),
                        'interrupted_playback': previous.get('interrupted_playback', False),
                        'heard_text': previous.get('heard_text', ''),
                        'playback_status': previous.get('playback_status',
                            ('partial' if previous.get('heard_text') else 'unknown')
                            if previous.get('interrupted_playback') else 'completed')})
    fixture_provider = SimpleNamespace(info=lambda: {'configured': True, 'execution_location': 'cloud'})
    capabilities, bindings = runtime.resources.LocalResources(
        maps=fixture_provider, web=fixture_provider, manual=fixture_provider).declarations()
    capabilities.sort(key=lambda item: (item.name, item.action or '', item.content_kind or ''))
    places = [{'place_ref': place.get('place_ref') or 'fixture:' + place['place_id'],
               'name': place['name'], 'provider': place.get('provider', 'fixture'),
               'source_turn_id': 'history-0', 'presented_to_user': True, 'details': deepcopy(place)}
              for place in case.get('recent_places', [])]
    request = runtime.wire.StartRequest.model_validate({
        'identity': {'run_id': 'capture-' + case['id'], 'request_id': case['id'], 'turn_id': case['id'], 'context_revision': 1},
        'received_at': fixed_time, 'user_text': case['text'], 'speaker_seat': case.get('speaker_seat', runtime.settings.DEFAULT_SPEAKER_SEAT),
        'persona_id': case.get('persona', 'warm'), 'history': history,
        'context': {'revision': 1, 'observed_at': fixed_time,
            'vehicle': {'revision': 1, 'observed_at': fixed_time, 'state': vehicle},
            'capabilities': {'revision': 1, 'observed_at': fixed_time, 'tools': [item.model_dump() for item in capabilities],
                'features': {name: True for name in runtime.catalog.TOOLS}, 'cockpit': {'configured': False}},
            'environment': {'timezone': meta['timezone'], 'location':
                {'name': meta['location'], 'country': meta.get('location_country', ''), 'source': 'user_setting'} if meta.get('location') else None},
            'tasks': tasks, 'places': places}})
    return request, bindings


async def capture_case(runtime, case, fixed_time):
    if case['role'] not in ('talker', 'thinker_quick', 'thinker_research'):
        raise ValueError('Offline capture supports Talker and Thinker first decisions only')
    if case['role'].startswith('thinker'):
        task_runner = runtime.task_runner
        mode = 'quick' if case['role'] == 'thinker_quick' else 'research'
        task = {'goal': case['text'], 'constraints': [], 'revision': 1,
                'context': {'query_mode': mode, 'query_domain': 'weather' if mode == 'quick' else 'general'}}
        instance = object.__new__(task_runner.TaskRunner)
        messages = instance._messages(task, current_utc=fixed_time)
        tools = deepcopy(runtime.research.QUICK_TOOLS if mode == 'quick' else runtime.research.THINKER_TOOLS)
        if mode == 'quick':
            tools = [item for item in tools if item['function']['name'] in {'get_weather_forecast', 'ask_clarification'}]
        return {'messages': messages, 'tools': tools, 'thinker': mode == 'research',
                'stream': False, 'max_tokens': 900 if mode == 'quick' else 6000}
    request, bindings = capture_start_request(runtime, case, fixed_time)
    service = runtime.gateway.NLPService(CaptureOnlyModel(), model_policy_id='offline-capture',
        semantic_policy=runtime.policy.ProductTalkerPolicy(bindings,
            enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management']))
    prepared = service.compile_start(request)
    return {'messages': prepared.messages, 'tools': prepared.tools,
            'thinker': False, 'stream': True, 'max_tokens': 900}


async def capture(args):
    from super_xd.nlp.config.services import load_services_config, resolved
    suite = json.loads(Path(args.cases).read_text())
    runtime = import_runtime()
    config = load_services_config()
    cases = []
    for case in suite['cases']:
        request = await capture_case(runtime, case, suite['fixed_current_utc'])
        role = model_role(case['role'])
        fallback = runtime.settings.THINKER_MODEL if request['thinker'] else runtime.settings.TALKER_MODEL
        request['model'] = resolved(config.role(role), 'model', fallback)
        request['temperature'] = 0
        request['thinking'] = {'type': 'enabled' if request['thinker'] else 'disabled'}
        cases.append({**case, 'request': request, 'request_sha256': digest(request), 'tools_sha256': digest(request['tools'])})
    output = {'kind': 'synthetic_prompt_decision_capture', 'version': suite['version'],
              'captured_at': datetime.now(timezone.utc).isoformat(), 'fixed_current_utc': suite['fixed_current_utc'],
              'scope': suite['scope'], 'source': str(ROOT),
              'provider_calls': 0, 'tool_execution': False, 'cases': cases}
    with Path(args.output).open('x') as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'output': args.output, 'cases': len(cases), 'provider_calls': 0}, ensure_ascii=False))


def _message_size(request):
    messages = request['messages']
    return {'messages': len(messages),
            'content_characters': sum(len(message.get('content') or '') for message in messages),
            'system_characters': sum(len(message.get('content') or '') for message in messages if message['role'] == 'system'),
            'serialized_characters': len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')))}


def compare_captures(before, after, examples):
    """Compare inputs offline; character savings do not imply model quality."""
    if before['fixed_current_utc'] != after['fixed_current_utc']:
        raise ValueError('Capture clocks differ; use the same suite fixed_current_utc')
    before_ids = [(case['id'], case['role']) for case in before['cases']]
    after_ids = [(case['id'], case['role']) for case in after['cases']]
    if not before_ids or before_ids != after_ids or len(set(before_ids)) != len(before_ids):
        raise ValueError('Captures must contain the same nonempty ordered case identities')
    rows, totals = [], {}
    for previous, current in zip(before['cases'], after['cases']):
        old, new = previous['request'], current['request']
        old_options = {key: value for key, value in old.items() if key not in {'messages', 'tools'}}
        new_options = {key: value for key, value in new.items() if key not in {'messages', 'tools'}}
        old_size, new_size = _message_size(old), _message_size(new)
        row = {'id': current['id'], 'role': current['role'], 'before': old_size, 'after': new_size,
               'delta': {key: new_size[key] - old_size[key] for key in old_size},
               'case_data_identical': {key: value for key, value in previous.items() if key not in {'request', 'request_sha256', 'tools_sha256'}} ==
                                      {key: value for key, value in current.items() if key not in {'request', 'request_sha256', 'tools_sha256'}},
               'model_options_identical': old_options == new_options,
               'tools_identical': old['tools'] == new['tools'],
               'non_system_messages_identical': [m for m in old['messages'] if m['role'] != 'system'] ==
                                                [m for m in new['messages'] if m['role'] != 'system']}
        if current['role'] == 'talker':
            row['native_examples_preserved'] = (old['messages'][2:2 + len(examples)] == examples and
                                                new['messages'][2:2 + len(examples)] == examples)
        rows.append(row)
        total = totals.setdefault(current['role'], {'cases': 0, 'before': dict.fromkeys(old_size, 0),
                                                   'after': dict.fromkeys(new_size, 0)})
        total['cases'] += 1
        for key in old_size:
            total['before'][key] += old_size[key]
            total['after'][key] += new_size[key]
    for total in totals.values():
        total['delta'] = {key: total['after'][key] - total['before'][key] for key in total['before']}
    return {'kind': 'offline_prompt_input_comparison', 'fixed_current_utc': after['fixed_current_utc'],
            'provider_calls': 0, 'cases': len(rows), 'by_role': totals,
            'case_data_identical': all(row['case_data_identical'] for row in rows),
            'model_options_identical': all(row['model_options_identical'] for row in rows),
            'tools_identical': all(row['tools_identical'] for row in rows),
            'non_system_messages_identical': all(row['non_system_messages_identical'] for row in rows),
            'native_example_messages': len(examples), 'native_examples_sha256': digest(examples),
            'native_examples_checked_cases': sum(row['role'] == 'talker' for row in rows),
            'native_examples_preserved': all(row.get('native_examples_preserved', True) for row in rows),
            'limitations': 'Character/message comparison only; model behavior and provider token usage were not evaluated.',
            'results': rows}


def compare(args):
    from super_xd.nlp.prompts.templates import load_examples
    before, after = (json.loads(Path(path).read_text()) for path in (args.before, args.after))
    report = compare_captures(before, after, load_examples())
    report['inputs'] = {'before': args.before, 'after': args.after}
    with Path(args.output).open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}, ensure_ascii=False, indent=2))
    return 0 if all(report[key] for key in ('case_data_identical', 'model_options_identical', 'tools_identical',
                                           'non_system_messages_identical', 'native_examples_preserved')) else 1


def parsed_calls(message):
    result = []
    for call in message.get('tool_calls', []):
        function = call.get('function', {})
        raw = function.get('arguments', '{}')
        arguments = json.loads(raw) if isinstance(raw, str) else raw
        result.append({'name': function.get('name'), 'arguments': arguments})
    return result


def structural_check(case, message, tools):
    errors = []
    try:
        actual = parsed_calls(message)
    except (ValueError, TypeError) as error:
        return False, [], [f'Invalid function arguments: {type(error).__name__}']
    schemas = {tool['function']['name']: tool['function']['parameters'] for tool in tools}
    for call in actual:
        if call['name'] not in schemas:
            errors.append(f'Function unavailable: {call["name"]}')
        else:
            for error in Draft202012Validator(schemas[call['name']]).iter_errors(call['arguments']):
                errors.append(f'Parameter schema: {call["name"]}: {error.message}')
    expected = case['expected']
    if 'allowed_names' in expected:
        if len(actual) < expected.get('min_calls', 0):
            errors.append('Too few tool calls')
        if any(call['name'] not in expected['allowed_names'] for call in actual):
            errors.append('Unexpected research decision')
    else:
        wanted = expected.get('calls', [])
        if Counter(item['name'] for item in actual) != Counter(item['name'] for item in wanted):
            errors.append('Tool call set differs')
        remaining = deepcopy(actual)
        for target in wanted:
            match = next((item for item in remaining if item['name'] == target['name'] and (
                item['arguments'] == target['arguments'] if 'arguments' in target else
                all(item['arguments'].get(key) == value for key, value in target.get('argument_subset', {}).items()))), None)
            if match is None:
                errors.append(f'Expected parameters absent: {target["name"]}')
            else:
                remaining.remove(match)
    if case.get('response_json') is not None:
        try:
            content = json.loads(message.get('content') or '')
        except ValueError:
            content = None
        if content != case['response_json']:
            errors.append('Speech routing JSON differs')
    elif not actual and not str(message.get('content') or '').strip():
        errors.append('No tool call and no answer')
    return not errors, actual, errors


def replay_request(llm, request, case_role):
    """Use the captured experiment exactly, without production role overrides."""
    from super_xd.nlp.contracts.providers import ModelRequest
    supported = {'messages', 'tools', 'thinker', 'stream', 'model', 'max_tokens', 'temperature', 'thinking', 'tool_choice'}
    if request.keys() - supported:
        raise ValueError('Capture contains unsupported model request options')
    if (not isinstance(request.get('model'), str) or not request['model'].strip()
            or type(request.get('max_tokens')) is not int or request['max_tokens'] <= 0
            or type(request.get('thinker')) is not bool or type(request.get('stream')) is not bool):
        raise ValueError('Capture requires explicit model, token budget, thinker and stream options')
    if type(request.get('temperature')) not in (int, float) or request['temperature'] != 0:
        raise ValueError('Configured adapters support only temperature=0 for capture replay')
    thinking = request.get('thinking')
    if not isinstance(thinking, dict) or set(thinking) != {'type'} or thinking['type'] not in ('enabled', 'disabled'):
        raise ValueError('Capture requires an explicit thinking setting')
    role = model_role(case_role)
    profile = llm.config.role(role)['profile']
    settings = llm.config.document['llm']['profiles'][profile]
    if thinking['type'] == 'enabled' and settings['provider'] == 'openai_compatible' and not settings.get('reasoning_field'):
        raise ValueError('Selected provider cannot represent captured enabled reasoning')
    exact = ModelRequest(request['messages'], request['tools'], role, request['model'],
                       thinking['type'] == 'enabled', request['max_tokens'], request.get('tool_choice'))
    return llm.providers[profile], exact, {'profile': profile, 'provider': settings['provider'],
                                         'model': exact.model, 'role': role}


async def replay(args):
    from super_xd.nlp.adapters.common import sanitized_error
    from super_xd.nlp.adapters.factory import create_llm
    from super_xd.nlp.contracts.providers import redact
    if type(args.concurrency) is not int or args.concurrency <= 0:
        raise ValueError('Replay concurrency must be a positive integer')
    source = Path(args.input)
    capture_data = json.loads(source.read_text())
    report = {'kind': 'configured_model_prompt_first_decision_replay', 'input': str(source),
              'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'created_at': datetime.now(timezone.utc).isoformat(), 'tool_execution': False,
              'real_vehicle_receipts': False, 'real_asr_tts': False, 'retried_cases': 0,
              'scope': capture_data['scope'], 'attempts': []}
    destination = Path(args.output)
    with destination.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    semaphore = asyncio.Semaphore(args.concurrency)
    llm = create_llm()
    report['model_configuration'] = {'source': llm.config.source}
    secrets = tuple(secret for provider in llm.providers.values() for secret in getattr(provider, 'secrets', ()))
    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    async def one(case):
        async with semaphore:
            request = case['request']
            item = {'id': case['id'], 'role': case['role'], 'text': case['text'], 'expected': case['expected'],
                    'request_sha256': case['request_sha256'], 'request': request,
                    'manual_review_required': case.get('manual_review'), 'automatic_pass': False}
            report['attempts'].append(item)
            start = time.perf_counter()
            try:
                provider, exact, route = replay_request(llm, request, case['role'])
                item['model_execution'] = route
                if request['stream']:
                    async for event in provider.stream(exact):
                        if event['type'] == 'message':
                            item['response'] = event
                    message = item.get('response', {}).get('message', {})
                else:
                    result = await provider.complete(exact)
                    message = result.message
                    item['response'] = {'message': message, 'meta': result.metadata}
                item['automatic_pass'], item['actual_calls'], item['errors'] = structural_check(case, message, request['tools'])
            except Exception as error:
                item['error'] = sanitized_error(error, secrets)
                if getattr(error, 'trace_partial', None):
                    item['partial'] = redact(error.trace_partial, secrets)
            item['elapsed_ms'] = (time.perf_counter() - start) * 1000
            save()
            print(json.dumps({key: item.get(key) for key in ('id', 'automatic_pass', 'actual_calls', 'errors', 'error', 'elapsed_ms')}, ensure_ascii=False), flush=True)
    try:
        selected = [case for case in capture_data['cases'] if not args.only or case['id'] in args.only]
        await asyncio.gather(*(one(case) for case in selected))
    finally:
        await llm.aclose()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['summary'] = {'attempted': len(report['attempts']), 'automatic_passed': sum(item['automatic_pass'] for item in report['attempts']),
                             'manual_reviews_required': sum(bool(item['manual_review_required']) for item in report['attempts']),
                             'errors': sum('error' in item for item in report['attempts'])}
        save()
    print(json.dumps({'output': str(destination), **report['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    capture_parser = sub.add_parser('capture')
    capture_parser.add_argument('--cases', default='evals/structured_prompt_cases.json')
    capture_parser.add_argument('--output', required=True)
    compare_parser = sub.add_parser('compare', help='Compare offline captures from separate checkouts')
    compare_parser.add_argument('--before', required=True)
    compare_parser.add_argument('--after', required=True)
    compare_parser.add_argument('--output', required=True)
    replay_parser = sub.add_parser('replay')
    replay_parser.add_argument('--input', required=True)
    replay_parser.add_argument('--output', required=True)
    replay_parser.add_argument('--concurrency', type=int, choices=[1, 2, 3], default=3)
    replay_parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    if args.action == 'compare':
        raise SystemExit(compare(args))
    else:
        asyncio.run(capture(args) if args.action == 'capture' else replay(args))
