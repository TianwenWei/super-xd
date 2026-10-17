"""Capture and replay first-decision cases using configured model providers.

Capture performs no network requests or tool execution. Replay preserves every
attempt; it never retries a failed case. Structural checks are automatic while
free-text meaning and goal preservation remain explicitly marked for review.
Replay keeps the captured model/options, selecting credentials and endpoints
from the current service configuration. It never substitutes a role's model.
"""
from __future__ import annotations

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
    return SimpleNamespace(task_runner=importlib.import_module('backend.harness.task_runner'),
                           **{name: importlib.import_module(f'backend.{name}') for name in
                              ('session', 'catalog', 'persona', 'models', 'settings')})


class CaptureArk:
    async def stream(self, messages, tools):
        self.request = {'messages': deepcopy(messages), 'tools': deepcopy(tools)}
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'capture-only'}}


def set_path(state, path, value):
    parts = path.split('.')
    node = state
    for key in parts[:-1]:
        node = node[key]
    node[parts[-1]] = deepcopy(value)


async def capture_case(runtime, case, fixed_time):
    if case['role'] not in ('talker', 'thinker_quick', 'thinker_research'):
        raise ValueError('Offline capture supports Talker and Thinker first decisions only')
    session = runtime.session
    session.utcnow = lambda: fixed_time
    if case['role'].startswith('thinker'):
        task_runner = runtime.task_runner
        mode = 'quick' if case['role'] == 'thinker_quick' else 'research'
        task = {'goal': case['text'], 'constraints': [], 'revision': 1,
                'context': {'query_mode': mode, 'query_domain': 'weather' if mode == 'quick' else 'general'}}
        instance = object.__new__(task_runner.TaskRunner)
        messages = instance._messages(task, current_utc=fixed_time)
        tools = deepcopy(task_runner.QUICK_TOOLS if mode == 'quick' else task_runner.THINKER_TOOLS)
        if mode == 'quick':
            tools = [item for item in tools if item['function']['name'] in {'get_weather_forecast', 'ask_clarification'}]
        return {'messages': messages, 'tools': tools, 'thinker': mode == 'research',
                'stream': False, 'max_tokens': 900 if mode == 'quick' else 6000}
    actor = object.__new__(session.SessionActor)
    actor.id = 'synthetic-prompt-evaluation'
    actor.speaker_seat = case.get('speaker_seat', session.DEFAULT_SPEAKER_SEAT)
    tasks = deepcopy(case.get('tasks', []))
    for task in tasks:
        # Synthetic tasks need the same ordering field supplied by the real ledger.
        task.setdefault('created_at', fixed_time)
    actor.db = SimpleNamespace(list_tasks=lambda session_id: deepcopy(tasks))
    actor.tasks = {task['id']: deepcopy(task) for task in tasks}
    actor.vehicle = runtime.catalog.initial_vehicle_state()
    for path, value in case.get('vehicle_overrides', {}).items():
        set_path(actor.vehicle, path, value)
    actor.recent_places = deepcopy(case.get('recent_places', []))
    from backend.cockpit import initial_cockpit_state
    actor.cockpit = None
    actor.cockpit_state = initial_cockpit_state()
    actor.media_choices = {}
    actor.persona = runtime.persona.PERSONAS[case.get('persona', 'warm')].model_copy(deep=True)
    actor.ark = CaptureArk()
    actor.turns = []
    for index, previous in enumerate(case.get('history', [])):
        turn = session.Turn(f'history-{index}', previous['user'], actor.persona.model_copy(deep=True), 1)
        turn.messages = deepcopy(previous.get('messages') or [
            {'role': 'user', 'content': previous['user']},
            {'role': 'assistant', 'content': previous['assistant']}])
        turn.finished = True
        actor.turns.append(turn)
    turn = session.Turn(case['id'], case['text'], actor.persona.model_copy(deep=True), 1)
    turn.messages = [{'role': 'user', 'content': case['text']}]
    if case.get('speech_mode'):
        turn.audio_queue = asyncio.Queue()  # Voice and text now share one request shape.
    actor.turns.append(turn)
    actor.current = turn
    actor.trace = lambda *args, **kwargs: None
    actor.spawn = asyncio.create_task
    async def ignore_post(event):
        pass
    actor.post = ignore_post
    actor.launch_model(turn)
    await turn.model_task
    return {**actor.ark.request, 'thinker': False, 'stream': True, 'max_tokens': 900}


async def capture(args):
    from backend.services.config import load_services_config, resolved
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
    from backend.prompt_templates import load_examples
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
    from backend.services.contracts import LLMRequest
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
    exact = LLMRequest(request['messages'], request['tools'], role, request['model'],
                       thinking['type'] == 'enabled', request['max_tokens'], request.get('tool_choice'))
    return llm.providers[profile], exact, {'profile': profile, 'provider': settings['provider'],
                                         'model': exact.model, 'role': role}


async def replay(args):
    from backend.models import sanitized_error
    from backend.services.factory import create_llm
    from backend.services.contracts import redact
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
