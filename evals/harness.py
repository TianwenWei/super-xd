"""Behavior eval runner: real Talker/Planner models, simulated vehicle, structured grading.

Each case drives an in-process ``SessionActor`` exactly like the browser does:
input → vehicle.command → simulator receipt → vehicle.receipt. Grading only
compares structured data (dispatched commands, Talker tool calls, vehicle state,
created tasks). Nothing here reads the assistant's prose to decide pass/fail.

    .venv/bin/python -m evals.harness                 # all cases, needs ARK_API_KEY
    .venv/bin/python -m evals.harness --tag hold      # subset
    .venv/bin/python -m evals.harness --out data/evals/run.json
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from backend.db import Database
from backend.persona import PERSONAS
from backend.session import SessionActor

CASE_DIR = Path(__file__).resolve().parent / 'cases'
TURN_TIMEOUT = 90


def load_cases(directory=CASE_DIR):
    cases = []
    for path in sorted(directory.glob('*.json')):
        for case in json.loads(path.read_text(encoding='utf-8')):
            case.setdefault('file', path.name)
            cases.append(case)
    ids = [case['id'] for case in cases]
    duplicates = {identity for identity in ids if ids.count(identity) > 1}
    if duplicates:
        raise ValueError(f'duplicate eval case ids: {sorted(duplicates)}')
    return cases


def state_value(state, path):
    node = state
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def set_state_value(state, path, value):
    *parents, leaf = path.split('.')
    node = state
    for key in parents:
        node = node[key]
    if leaf not in node:
        raise KeyError(path)
    node[leaf] = copy.deepcopy(value)


def subset(expected, actual):
    """Expected arguments are a subset of actual; numbers compare by value."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(map(subset, expected, actual))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and expected == actual
    return expected == actual


def matches(spec, call):
    return spec['name'] == call['name'] and subset(spec.get('arguments', {}), call['arguments'])


def grade(expect, observed):
    """Return a list of failure strings; empty means the turn passed."""
    failures = []
    commands, talker = observed['commands'], observed['talker_calls']
    for spec in expect.get('commands', []):
        if not any(matches(spec, item) for item in commands):
            failures.append(f'missing command {spec}')
    if expect.get('commands_exact') and len(commands) != len(expect.get('commands', [])):
        failures.append(f'expected exactly {len(expect.get("commands", []))} commands, got {len(commands)}')
    if expect.get('no_commands') and commands:
        failures.append(f'unexpected commands {[c["name"] for c in commands]}')
    for spec in expect.get('forbid_commands', []):
        hit = [item for item in commands if matches(spec, item)]
        if hit:
            failures.append(f'forbidden command {hit[0]}')
    for spec in expect.get('talker_calls', []):
        if not any(matches(spec, item) for item in talker):
            failures.append(f'missing talker call {spec}')
    for name in expect.get('forbid_lookups', []):
        if name in observed.get('lookups', []):
            failures.append(f'forbidden lookup executed: {name}')
    for name in expect.get('forbid_talker_calls', []):
        if any(item['name'] == name for item in talker):
            failures.append(f'forbidden talker call {name}')
    if 'no_talker_calls' in expect and expect['no_talker_calls'] and talker:
        failures.append(f'unexpected talker calls {[c["name"] for c in talker]}')
    for path, value in expect.get('state', {}).items():
        try:
            actual = state_value(observed['vehicle'], path)
        except KeyError:
            failures.append(f'unknown state path {path}')
            continue
        if not subset(value, actual):
            failures.append(f'state {path}={actual!r}, expected {value!r}')
    for path in expect.get('unchanged', []):
        if state_value(observed['vehicle'], path) != state_value(observed['vehicle_before'], path):
            failures.append(f'state {path} changed')
    if 'tasks_created' in expect and observed['tasks_created'] != expect['tasks_created']:
        failures.append(f'tasks_created={observed["tasks_created"]}, expected {expect["tasks_created"]}')
    if 'route' in expect and observed['route'] != expect['route']:
        failures.append(f'route={observed["route"]}, expected {expect["route"]}')
    if observed.get('error') and not expect.get('allow_error'):
        failures.append(f'turn error {observed["error"]}')
    return failures


