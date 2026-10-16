"""User feedback comes from task state; provider diagnostics remain in trace."""

PHASES = {
    'search_web': '正在查找相关资料。',
    'lookup_web': '正在查找并核实资料。',
    'extract_web': '正在读取来源页面，核实关键信息。',
    'get_weather_forecast': '正在查询目标日期的天气预报。',
    'load_skill': '正在查看车辆设置说明。',
    'suggest_vehicle_action': '正在整理车辆设置建议，还没有执行。',
    'finish_task': '正在整理已有资料和未核实的部分。',
    'model': '还在分析已有资料。',
}

ERRORS = {
    'deadline': '已到这次查询的时间上限，完整结果还没能整理好。',
    'step_budget': '这次查询还没能得到完整结果，已停止继续检索。',
    'clarification_timeout': '等待补充条件的时间已过，请重新发起任务。',
    'execution_failed': '查询过程中遇到问题，暂时没能完成。',
    'finalization_failed': '已有资料还没能整理成可靠的完整结果，已停止这次查询。',
}


def task_user_detail(task):
    status = task.get('status')
    if status == 'failed':
        detail = ERRORS.get(task.get('error_code'), ERRORS['execution_failed'])
        if task.get('sources'):
            detail += '已找到的来源保留在任务卡里，尚未形成完整答案。'
        return detail
    if status == 'waiting_clarification':
        return task.get('question') or '还需要你补充一个条件。'
    if status in {'cancelled', 'cancelling'}:
        return '任务已取消。' if status == 'cancelled' else '正在取消这项任务。'
    if status == 'queued':
        return '任务还在排队，尚未开始查询。'
    if status == 'running':
        progress = task.get('progress') or {}
        count = progress.get('read_sources', 0)
        prefix = f'已读取{count}个来源页面，' if count else ''
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
