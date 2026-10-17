"""Behavior evaluations through production NLPService and a local Gateway.

Device fixtures own independent state. Structured behavior and semantic response
review are reported separately; an unreviewed rubric is never a semantic pass.

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
import math
import time
from pathlib import Path

if __name__ == '__main__':
    from backend.local_environment import load_local_environment
    load_local_environment()

from evals.gateway_driver import EvaluationGateway, model_secrets
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
    if 'talker_model_calls' in expect and observed.get('talker_model_calls') != expect['talker_model_calls']:
        failures.append('unexpected Talker model call count')
    if 'max_lookup_calls' in expect:
        limits = expect['max_lookup_calls']
        if isinstance(limits, int):
            if len(observed.get('lookups', [])) > limits:
                failures.append('lookup call budget exceeded')
        else:
            for name, limit in limits.items():
                if observed.get('lookups', []).count(name) > limit:
                    failures.append(f'lookup call budget exceeded: {name}')
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
    # Expected business/tool rejection cannot waive a failed model call, even
    # when a later response or fallback presentation hides the top-level error.
    if observed.get('model_errors'):
        failures.append(f'model errors observed: {len(observed["model_errors"])}')
    if observed.get('error') and not expect.get('allow_error'):
        failures.append(f'turn error {observed["error"]}')
    return failures






def useful_latency_failures(observed, maximum_ms):
    """Gate the existing verified-result metric, never a preface or error text."""
    if maximum_ms is None:
        return []
    failures = []
    if (observed.get('error') or observed.get('model_errors') or observed.get('outcome_unknown')
            or observed.get('presentation_source') == 'error_template'):
        failures.append('first-useful gate: turn has an error or unknown outcome')
    useful = observed.get('first_useful_ms')
    if type(useful) not in (int, float) or not math.isfinite(useful) or useful < 0:
        failures.append('first-useful gate: first_useful_ms missing or invalid')
    elif useful > maximum_ms:
        failures.append(f'first-useful gate: {useful:.1f} ms exceeds {maximum_ms:g} ms')
    return failures


class SerialCadence:
    """One shared clock across turns/cases/repeats, outside request deadlines."""
    def __init__(self, pause_seconds):
        self.pause_seconds = pause_seconds
        self.finished_at = None

    async def before_turn(self):
        if self.finished_at is None:
            return {'actual_idle_ms': None, 'cadence_group': 'first_request'}
        remaining = self.pause_seconds - (time.perf_counter() - self.finished_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        return {'actual_idle_ms': (time.perf_counter() - self.finished_at) * 1000,
                'cadence_group': 'after_pause' if self.pause_seconds else 'consecutive'}

    def after_turn(self):
        self.finished_at = time.perf_counter()


async def run_case(case, ark, *, persona='warm', planner=None, maps=None, repeat=0,
                   max_first_useful_ms=None, cadence=None):
    gateway = EvaluationGateway(case, ark, planner=planner, maps=maps)
    task_aliases, traces = {}, []
    try:
        for path, value in case.get('vehicle', {}).items():
            set_state_value(gateway.vehicle.state, path, value)
        if 'cockpit' in case:
            for path, value in case['cockpit'].get('state', {}).items():
                set_state_value(gateway.device.state, path, value)
            gateway.media = {item['content_id']: {**item, 'provider': item.get('provider', 'local'),
                'source_turn_id': 'seeded', 'presented_to_user': True} for item in case['cockpit'].get('items', [])}
        scope = gateway.ledger.for_scope('eval-' + case['id'])
        for index, seeded in enumerate(case.get('tasks', [])):
            identity = f'seeded-{index}'
            context = {**seeded.get('context', {}), 'release_id': gateway.service.release_id,
                       'model_policy_id': gateway.service.model_policy_id}
            task = await scope.create_task(identity, seeded['goal'], context, identity)
            task.update(seeded.get('state', {}))
            if seeded.get('question'):
                task.update(status='waiting_clarification', question=seeded['question'])
            gateway.ledger.tasks[identity] = task
            task_aliases[identity] = f'seeded:{index}'
        turns = []
        for index, spec in enumerate(case['turns']):
            previous = set(gateway.ledger.tasks)
            timing = await cadence.before_turn() if cadence is not None else {}
            try:
                async with asyncio.timeout(TURN_TIMEOUT):
                    observed = await gateway.turn(spec, persona, spec.get('speaker_seat') or case.get('speaker_seat') or 'driver')
                failures = grade(spec.get('expect', {}), observed)
            except Exception as error:
                observed = gateway.failed_turn(error)
                failures = ['turn timed out' if isinstance(error, TimeoutError) else f'run failed: {type(error).__name__}']
            finally:
                if cadence is not None:
                    cadence.after_turn()
            behavior_passed = not failures
            latency_failures = useful_latency_failures(observed, max_first_useful_ms)
            failures.extend(latency_failures)
            for task_id in [identity for identity in gateway.ledger.tasks if identity not in previous]:
                task_aliases[task_id] = f'created:{index}:{len(task_aliases)}'
            traces.extend(observed.pop('trace', []))
            turns.append({'input': spec['input'], 'passed': not failures, 'failures': failures,
                'behavior_passed': behavior_passed,
                **({'latency_failures': latency_failures} if max_first_useful_ms is not None else {}), **timing,
                'response_rubric': spec.get('expect', {}).get('response_rubric'),
                'response_review_status': 'pending' if spec.get('expect', {}).get('response_rubric') else 'not_requested',
                **{key: value for key, value in observed.items() if key not in {'vehicle', 'vehicle_before'}}})
        result = {'id': case['id'], 'persona': persona, 'repeat': repeat, 'tags': case.get('tags', []),
            'engine': 'NLPService', 'release_id': gateway.service.release_id,
            'model_policy_id': gateway.service.model_policy_id, 'semantic_policy_id': gateway.service.semantic_policy.policy_id,
            'behavior_passed': all(turn['behavior_passed'] for turn in turns),
            'passed': all(turn['passed'] for turn in turns), 'turns': turns, 'task_id_aliases': task_aliases,
            'traces': traces, 'trace_capture': {'event_count': len(traces), 'recording_error': None}}
        return safe_diagnostic(result, model_secrets(ark))
    finally:
        await gateway.close()



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


async def run_all(cases, ark, *, concurrency=4, planner=None, maps=None, repeat=1,
                  max_first_useful_ms=None, pause_seconds=0):
    if not math.isfinite(pause_seconds) or pause_seconds < 0 or (pause_seconds and concurrency != 1):
        raise ValueError('pause_seconds must be nonnegative and requires concurrency=1 when positive')
    if max_first_useful_ms is not None and (not math.isfinite(max_first_useful_ms) or max_first_useful_ms <= 0):
        raise ValueError('max_first_useful_ms must be finite and positive')
    semaphore = asyncio.Semaphore(concurrency)
    cadence = SerialCadence(pause_seconds) if concurrency == 1 else None

    async def one(case, persona, index):
        async with semaphore:
            return await run_case(case, ark, persona=persona, planner=planner, maps=maps, repeat=index,
                                  max_first_useful_ms=max_first_useful_ms, cadence=cadence)

    jobs = [one(case, persona, index) for index in range(repeat) for case in cases
            for persona in case.get('personas', ['warm'])]
    results = await asyncio.gather(*jobs)
    # Invariant 6: the persona is in the prompt, so it must not change tool calls.
    # Sampling varies run to run, so a case fails only if no tool-call sequence
    # is shared by every persona across all repeats.
    by_case = {}
    for result in results:
        result.setdefault('behavior_passed', result['passed'])
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


def first_talker_cache_groups(result):
    """Classify the first Talker completion; later calls cannot warm its start."""
    groups = {}
    for row in result.get('traces', []):
        data = row.get('data', {})
        turn_id = row.get('turn_id')
        if (row.get('kind') != 'model.completed' or not turn_id or turn_id in groups
                or not isinstance(data, dict) or data.get('agent', 'talker') != 'talker'):
            continue
        meta = data.get('meta', {})
        usage = meta.get('usage', {}) if isinstance(meta, dict) else {}
        details = usage.get('prompt_tokens_details', {}) if isinstance(usage, dict) else {}
        cached = (details.get('cached_tokens') if isinstance(details, dict) and 'cached_tokens' in details
                  else usage.get('cached_tokens') if isinstance(usage, dict) else None)
        groups[turn_id] = ('zero_cache' if cached == 0 else 'cache_hit') if type(cached) is int and cached >= 0 else 'unknown'
    return groups


def text_latency_counts(turns):
    first = [t['first_effective_ms'] for t in turns if t.get('first_effective_ms') is not None]
    model_first = [t['first_model_text_ms'] for t in turns
                   if type(t.get('first_model_text_ms')) in (int, float)
                   and math.isfinite(t['first_model_text_ms']) and t['first_model_text_ms'] >= 0]
    no_model_text = sum('first_model_text_ms' in t and t['first_model_text_ms'] is None for t in turns)
    total = [t['total_ms'] for t in turns if t.get('total_ms') is not None]
    useful = [t['first_useful_ms'] for t in turns
              if type(t.get('first_useful_ms')) in (int, float)
              and math.isfinite(t['first_useful_ms']) and t['first_useful_ms'] >= 0]
    return {'turns': len(turns), 'first_text_samples': len(first),
            'no_first_text_turns': len(turns) - len(first),
            'model_text_samples': len(model_first), 'no_model_text_turns': no_model_text,
            'model_text_unknown_turns': len(turns) - len(model_first) - no_model_text,
            'first_model_text_p50': percentile(model_first, .5), 'first_model_text_p90': percentile(model_first, .9),
            'error_turns': sum(bool(t.get('error')) for t in turns),
            'first_useful_samples': len(useful), 'no_first_useful_turns': len(turns) - len(useful),
            'first_useful_p50': percentile(useful, .5), 'first_useful_p90': percentile(useful, .9),
            'first_useful_gate_turns': sum('latency_failures' in t for t in turns),
            'first_useful_gate_failed_turns': sum(bool(t.get('latency_failures')) for t in turns),
            'first_text_p50': percentile(first, .5), 'first_text_p90': percentile(first, .9),
            'total_p50': percentile(total, .5), 'total_p90': percentile(total, .9)}


def latency_summary(results):
    """Production text latency and all-role calls; audio is measured separately."""
    turns = []
    for result in results:
        cache_groups = first_talker_cache_groups(result)
        turns.extend((turn, cache_groups.get(turn.get('turn_id'), 'unknown')) for turn in result['turns'])
    out = {}
    for route in sorted({t.get('route') or 'unknown' for t, _ in turns} | {'all'}):
        selected = [(t, cache) for t, cache in turns if route == 'all' or (t.get('route') or 'unknown') == route]
        group = [t for t, _ in selected]
        speech = [t['first_speech_ms'] for t in group if t.get('first_speech_ms') is not None]
        out[route] = {**text_latency_counts(group),
                      'first_speech_p50': percentile(speech, .5), 'first_speech_p90': percentile(speech, .9),
                      'cache_groups': {cache: text_latency_counts([t for t, status in selected if status == cache])
                                       for cache in ('zero_cache', 'cache_hit', 'unknown')},
                      'cadence_groups': {cadence: text_latency_counts([t for t in group if t.get('cadence_group', 'unknown') == cadence])
                                         for cadence in ('first_request', 'consecutive', 'after_pause', 'unknown')},
                      'model_calls': sum(t.get('model_calls') or 0 for t in group),
                      'model_calls_mean': round(sum(t.get('model_calls') or 0 for t in group) / len(group), 2) if group else None,
                      'model_calls_by_role': {role: sum(t.get('model_calls_by_role', {}).get(role, 0) for t in group)
                                              for role in ('talker', 'planner', 'thinker', 'finalizer')},
                      'prompt_tokens': sum(t.get('usage', {}).get('prompt_tokens', 0) for t in group),
                      'cached_tokens': sum(t.get('usage', {}).get('cached_tokens', 0) for t in group)}
    return out


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tag', action='append', default=[])
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--repeat', type=int, default=1, help='run every case N times to measure flakiness')
    parser.add_argument('--max-first-useful-ms', type=float,
                        help='fail turns with errors, no useful result, or a useful result later than this limit')
    parser.add_argument('--pause-seconds', type=float, default=0,
                        help='minimum idle between turns, including case boundaries; positive values require --concurrency 1')
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.concurrency < 1:
        parser.error('--repeat and --concurrency must be positive integers')
    if args.max_first_useful_ms is not None and (not math.isfinite(args.max_first_useful_ms) or args.max_first_useful_ms <= 0):
        parser.error('--max-first-useful-ms must be finite and positive')
    if not math.isfinite(args.pause_seconds) or args.pause_seconds < 0:
        parser.error('--pause-seconds must be finite and nonnegative')
    if args.pause_seconds and args.concurrency != 1:
        parser.error('--pause-seconds requires --concurrency 1')
    available = load_cases()
    unknown = sorted(set(args.case) - {case['id'] for case in available})
    if unknown:
        parser.error('Unknown eval case IDs: ' + ', '.join(unknown))
    cases = [c for c in available
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
        results = await run_all(cases, ark, concurrency=args.concurrency, repeat=args.repeat,
                                max_first_useful_ms=args.max_first_useful_ms, pause_seconds=args.pause_seconds)
        from evals.response_review import review_results
        response_review = await review_results(ark, results, concurrency=args.concurrency)
        for result in results:
            for turn in result['turns']:
                if not turn.get('response_rubric'):
                    continue
                verdict = turn.get('response_review') or {}
                turn['structured_passed'] = turn['passed']
                turn['response_review_status'] = verdict.get('status', 'unreviewed')
                if verdict.get('status') != 'reviewed' or verdict.get('passed') is not True:
                    turn['passed'] = False
                    turn['failures'].append('response rubric failed' if verdict.get('passed') is False else 'response rubric unreviewed')
            result['passed'] = result['passed'] and all(turn['passed'] for turn in result['turns'])
    finally:
        await ark.aclose()
    report = {'engine': 'NLPService', 'release': get_release_manifest(), 'model_configuration': configuration,
              'selection': {'requested_case_ids': args.case, 'tags': args.tag,
                            'selected_case_ids': [case['id'] for case in cases],
                            'repeat': args.repeat, 'concurrency': args.concurrency,
                            'max_first_useful_ms': args.max_first_useful_ms, 'pause_seconds': args.pause_seconds},
              'response_review': response_review,
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