async def _drive_turn(actor, outgoing, text):
    """Send one input and play the device role until the turn completes."""
    before = copy.deepcopy(actor.vehicle)
    request_index = len(getattr(actor.ark, 'requests', []))
    await actor.post({'type': 'input', 'text': text})
    commands, cards, turn_id, receipts, lookups = [], [], None, [], []

    async def execute(event):
        receipt = await actor.simulator.execute(event['command_id'])
        await actor.post({'type': 'vehicle.receipt', 'command_id': event['command_id'],
                          'receipt_id': receipt['receipt_id']})

    async with asyncio.timeout(TURN_TIMEOUT):
        while True:
            event = await outgoing.get()
            kind = event['type']
            if kind == 'turn.started' and not event.get('proactive') and turn_id is None:
                turn_id = event['turn_id']
            elif kind == 'vehicle.command' and event.get('turn_id') == turn_id:
                commands.append({'name': event['name'], 'arguments': event['arguments'],
                                 'source': event.get('source', 'talker')})
                receipts.append(asyncio.create_task(execute(event)))
            elif kind == 'lookup.result' and event.get('turn_id') == turn_id:
                lookups.append(event['tool'])
            elif kind == 'ui.card' and event.get('origin_turn_id') == turn_id:
                cards.append(event['task'])
            elif kind == 'stream.done' and event['turn_id'] == turn_id:
                done = event
                break
    await asyncio.gather(*receipts, return_exceptions=True)
    turn = next(item for item in actor.turns if item.id == turn_id)
    talker = []
    for message in turn.messages:
        for call in message.get('tool_calls', []):
            try:
                arguments = json.loads(call['function'].get('arguments') or '{}')
            except ValueError:
                arguments = {'_raw': call['function'].get('arguments')}
            talker.append({'name': call['function']['name'], 'arguments': arguments})
    return {'commands': commands, 'talker_calls': talker, 'lookups': lookups, 'vehicle': copy.deepcopy(actor.vehicle),
            'vehicle_before': before, 'tasks_created': len(cards), 'route': done['metrics'].get('route'),
            'error': done['metrics'].get('error'), 'text': done['text'],
            'model_calls': done['metrics'].get('model_calls'), 'total_ms': done['metrics'].get('total_ms'),
            # The first tool-enabled request is the routing decision invariant 6 is about.
            'decision_input': next((without_clock(r['messages']) for r in getattr(actor.ark, 'requests', [])[request_index:]
                                    if r['tools']), None)}


async def run_case(case, ark, *, persona='warm', planner=None, maps=None, repeat=0):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / 'eval.sqlite3')
        actor = SessionActor(f"eval-{case['id']}", db, RecordingArk(ark), vehicle_planner=planner, maps=maps)
        try:
            outgoing = await actor.attach()
            await outgoing.get()
            # Seeding happens before any input is queued, so no handler races it.
            for path, value in case.get('vehicle', {}).items():
                set_state_value(actor.vehicle, path, value)
                set_state_value(actor.simulator.state, path, value)
            for task in case.get('tasks', []):
                created = await asyncio.to_thread(db.create_task, actor.id, task['goal'], task.get('context', {}))
                if task.get('question'):
                    claimed = await asyncio.to_thread(db.claim_next)
                    await asyncio.to_thread(db.save_task, claimed['id'], claimed['revision'],
                                            status='waiting_clarification', question=task['question'])
                task['_id'] = created['id']
            # Let the actor's event poller mirror seeded tasks before the first input.
            async with asyncio.timeout(5):
                while hasattr(actor, 'tasks') and len(actor.tasks) < len(case.get('tasks', [])) or hasattr(actor, 'tasks') and any(
                        actor.tasks[t['id']]['status'] != t['status'] for t in await asyncio.to_thread(db.list_tasks, actor.id)):
                    await asyncio.sleep(.05)
            if persona != 'warm':
                await actor.post({'type': 'persona.set', 'preset': persona})
            turns = []
            for spec in case['turns']:
                started = time.perf_counter()
                try:
                    observed = await _drive_turn(actor, outgoing, spec['input'])
                    failures = grade(spec.get('expect', {}), observed)
                except TimeoutError:
                    observed, failures = {}, ['turn timed out']
                turns.append({'input': spec['input'], 'passed': not failures, 'failures': failures,
                              'elapsed_ms': round((time.perf_counter() - started) * 1000),
                              **{k: v for k, v in observed.items() if k not in {'vehicle', 'vehicle_before'}}})
            return {'id': case['id'], 'persona': persona, 'repeat': repeat, 'tags': case.get('tags', []),
                    'passed': all(turn['passed'] for turn in turns), 'turns': turns}
        finally:
            await actor.close()


