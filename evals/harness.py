"""Behavior eval runner: real Talker/Planner models, simulated vehicle, structured grading.

Each case drives an in-process ``SessionActor`` exactly like the browser does:
input → vehicle.command → simulator receipt → vehicle.receipt. Grading only
compares structured data (dispatched commands, Talker tool calls, vehicle state,
created tasks). Nothing here reads the assistant's prose to decide pass/fail.

    .venv/bin/python -m evals.harness                 # all cases, uses configured LLM roles
    .venv/bin/python -m evals.harness --tag hold      # subset
    .venv/bin/python -m evals.harness --out data/evals/run.json
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import copy
from itertools import combinations
import json
import tempfile
import time
from pathlib import Path

from backend.db import Database
from backend.session import SessionActor
from backend.harness.release import get_release_manifest
from backend.harness.diagnostics import safe_diagnostic

CASE_DIR = Path(__file__).resolve().parent / 'cases'
TURN_TIMEOUT = 90
# Only presentation metadata is exempt. Queries, goals, locations, dates and
# target identifiers can change behavior even when their schema type is string.
PERSONA_DISPLAY_FIELDS = {'delegate_task': frozenset({'spoken_goal'})}


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
    if 'model_calls' in expect and observed.get('model_calls') != expect['model_calls']:
        failures.append(f'expected {expect["model_calls"]} model calls, got {observed.get("model_calls")}')
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
    for options in expect.get('talker_call_options', []):
        if not any(matches(spec, item) for spec in options for item in talker):
            failures.append(f'missing talker call from accepted structured options {options}')
    for spec in expect.get('cockpit_commands', []):
        if not any(subset(spec,item) for item in observed.get('cockpit_commands', [])):
            failures.append(f'missing cockpit command {spec}')
    if expect.get('cockpit_commands_exact') and len(observed.get('cockpit_commands', [])) != len(expect.get('cockpit_commands', [])):
        failures.append('unexpected cockpit command count')
    for path,value in expect.get('cockpit_state', {}).items():
        if not subset(value,state_value(observed['cockpit'],path)):
            failures.append(f'cockpit state mismatch: {path}')
    for name in expect.get('forbid_lookups', []):
        if name in observed.get('lookups', []):
            failures.append(f'forbidden lookup executed: {name}')
    if expect.get('manual_pages_any'):
        pages = {hit['page'] for record in observed.get('lookup_results', [])
                 if record.get('tool') == 'search_manual' and record.get('result', {}).get('ok')
                 for hit in record['result'].get('hits', [])}
        if not pages.intersection(expect['manual_pages_any']):
            failures.append(f'manual source pages {sorted(pages)} do not include expected evidence')
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
    await actor.post({'type': 'input', 'text': text})
    commands, cards, turn_id, receipts, lookups, lookup_results = [], [], None, [], [], []
    cockpit_commands, device_queries = [], []

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
            elif kind == 'cockpit.command' and event.get('turn_id') == turn_id:
                # Explicit eval device fixtures; this is not browser playback verification.
                envelope = {key:event[key] for key in ('command_id','request_id','session_id','turn_id','target_device','kind')}
                if event['kind'] == 'control':
                    cockpit_commands.append({'domain':event['domain'],'action':event['action'],'arguments':copy.deepcopy(event['arguments'])})
                    pending = actor.pending_cockpit[event['command_id']]
                    await actor.post({'type':'cockpit.receipt',**envelope,'ok':True,
                        'state_version':event['expected_state_version']+1,'state':copy.deepcopy(pending['expected'])})
                else:
                    device_queries.append({'domain':event['domain'],'action':event['action']})
                    state = actor.cockpit_state['media']
                    await actor.post({'type':'cockpit.receipt',**envelope,'ok':True,
                        'result':{'ok':True,'state':copy.deepcopy(state),'state_version':state['version']}})
            elif kind == 'lookup.result' and event.get('turn_id') == turn_id:
                lookups.append(event['tool'])
                lookup_results.append({'tool': event['tool'], 'result': copy.deepcopy(event['result'])})
            elif kind == 'ui.card' and event.get('origin_turn_id') == turn_id:
                cards.append(event['task'])
            elif kind == 'stream.done' and event['turn_id'] == turn_id:
                done = event
                break
    await asyncio.gather(*receipts, return_exceptions=True)
    turn = next(item for item in actor.turns if item.id == turn_id)
    talker, created_task_indices = [], {}
    for message in turn.messages:
        for call in message.get('tool_calls', []):
            try:
                arguments = json.loads(call['function'].get('arguments') or '{}')
            except ValueError:
                arguments = {'_raw': call['function'].get('arguments')}
            talker.append({'name': call['function']['name'], 'arguments': arguments})
            if call['function']['name'] == 'delegate_task':
                result = turn.tool_results.get(call['id'], {})
                task_id = (result.get('task') or {}).get('id')
                if result.get('ok') is True and isinstance(task_id, str):
                    # Bind to the creating call, not asynchronous card arrival.
                    created_task_indices[task_id] = len(talker) - 1
    return {'turn_id': turn_id, 'cockpit_commands':cockpit_commands,'device_queries':device_queries,
            'cockpit':copy.deepcopy(actor.cockpit_state),'cockpit_device_mode':'explicit_eval_fixture' if actor.cockpit else None,
            'commands': commands, 'talker_calls': talker, 'lookups': lookups, 'lookup_results': lookup_results, 'vehicle': copy.deepcopy(actor.vehicle),
            'vehicle_before': before, 'tasks_created': len(cards), 'route': done['metrics'].get('route'),
            'created_task_indices': created_task_indices,
            'error': done['metrics'].get('error'), 'text': done['text'], 'preface': done['metrics'].get('preface'),
            'model_calls': done['metrics'].get('model_calls'), 'total_ms': done['metrics'].get('total_ms'),
            'first_speech_ms': done['metrics'].get('first_speech_ms')}


async def _case_trace(db, session_id):
    records, cursor = [], 0
    while True:
        batch = await asyncio.to_thread(db.traces_after, session_id, after_id=cursor, limit=500)
        records.extend(batch)
        if len(batch) < 500:
            return records
        cursor = batch[-1]['trace_id']


def _model_secrets(model):
    # Configured profiles may use custom key_env names outside the legacy list.
    # Provider adapters already retain their configured secret for redaction.
    providers = getattr(model, 'providers', {})
    values = [model, *providers.values()] if isinstance(providers, dict) else [model]
    return tuple(secret for provider in values for secret in getattr(provider, 'secrets', ())
                 if isinstance(secret, str) and secret)


async def run_case(case, ark, *, persona='warm', planner=None, maps=None, repeat=0):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / 'eval.sqlite3')
        cockpit = None
        if 'cockpit' in case:
            from backend.cockpit import CockpitServices, LocalMediaCatalog
            cockpit = CockpitServices(LocalMediaCatalog(case['cockpit'].get('items', [])))
        session_id = f"eval-{case['id']}"
        actor = SessionActor(session_id, db, ark, vehicle_planner=planner, maps=maps, cockpit=cockpit)
        task_id_aliases = {}
        try:
            outgoing = await actor.attach()
            await outgoing.get()
            # Seeding happens before any input is queued, so no handler races it.
            for path, value in case.get('vehicle', {}).items():
                set_state_value(actor.vehicle, path, value)
                set_state_value(actor.simulator.state, path, value)
            if 'cockpit' in case:
                from backend.cockpit.media import media_item
                actor.media_choices = {item['content_id']:media_item(item) for item in case['cockpit'].get('items', [])}
                for path,value in case['cockpit'].get('state', {}).items():
                    set_state_value(actor.cockpit_state,path,value)
            for index, task in enumerate(case.get('tasks', [])):
                created = await asyncio.to_thread(db.create_task, actor.id, task['goal'], task.get('context', {}))
                if task.get('state'):
                    await asyncio.to_thread(db.save_task, created['id'], created['revision'], **task['state'])
                if task.get('question'):
                    claimed = await asyncio.to_thread(db.claim_next)
                    await asyncio.to_thread(db.save_task, claimed['id'], claimed['revision'],
                                            status='waiting_clarification', question=task['question'])
                task_id_aliases[created['id']] = f'seeded:{index}'
            # Let the actor's event poller mirror seeded tasks before the first input.
            async with asyncio.timeout(5):
                while hasattr(actor, 'tasks') and len(actor.tasks) < len(case.get('tasks', [])) or hasattr(actor, 'tasks') and any(
                        actor.tasks[t['id']]['status'] != t['status'] for t in await asyncio.to_thread(db.list_tasks, actor.id)):
                    await asyncio.sleep(.05)
            if persona != 'warm':
                await actor.post({'type': 'persona.set', 'preset': persona})
            turns = []
            for turn_index, spec in enumerate(case['turns']):
                if spec.get('speaker_seat') or case.get('speaker_seat'):
                    await actor.post({'type': 'speaker.set', 'seat': spec.get('speaker_seat') or case['speaker_seat']})
                    await actor.queue.join()
                started = time.perf_counter()
                try:
                    observed = await _drive_turn(actor, outgoing, spec['input'])
                    for task_id, index in observed.get('created_task_indices', {}).items():
                        task_id_aliases[task_id] = f'created:{turn_index}:{index}'
                    failures = grade(spec.get('expect', {}), observed)
                except TimeoutError:
                    observed, failures = {}, ['turn timed out']
                turns.append({'input': spec['input'], 'passed': not failures, 'failures': failures,
                              'elapsed_ms': round((time.perf_counter() - started) * 1000),
                              **{k: v for k, v in observed.items() if k not in {'vehicle', 'vehicle_before'}}})
            result = {'id': case['id'], 'persona': persona, 'repeat': repeat, 'tags': case.get('tags', []),
                      'passed': all(turn['passed'] for turn in turns), 'turns': turns,
                      'task_id_aliases': task_id_aliases}
        finally:
            # close cancels producers and drains the ordered trace writer. Read
            # afterward, while the temporary database still exists.
            await actor.close()
        result['traces'] = await _case_trace(db, session_id)
        result['trace_capture'] = {'after_actor_close': True, 'event_count': len(result['traces']),
                                   'recording_error': getattr(actor, 'trace_error', None)}
        for turn in result['turns']:
            turn['model_errors'] = [record for record in result['traces']
                if record['kind'] == 'model.error' and record['turn_id'] == turn.get('turn_id')]
        return safe_diagnostic(result, _model_secrets(ark))


def persona_signature(result):
    """Compare behavior arguments, preserving text and logical target identity.

    Only task IDs actually allocated by this eval run receive stable aliases;
    selecting another task, an unknown ID or a different place remains distinct.
    """
    task_ids = result.get('task_id_aliases', {})

    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        # JSON numbers share value semantics; 24 and 24.0 are the same setting.
        return int(value) if type(value) is float and value.is_integer() else value

    def key(call):
        arguments = {k: v for k, v in call['arguments'].items() if k not in PERSONA_DISPLAY_FIELDS.get(call['name'], ())} \
            if isinstance(call['arguments'], dict) else call['arguments']
        if call['name'] == 'manage_task' and isinstance(arguments, dict):
            def task_reference(value):
                return {'eval_task_ref': task_ids[value]} if isinstance(value, str) and value in task_ids else value
            if 'task_id' in arguments:
                arguments['task_id'] = task_reference(arguments['task_id'])
            if isinstance(arguments.get('task_ids'), list):
                arguments['task_ids'] = [task_reference(value) for value in arguments['task_ids']]
        return (call['name'], json.dumps(canonical(arguments), sort_keys=True, ensure_ascii=False))
    return [[key(c) for c in turn.get('talker_calls', [])] + [key(c) for c in turn.get('commands', [])]
            for turn in result['turns']]


async def run_all(cases, ark, *, concurrency=4, planner=None, maps=None, repeat=1):
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case, persona, index):
        async with semaphore:
            return await run_case(case, ark, persona=persona, planner=planner, maps=maps, repeat=index)

    jobs = [one(case, persona, index) for index in range(repeat) for case in cases
            for persona in case.get('personas', ['warm'])]
    results = await asyncio.gather(*jobs)
    # Invariant 6: the persona is in the prompt, so it must not change tool calls.
    # Sampling varies run to run, so a case fails only if no tool-call sequence
    # is shared by every persona across all repeats.
    by_case = {}
    for result in results:
        result['behavior_passed'] = result['passed']
        by_case.setdefault(result['id'], {}).setdefault(result['persona'], set()).add(json.dumps(persona_signature(result)))
    for identity, personas in by_case.items():
        if len(personas) > 1 and not set.intersection(*personas.values()):
            for result in results:
                if result['id'] == identity:
                    result['passed'] = False
                    result.setdefault('failures', []).append('no tool-call sequence is shared by all personas across repeats')
    return results


def persona_metrics(results):
    """Describe observed variation without claiming that persona caused it.

    The compatibility gate needs one shared sequence across repeats. Pairwise
    differences include every run pair from different personas, so sampling
    variation remains visible even when that gate passes.
    """
    by_case, by_persona = {}, {}
    for result in results:
        by_case.setdefault(result['id'], {}).setdefault(result['persona'], []).append(result)
        by_persona.setdefault(result['persona'], []).append(result)

    def rates(runs):
        passed = sum(result['passed'] for result in runs)
        behavior_passed = sum(result.get('behavior_passed', result['passed']) for result in runs)
        return {'runs': len(runs), 'passed': passed, 'pass_rate': round(passed / len(runs), 4),
                'behavior_passed': behavior_passed, 'behavior_pass_rate': round(behavior_passed / len(runs), 4)}

    cases = {}
    for identity, personas in sorted(by_case.items()):
        sequences = {persona: Counter(json.dumps(persona_signature(result), ensure_ascii=False) for result in runs)
                     for persona, runs in personas.items()}
        comparisons, differences = 0, 0
        for left, right in combinations(sequences.values(), 2):
            pairs = sum(left.values()) * sum(right.values())
            comparisons += pairs
            differences += pairs - sum(count * right[signature] for signature, count in left.items())
        shared = set.intersection(*(set(counts) for counts in sequences.values()))
        cases[identity] = {
            'per_persona': {persona: {**rates(personas[persona]), 'distinct_sequences': len(counts)}
                            for persona, counts in sorted(sequences.items())},
            'shared_sequence_count': len(shared),
            'compatibility_passed': len(personas) < 2 or bool(shared),
            'all_runs_identical': len(set.union(*(set(counts) for counts in sequences.values()))) == 1,
            'cross_persona_pairs': comparisons, 'different_cross_persona_pairs': differences,
            'cross_persona_difference_rate': round(differences / comparisons, 4) if comparisons else None,
        }
    comparisons = sum(case['cross_persona_pairs'] for case in cases.values())
    differences = sum(case['different_cross_persona_pairs'] for case in cases.values())
    return {'comparison_basis': 'all run pairs from different personas within each case, including different repeats',
            'compatibility_rule': 'at least one tool-call sequence shared by all personas across repeats',
            'per_persona': {persona: rates(runs) for persona, runs in sorted(by_persona.items())},
            'cross_persona_pairs': comparisons, 'different_cross_persona_pairs': differences,
            'cross_persona_difference_rate': round(differences / comparisons, 4) if comparisons else None,
            'cases': cases}


def summarize(results):
    passed = sum(1 for r in results if r['passed'])
    per_case = {}
    for result in results:
        runs = per_case.setdefault(result['id'], [0, 0])
        runs[0] += result['passed']; runs[1] += 1
    return {'total': len(results), 'passed': passed,
            'pass_rate': round(passed / len(results), 4) if results else None,
            'failed': {identity: f'{ok}/{total}' for identity, (ok, total) in sorted(per_case.items()) if ok < total},
            'persona_metrics': persona_metrics(results),
            'latency': latency_summary(results)}


def percentile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))]) if values else None


def latency_summary(results):
    """Per-turn latency from text input (no ASR), grouped by route.

    first_speech_ms: when the first words were ready for TTS. total_ms: turn
    done, including simulated vehicle receipts. model_calls: Talker calls.
    """
    turns = [t for r in results for t in r['turns'] if t.get('total_ms') is not None]
    out = {}
    for route in sorted({t.get('route') for t in turns} | {'all'}):
        group = [t for t in turns if route == 'all' or t.get('route') == route]
        speech = [t['first_speech_ms'] for t in group if t.get('first_speech_ms') is not None]
        out[route] = {'turns': len(group),
                      'first_speech_p50': percentile(speech, .5), 'first_speech_p90': percentile(speech, .9),
                      'total_p50': percentile([t['total_ms'] for t in group], .5),
                      'total_p90': percentile([t['total_ms'] for t in group], .9),
                      'model_calls_mean': round(sum(t.get('model_calls') or 0 for t in group) / len(group), 2) if group else None}
    return out


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tag', action='append', default=[])
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--repeat', type=int, default=1, help='run every case N times to measure flakiness')
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.concurrency < 1:
        parser.error('--repeat and --concurrency must be positive integers')
    cases = [c for c in load_cases()
             if (not args.tag or set(args.tag) & set(c.get('tags', [])))
             and (not args.case or c['id'] in args.case)]
    if not cases:
        parser.error('No eval cases matched the selected --case/--tag filters')
    from backend.services.factory import create_llm
    ark = create_llm()
    try:
        configuration = {'source': ark.config.source, **ark.info()}
        missing = [role for role in ('talker', 'planner')
                   if not configuration['roles'][role]['provider']['configured']]
        if missing:
            parser.error('Selected LLM roles are not configured: ' + ', '.join(missing))
        results = await run_all(cases, ark, concurrency=args.concurrency, repeat=args.repeat)
    finally:
        await ark.aclose()
    report = {'release': get_release_manifest(), 'model_configuration': configuration,
              'summary': summarize(results), 'results': results}
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
    return 0 if results and all(result['passed'] for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
