"""Advisory review of what the assistant said, using a model as the reader.

Behavior evals (harness.py) grade structured results only. This script reads
the spoken text of a harness report and reports two things that code cannot
check without parsing language (AGENTS.md: 必须保持的不变量):

- tense: whether the sentence spoken before receipts claims completion;
- naturalness: a 1–5 score for in-car spoken Chinese, with a short reason.

The scores are advisory. Invalid or unavailable reviews are reported as
unreviewed and make the CLI return nonzero; they are never scored as passes.

    .venv/bin/python -m evals.speech_review data/evals/run.json --out data/evals/run.speech.json
"""
from __future__ import annotations
from super_xd.nlp.contracts.providers import ModelRequest

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import math
import sys
from pathlib import Path

if __name__ == '__main__':
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

from super_xd.nlp.adapters.common import sanitized_error
from super_xd.nlp.adapters.factory import create_llm
from super_xd.nlp.textutil import decode_json_object

PROMPT = """你在审阅车载语音助手说给司机听的话。只根据给出的内容判断，输出一个 JSON 对象，不要输出其他文字。

字段：
- preface_claims_completion：只看 preface（助手在车辆回执之前说的话）。如果它声称动作已经完成，比如"已经调好了""搞定了""打开了"，填 true；进行时或将来时（"这就调""马上打开""我去查一下"）填 false。preface 为空时填 null。
- natural：1 到 5 分，衡量整段话像不像一个自然、简短、口语化的人在车里说话。5 分是自然得体；3 分是能用但生硬、像模板或啰嗦；1 分是别扭或不适合朗读。
- issue：一句话说明最主要的问题；没有问题就填空字符串。"""

_VERDICT_FIELDS = frozenset({'preface_claims_completion', 'natural', 'issue'})


def decode_verdict(raw, *, preface):
    """Validate reviewer fields after strict format-only decoding."""
    verdict = decode_json_object(raw)
    if set(verdict) != _VERDICT_FIELDS:
        raise ValueError('Review must contain exactly the three verdict fields')
    claims = verdict['preface_claims_completion']
    if (preface and type(claims) is not bool) or (not preface and claims is not None):
        raise ValueError('Completion claim must be boolean for a preface and null without one')
    score = verdict['natural']
    if type(score) not in (int, float) or not math.isfinite(score) or not 1 <= score <= 5:
        raise ValueError('Naturalness must be a finite number from 1 to 5')
    if not isinstance(verdict['issue'], str):
        raise ValueError('Issue must be a string')
    return verdict


def parse_review_response(raw, *, preface):
    record = {'raw_response': raw}
    try:
        verdict = decode_verdict(raw, preface=preface)
    except (ValueError, TypeError) as error:
        return {**record, 'review_status': 'parse_error', 'parse_error': str(error)}
    return {**record, 'review_status': 'reviewed', **verdict}


def summarize_reviews(reviews):
    scored = [row for row in reviews if row.get('review_status') == 'reviewed']
    prefaced = [row for row in scored if row['preface_claims_completion'] is not None]
    return {'turns': len(reviews), 'reviewed': len(scored), 'unreviewed': len(reviews) - len(scored),
            'parse_errors': sum(row.get('review_status') == 'parse_error' for row in reviews),
            'provider_errors': sum(bool(row.get('error')) for row in reviews), 'scored': len(scored),
            'mean_natural': round(sum(row['natural'] for row in scored) / len(scored), 2) if scored else None,
            'prefaces': len(prefaced),
            'completion_claims': sum(row['preface_claims_completion'] is True for row in prefaced)}


def reparse_report(report):
    """Reinterpret saved responses without requesting or inventing model output."""
    revised = deepcopy(report)
    for row in revised['reviews']:
        raw = row.get('raw_response', row.get('parse_error'))
        if raw is None:
            row['review_status'] = 'unreviewed'
            row['reparse_error'] = 'No saved raw response available'
            continue
        prior = {key: row.pop(key) for key in (*_VERDICT_FIELDS, 'parse_error', 'review_status') if key in row}
        row['previous_review'] = prior
        row.update(parse_review_response(raw, preface=row.get('preface') or ''))
    revised['summary'] = summarize_reviews(revised['reviews'])
    return revised


async def review_turn(llm, model, turn):
    payload = {'user': turn['input'], 'preface': turn.get('preface') or '', 'full_reply': turn.get('text') or ''}
    request = llm.resolve(ModelRequest([{'role': 'system', 'content': PROMPT},
                           {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], role='thinker', reasoning=False, max_tokens=300))
    if model is not None:
        if not model.strip():
            raise ValueError('Review model must be a nonblank identifier')
        # --model is an explicit experiment, not a legacy production model hint.
        request = replace(request, model=model)
    profile = llm.config.role('thinker')['profile']
    result = await llm.providers[profile].complete(request)
    message = result.message
    verdict = parse_review_response(message.get('content'), preface=payload['preface'])
    return {**payload, **verdict, 'model_execution': {'profile': profile, 'model': request.model,
            'provider': llm.config.document['llm']['profiles'][profile]['provider']},
            'response_metadata': result.metadata}


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('report')
    parser.add_argument('--out')
    parser.add_argument('--model', help='Explicit review model override; otherwise use the configured thinker model')
    parser.add_argument('--concurrency', type=int, default=6)
    args = parser.parse_args(argv)
    if args.concurrency <= 0:
        parser.error('--concurrency must be positive')
    report = json.loads(Path(args.report).read_text(encoding='utf-8'))
    turns, seen = [], set()
    for result in report['results']:
        for turn in result['turns']:
            key = (turn['input'], turn.get('preface'), turn.get('text'))
            if turn.get('text') and key not in seen:
                seen.add(key)
                turns.append(turn)
    llm, semaphore = create_llm(), asyncio.Semaphore(args.concurrency)
    secrets = tuple(secret for provider in llm.providers.values() for secret in getattr(provider, 'secrets', ()))

    async def one(turn):
        async with semaphore:
            try:
                return await review_turn(llm, args.model, turn)
            except RuntimeError as exc:
                return {'user': turn['input'], 'review_status': 'unreviewed', 'error': sanitized_error(exc, secrets)}
    try:
        reviews = await asyncio.gather(*(one(turn) for turn in turns))
    finally:
        await llm.aclose()
    scored = [r for r in reviews if r.get('review_status') == 'reviewed']
    prefaced = [r for r in scored if r['preface_claims_completion'] is not None]
    summary = summarize_reviews(reviews)
    for review in sorted(scored, key=lambda r: r['natural'])[:12]:
        print(f"{review['natural']}  {review['user']}\n     → {review['full_reply']}\n     {review.get('issue', '')}")
    for review in prefaced:
        if review['preface_claims_completion'] is True:
            print(f"COMPLETION CLAIM: {review['user']} → {review['preface']}")
    print(json.dumps(summary, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps({'model_configuration': {'source': llm.config.source},
                                             'summary': summary, 'reviews': reviews}, ensure_ascii=False, indent=1),
                                  encoding='utf-8')
    return 1 if summary['unreviewed'] else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
