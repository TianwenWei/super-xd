"""User feedback comes from task state; provider diagnostics remain in trace."""

import json
from super_xd.nlp.prompts.speech import feedback_text
from super_xd.nlp.prompts.speech_text import speech_text

PHASES = {name: feedback_text('task.phases.' + name) for name in (
    'search_web', 'lookup_web', 'extract_web', 'get_weather_forecast', 'load_skill',
    'suggest_vehicle_action', 'finish_task', 'model')}

ERRORS = {name: feedback_text('task.errors.' + name) for name in (
    'deadline', 'step_budget', 'clarification_timeout', 'execution_failed', 'finalization_failed')}


def task_user_detail(task):
    status = task.get('status')
    if status == 'failed':
        detail = ERRORS.get(task.get('error_code'), ERRORS['execution_failed'])
        if task.get('sources'):
            detail += feedback_text('task.sources_retained')
        return detail
    if status == 'waiting_clarification':
        return task.get('question') or feedback_text('task.needs_condition')
    if status in {'cancelled', 'cancelling'}:
        return feedback_text('task.cancelled') if status == 'cancelled' else feedback_text('task.cancelling')
    if status == 'queued':
        return feedback_text('task.queued')
    if status == 'running':
        progress = task.get('progress') or {}
        count = progress.get('read_sources', 0)
        prefix = feedback_text('task.read_sources', count=count) if count else ''
        return prefix + PHASES.get(progress.get('phase'), PHASES['model'])
    return task.get('summary') or ''


def partial_result(task):
    """Retain actual material without inventing a draft when synthesis failed."""
    checkpoint = task.get('checkpoint') or {}
    sources = task.get('sources') or checkpoint.get('sources') or []
    if not sources and not checkpoint.get('observations'):
        return None
    detail = task_user_detail(task)
    return {'partial': True, 'summary': detail, 'spoken_summary': detail,
            'sources': sources, 'suggested_actions': checkpoint.get('suggestions', []),
            'tool_observations': checkpoint.get('observations', []),
            'web_evidence': 'incomplete'}


def task_presentation(task):
    """Select the same factual text for foreground and deferred delivery."""
    status, result = task['status'], task.get('result')
    full = ((result.get('summary') or result.get('answer') or json.dumps(result, ensure_ascii=False))
            if isinstance(result, dict) else result) or task.get('summary') or ''
    selected = (str(full) if task.get('context', {}).get('read_details') is True else
                ((result.get('spoken_summary') if isinstance(result, dict) else None) or speech_text(str(full))))
    detail = selected if status == 'completed' else task_user_detail(task)
    if status == 'completed' and isinstance(result, dict) and result.get('partial') is True:
        detail = feedback_text('task.partial_prefix') + detail
    kind = ('progress' if status in {'queued', 'running'} else
            'clarify' if status == 'waiting_clarification' else
            'error' if status == 'failed' else 'result')
    return detail, kind
