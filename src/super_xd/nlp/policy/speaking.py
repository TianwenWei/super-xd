"""Pure notification arbitration; the Gateway owns facts, queues and delivery."""
from datetime import datetime

from super_xd.nlp.contracts.events import PresentationDecision, SpeakingContext, TaskNotice


def notice_order(notice: TaskNotice, now: datetime):
    # An expiring question precedes ordinary results without interrupting speech.
    priority = 1 if notice.kind == 'clarification' and (notice.expires_at - now).total_seconds() <= 30 else notice.priority
    return priority, notice.expires_at, notice.created_at, notice.event_id


def decide_presentation(notice: TaskNotice, context: SpeakingContext, *, now: datetime,
                        current_revision: int, current_status: str) -> PresentationDecision:
    if notice.revision != current_revision or notice.status != current_status:
        return PresentationDecision(action='drop', reason='stale_task')
    if now >= notice.expires_at:
        return PresentationDecision(action='drop', reason='expired')
    if notice.kind == 'progress':
        return PresentationDecision(action='screen_only', reason='ordinary_progress')
    if not context.connected:
        return PresentationDecision(action='defer', reason='disconnected')
    if context.user_speaking or context.assistant_speaking or context.generating:
        return PresentationDecision(action='defer', reason='conversation_busy')
    if context.in_call or context.demanding_driving:
        return PresentationDecision(action='defer', reason='attention_unavailable')
    return PresentationDecision(action='speak', reason='available')
