"""Advisory review of what the assistant said, using a model as the reader.

Behavior evals (harness.py) grade structured results only. This script reads
the spoken text of a harness report and reports two things that code cannot
check without parsing language (AGENTS.md §3):

- tense: whether the sentence spoken before receipts claims completion;
- naturalness: a 1–5 score for in-car spoken Chinese, with a short reason.

The numbers are for comparison between runs. They never fail a build.

    .venv/bin/python -m evals.speech_review data/evals/run.json --out data/evals/run.speech.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from backend.models import ArkClient
from backend.settings import THINKER_MODEL

PROMPT = """你在审阅车载语音助手说给司机听的话。只根据给出的内容判断，输出一个 JSON 对象，不要输出其他文字。

字段：
- preface_claims_completion：只看 preface（助手在车辆回执之前说的话）。如果它声称动作已经完成，比如"已经调好了""搞定了""打开了"，填 true；进行时或将来时（"这就调""马上打开""我去查一下"）填 false。preface 为空时填 null。
- natural：1 到 5 分，衡量整段话像不像一个自然、简短、口语化的人在车里说话。5 分是自然得体；3 分是能用但生硬、像模板或啰嗦；1 分是别扭或不适合朗读。
- issue：一句话说明最主要的问题；没有问题就填空字符串。"""


async def review_turn(ark, model, turn):
    payload = {'user': turn['input'], 'preface': turn.get('preface') or '', 'full_reply': turn.get('text') or ''}
    message = await ark.complete([{'role': 'system', 'content': PROMPT},
                                  {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                                 model=model, thinking=False, max_tokens=300)
    try:
        verdict = json.loads(message.get('content') or '')
    except ValueError:
        verdict = {'parse_error': message.get('content')}
    return {**payload, **verdict}


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('report')
    parser.add_argument('--out')
    parser.add_argument('--model', default=THINKER_MODEL)
    parser.add_argument('--concurrency', type=int, default=6)
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding='utf-8'))
    turns, seen = [], set()
    for result in report['results']:
        for turn in result['turns']:
            key = (turn['input'], turn.get('preface'), turn.get('text'))
            if turn.get('text') and key not in seen:
                seen.add(key)
                turns.append(turn)
    ark, semaphore = ArkClient(), asyncio.Semaphore(args.concurrency)

    async def one(turn):
        async with semaphore:
            try:
                return await review_turn(ark, args.model, turn)
            except RuntimeError as exc:
                return {'user': turn['input'], 'error': str(exc)}
    try:
        reviews = await asyncio.gather(*(one(turn) for turn in turns))
    finally:
        await ark.aclose()
    scored = [r for r in reviews if isinstance(r.get('natural'), (int, float))]
    prefaced = [r for r in reviews if r.get('preface_claims_completion') is not None]
    summary = {'turns': len(reviews), 'scored': len(scored),
               'mean_natural': round(sum(r['natural'] for r in scored) / len(scored), 2) if scored else None,
               'prefaces': len(prefaced),
               'completion_claims': sum(1 for r in prefaced if r['preface_claims_completion'] is True)}
    for review in sorted(scored, key=lambda r: r['natural'])[:12]:
        print(f"{review['natural']}  {review['user']}\n     → {review['full_reply']}\n     {review.get('issue', '')}")
    for review in prefaced:
        if review['preface_claims_completion'] is True:
            print(f"COMPLETION CLAIM: {review['user']} → {review['preface']}")
    print(json.dumps(summary, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps({'summary': summary, 'reviews': reviews}, ensure_ascii=False, indent=1),
                                  encoding='utf-8')


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
