"""Serial chat latency benchmark through the normal production NLPService.

    python -m evals.latency --repeat 3 --pause-seconds 30 \
        --max-first-text-ms 2000 --out data/evals/chat-latency.json

This measures service submission to text, not microphone, WebSocket or speaker
latency. Every input starts a new conversation with the normal context and 26
tools. The configured model and its connection pool are shared for the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import time

if __name__ == '__main__':
    from backend.local_environment import load_local_environment
    load_local_environment()

from backend.demo.resources import freeze_local_model
from backend.harness.diagnostics import safe_diagnostic
from backend.harness.release import get_release_manifest
from backend.models import utcnow
from evals.gateway_driver import EvaluationGateway, model_secrets
from evals.harness import percentile, text_latency_counts


INPUTS = (
    ('greeting', '你好'),
    ('casual_chat', '今天想轻松聊几句。'),
    ('thanks', '谢谢你陪我聊天。'),
)
CADENCES = ('first_request', 'consecutive', 'after_pause')
CACHE_GROUPS = ('zero_cache', 'cache_hit', 'unknown')


class UnexpectedToolRequest(RuntimeError):
    code = 'unexpected_tool_request'


class ChatBenchmarkGateway(EvaluationGateway):
    async def execute(self, item, start):
        # Keep normal capabilities and model tools, but a chat benchmark must
        # never turn a model mistake into an actual device or network operation.
        raise UnexpectedToolRequest(f'Chat benchmark cannot execute {item.request.name}')


def first_talker_cached_tokens(observed):
    for row in observed.get('trace', []):
        data = row.get('data', {})
        if (row.get('kind') != 'model.completed' or row.get('turn_id') != observed.get('turn_id')
                or data.get('agent', 'talker') != 'talker'):
            continue
        meta = data.get('meta', {})
        usage = meta.get('usage', {}) if isinstance(meta, dict) else {}
        details = usage.get('prompt_tokens_details') if isinstance(usage, dict) else None
        cached = (details.get('cached_tokens') if isinstance(details, dict) and 'cached_tokens' in details
                  else usage.get('cached_tokens') if isinstance(usage, dict) else None)
        # A missing first completion's usage cannot be supplied by a later call.
        return cached if type(cached) is int and cached >= 0 else None
    return None


def failures_for(observed, max_first_text_ms):
    failures = []
    if observed.get('error'):
        failures.append('turn_error')
    first = observed.get('first_model_text_ms')
    if type(first) not in (int, float) or not math.isfinite(first) or first < 0:
        failures.append('no_model_text')
    elif max_first_text_ms is not None and first > max_first_text_ms:
        failures.append('first_model_text_threshold_exceeded')
    if observed.get('talker_calls') or observed.get('commands') or observed.get('cockpit_commands') or observed.get('lookups'):
        failures.append('unexpected_tool_request')
    if observed.get('model_calls') != 1:
        failures.append('unexpected_model_call_count')
    return failures


def group_summary(samples):
    successful = [sample for sample in samples if sample['response_succeeded']]
    first = [sample['first_model_text_ms'] for sample in successful]
    return {**text_latency_counts(samples),
        'passed': sum(sample['passed'] for sample in samples),
        'failed': sum(not sample['passed'] for sample in samples),
        'successful_response_turns': len(successful),
        'successful_first_model_text_samples': len(first),
        'successful_first_model_text_p50': percentile(first, .5),
        'successful_first_model_text_p90': percentile(first, .9),
        'model_calls': sum(sample.get('model_calls', 0) for sample in samples),
        'model_calls_mean': round(sum(sample.get('model_calls', 0) for sample in samples) / len(samples), 2) if samples else None}


def benchmark_summary(samples):
    return {**group_summary(samples),
        'percentile_basis': 'All observed model-text samples include partial failed turns; successful_* includes completed chat responses, including slow responses failing the threshold. Missing output remains in counts.',
        'cadence_groups': {cadence: group_summary([sample for sample in samples if sample['cadence_group'] == cadence])
                           for cadence in CADENCES},
        'cache_groups': {cache: group_summary([sample for sample in samples if sample['cache_group'] == cache])
                         for cache in CACHE_GROUPS},
        'cadence_cache_groups': {cadence: {cache: group_summary([
            sample for sample in samples if sample['cadence_group'] == cadence and sample['cache_group'] == cache])
            for cache in CACHE_GROUPS} for cadence in CADENCES}}


async def run_benchmark(model, *, repeat=3, pause_seconds=0, max_first_text_ms=None):
    model = freeze_local_model(model)
    samples, previous_finished = [], None
    for repetition in range(repeat):
        for identity, text in INPUTS:
            setup_started = time.perf_counter()
            gateway = ChatBenchmarkGateway({'id': f'latency-{identity}-{len(samples) + 1}'}, model)
            setup_ms = (time.perf_counter() - setup_started) * 1000
            try:
                if previous_finished is not None and pause_seconds:
                    await asyncio.sleep(pause_seconds)
                submitted_at = utcnow()
                started = time.perf_counter()
                idle_ms = (started - previous_finished) * 1000 if previous_finished is not None else None
                try:
                    observed = await gateway.turn({'input': text}, 'warm', 'driver')
                except Exception as error:
                    observed = gateway.failed_turn(error)
                previous_finished = time.perf_counter()
                failures = failures_for(observed, max_first_text_ms)
                cached = first_talker_cached_tokens(observed)
                samples.append({**observed, 'input_id': identity, 'input': text, 'repeat': repetition + 1,
                    'sequence': len(samples) + 1, 'submitted_at': submitted_at,
                    'setup_ms': setup_ms, 'actual_idle_ms': idle_ms,
                    'pause_requested_seconds': pause_seconds if idle_ms is not None else 0,
                    'cadence_group': 'first_request' if idle_ms is None else 'after_pause' if pause_seconds else 'consecutive',
                    'first_talker_cached_tokens': cached,
                    'cache_group': 'unknown' if cached is None else 'zero_cache' if cached == 0 else 'cache_hit',
                    'release_id': gateway.service.release_id, 'model_policy_id': gateway.policy_id,
                    'response_succeeded': not any(reason != 'first_model_text_threshold_exceeded' for reason in failures),
                    'passed': not failures, 'failures': failures})
            finally:
                await gateway.close()
    return {'engine': 'NLPService', 'release': get_release_manifest(), 'created_at': utcnow(),
        'scope': 'Service submission to text; excludes browser, ASR and TTS. New conversation per sample, shared frozen model/pool, normal 26-tool context. No external tools execute. First request does not imply zero cache.',
        'selection': {'inputs': [{'id': identity, 'text': text} for identity, text in INPUTS],
            'repeat': repeat, 'pause_seconds': pause_seconds, 'concurrency': 1,
            'max_first_text_ms': max_first_text_ms, 'persona': 'warm'},
        'summary': benchmark_summary(samples), 'samples': samples}


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--pause-seconds', type=float, default=0)
    parser.add_argument('--max-first-text-ms', type=float)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error('--repeat must be a positive integer')
    if not math.isfinite(args.pause_seconds) or args.pause_seconds < 0:
        parser.error('--pause-seconds must be finite and nonnegative')
    if args.max_first_text_ms is not None and (not math.isfinite(args.max_first_text_ms) or args.max_first_text_ms <= 0):
        parser.error('--max-first-text-ms must be finite and positive')
    from backend.services.factory import create_llm
    model = create_llm()
    try:
        frozen = freeze_local_model(model)
        configuration = {'source': frozen.config.source, **frozen.info()}
        if not configuration['roles']['talker']['provider']['configured']:
            parser.error('Selected Talker LLM role is not configured')
        report = await run_benchmark(frozen, repeat=args.repeat, pause_seconds=args.pause_seconds,
                                     max_first_text_ms=args.max_first_text_ms)
        report['model_configuration'] = configuration
        report = safe_diagnostic(report, model_secrets(model))
    finally:
        await model.aclose()
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))
    return 0 if report['summary']['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
