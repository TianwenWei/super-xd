"""Capture and replay synthetic first-decision cases against real Ark models.

Capture performs no network requests or tool execution. Replay preserves every
attempt; it never retries a failed case. Structural checks are automatic while
free-text meaning and goal preservation remain explicitly marked for review.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def import_runtime(source):
    if source:
        base = Path(source)
        for name in ('prompt_templates', 'catalog', 'maps_tools', 'models', 'persona', 'vehicle_planner', 'session', 'worker'):
            filename = base / 'backend' / f'{name}.py'
            spec = importlib.util.spec_from_file_location(f'backend.{name}', filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
    return SimpleNamespace(**{name: importlib.import_module(f'backend.{name}') for name in
                              ('session', 'catalog', 'persona', 'worker', 'models', 'settings')})


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
    session = runtime.session
    session.utcnow = lambda: fixed_time
    if case['role'].startswith('thinker'):
        worker = runtime.worker
        parsed_time = datetime.fromisoformat(fixed_time)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return parsed_time.astimezone(tz or timezone.utc)
        worker.datetime = FrozenDatetime
        mode = 'quick' if case['role'] == 'thinker_quick' else 'research'
        task = {'goal': case['text'], 'constraints': [], 'revision': 1,
                'context': {'query_mode': mode, 'query_domain': 'weather' if mode == 'quick' else 'general'}}
        instance = object.__new__(worker.ThinkerWorker)
        messages = instance._messages(task)
        tools = deepcopy(worker.QUICK_TOOLS if mode == 'quick' else worker.THINKER_TOOLS)
        if mode == 'quick':
            tools = [item for item in tools if item['function']['name'] in {'get_weather_forecast', 'ask_clarification'}]
        return {'messages': messages, 'tools': tools, 'thinker': mode == 'research',
                'stream': False, 'max_tokens': 900 if mode == 'quick' else 6000}
    actor = object.__new__(session.SessionActor)
    actor.id = 'synthetic-prompt-evaluation'
    actor.db = SimpleNamespace(list_tasks=lambda session_id: deepcopy(case.get('tasks', [])))
    actor.vehicle = runtime.catalog.initial_vehicle_state()
    for path, value in case.get('vehicle_overrides', {}).items():
        set_path(actor.vehicle, path, value)
    actor.recent_places = deepcopy(case.get('recent_places', []))
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
    turn.setting_clauses = session.explicit_setting_clauses(case['text'])
    if case.get('speech_mode') in {'route', 'answer'}:
        turn.audio_queue = asyncio.Queue()
    if case.get('speech_mode') == 'answer':
        turn.chat_streaming = True
        turn.model_calls = 1
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
    suite = json.loads(Path(args.cases).read_text())
    runtime = import_runtime(args.source)
    cases = []
    for case in suite['cases']:
        request = await capture_case(runtime, case, suite['fixed_current_utc'])
        request['model'] = runtime.settings.THINKER_MODEL if request['thinker'] else runtime.settings.TALKER_MODEL
        request['temperature'] = 0
        request['thinking'] = {'type': 'enabled' if request['thinker'] else 'disabled'}
        cases.append({**case, 'request': request, 'request_sha256': digest(request), 'tools_sha256': digest(request['tools'])})
    output = {'kind': 'synthetic_prompt_decision_capture', 'version': suite['version'],
              'captured_at': datetime.now(timezone.utc).isoformat(), 'fixed_current_utc': suite['fixed_current_utc'],
              'scope': suite['scope'], 'source': args.source or 'current-workspace',
              'provider_calls': 0, 'tool_execution': False, 'cases': cases}
    with Path(args.output).open('x') as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'output': args.output, 'cases': len(cases), 'provider_calls': 0}, ensure_ascii=False))


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


async def replay(args):
    from backend.models import ArkClient, sanitized_error
    source = Path(args.input)
    capture_data = json.loads(source.read_text())
    report = {'kind': 'real_ark_prompt_first_decision_replay', 'input': str(source),
              'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'created_at': datetime.now(timezone.utc).isoformat(), 'tool_execution': False,
              'real_vehicle_receipts': False, 'real_asr_tts': False, 'retried_cases': 0,
              'scope': capture_data['scope'], 'attempts': []}
    destination = Path(args.output)
    with destination.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    semaphore = asyncio.Semaphore(args.concurrency)
    ark = ArkClient()
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
                if request['stream']:
                    async for event in ark.stream(request['messages'], request['tools']):
                        if event['type'] == 'message':
                            item['response'] = event
                    message = item.get('response', {}).get('message', {})
                else:
                    message = await ark.complete(request['messages'], request['tools'], thinker=request['thinker'],
                                                 model=request['model'], thinking=request['thinking']['type'] == 'enabled',
                                                 max_tokens=request['max_tokens'])
                    item['response'] = {'message': message}
                item['automatic_pass'], item['actual_calls'], item['errors'] = structural_check(case, message, request['tools'])
            except Exception as error:
                item['error'] = sanitized_error(error)
                if getattr(error, 'trace_partial', None):
                    item['partial'] = error.trace_partial
            item['elapsed_ms'] = (time.perf_counter() - start) * 1000
            save()
            print(json.dumps({key: item.get(key) for key in ('id', 'automatic_pass', 'actual_calls', 'errors', 'error', 'elapsed_ms')}, ensure_ascii=False), flush=True)
    try:
        selected = [case for case in capture_data['cases'] if not args.only or case['id'] in args.only]
        await asyncio.gather(*(one(case) for case in selected))
    finally:
        await ark.aclose()
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
    capture_parser.add_argument('--source', help='Baseline directory containing backend source files')
    capture_parser.add_argument('--cases', default='evals/structured_prompt_cases.json')
    capture_parser.add_argument('--output', required=True)
    replay_parser = sub.add_parser('replay')
    replay_parser.add_argument('--input', required=True)
    replay_parser.add_argument('--output', required=True)
    replay_parser.add_argument('--concurrency', type=int, choices=[1, 2, 3], default=3)
    replay_parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    asyncio.run(capture(args) if args.action == 'capture' else replay(args))