class RecordingArk:
    """Forwards to the real client and keeps every Talker request for inspection."""

    def __init__(self, ark):
        self.ark, self.requests = ark, []

    def stream(self, messages, tools=None):
        self.requests.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        return self.ark.stream(messages, tools)

    def __getattr__(self, name):
        return getattr(self.ark, name)


def without_clock(messages):
    """Drop the snapshot's current_utc so identical inputs compare equal across runs."""
    marker = '"current_utc":"'
    result = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, str) and marker in content:
            start = content.index(marker) + len(marker)
            content = content[:start] + content[content.index('"', start):]
            message = {**message, 'content': content}
        result.append(message)
    return result


def persona_signature(result):
    return [[(c['name'], json.dumps(c['arguments'], sort_keys=True, ensure_ascii=False)) for c in turn.get('talker_calls', [])]
            + [(c['name'], json.dumps(c['arguments'], sort_keys=True, ensure_ascii=False)) for c in turn.get('commands', [])]
            for turn in result['turns']]


async def run_all(cases, ark, *, concurrency=4, planner=None, maps=None, repeat=1):
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case, persona, index):
        async with semaphore:
            return await run_case(case, ark, persona=persona, planner=planner, maps=maps, repeat=index)

    jobs = [one(case, persona, index) for index in range(repeat) for case in cases
            for persona in case.get('personas', ['warm'])]
    results = await asyncio.gather(*jobs)
    # Invariant 6 is checked on the decision call's input: it must be identical
    # under every persona. Differing outputs from identical inputs are sampling
    # noise and are reported, not failed, so they are not mistaken for persona leaks.
    groups = {}
    for result in results:
        groups.setdefault((result['id'], result['repeat']), []).append(result)
    for group in groups.values():
        if len(group) < 2:
            continue
        inputs = {json.dumps([t.get('decision_input') for t in r['turns'][:1]], ensure_ascii=False) for r in group}
        if len(inputs) > 1:
            for result in group:
                result['passed'] = False
                result.setdefault('failures', []).append('persona changed the decision-call input')
        elif len({json.dumps(persona_signature(r)) for r in group}) > 1:
            for result in group:
                result['notes'] = ['tool calls varied across personas with identical input (sampling noise)']
    return results


def summarize(results):
    passed = sum(1 for r in results if r['passed'])
    per_case = {}
    for result in results:
        runs = per_case.setdefault(result['id'], [0, 0])
        runs[0] += result['passed']; runs[1] += 1
    return {'total': len(results), 'passed': passed,
            'pass_rate': round(passed / len(results), 4) if results else None,
            'failed': {identity: f'{ok}/{total}' for identity, (ok, total) in sorted(per_case.items()) if ok < total}}


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tag', action='append', default=[])
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--repeat', type=int, default=1, help='run every case N times to measure flakiness')
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    if not os.getenv('ARK_API_KEY'):  # backend.settings already loaded .env
        sys.exit('ARK_API_KEY 未配置：评测需要真实模型，不使用假回复。')
    from backend.models import ArkClient
    cases = [c for c in load_cases()
             if (not args.tag or set(args.tag) & set(c.get('tags', [])))
             and (not args.case or c['id'] in args.case)]
    ark = ArkClient()
    try:
        results = await run_all(cases, ark, concurrency=args.concurrency, repeat=args.repeat)
    finally:
        await ark.client.aclose()
    report = {'summary': summarize(results), 'results': results}
    for result in results:
        mark = 'PASS' if result['passed'] else 'FAIL'
        print(f"{mark} {result['id']} [{result['persona']} #{result['repeat']}]")
        for turn in result['turns']:
            for failure in turn['failures']:
                print(f'    - {turn["input"]}: {failure}')
        for failure in result.get('failures', []) + result.get('notes', []):
            print(f'    - {failure}')
    print(json.dumps(report['summary'], ensure_ascii=False))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
