from datetime import datetime, timedelta, timezone

import pytest

from super_xd.nlp.contracts.events import SpeakingContext, TaskNotice
from super_xd.nlp.policy.speaking import decide_presentation, notice_order


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def notice(**changes):
    return TaskNotice(**{'event_id': 'event-1', 'task_id': 'task-1', 'revision': 2,
        'status': 'completed', 'kind': 'result', 'priority': 2, 'created_at': NOW,
        'expires_at': NOW + timedelta(minutes=10), **changes})


@pytest.mark.parametrize('flag', ['user_speaking', 'assistant_speaking', 'generating', 'in_call', 'demanding_driving'])
def test_attention_conditions_defer_results(flag):
    decision = decide_presentation(notice(), SpeakingContext(**{flag: True}), now=NOW,
                                   current_revision=2, current_status='completed')
    assert decision.action == 'defer'


@pytest.mark.parametrize('revision,status', [(3, 'completed'), (2, 'cancelled')])
def test_changed_task_cannot_be_presented(revision, status):
    assert decide_presentation(notice(), SpeakingContext(), now=NOW,
        current_revision=revision, current_status=status).action == 'drop'


def test_expiration_and_progress_are_not_spoken():
    assert decide_presentation(notice(expires_at=NOW), SpeakingContext(), now=NOW,
        current_revision=2, current_status='completed').reason == 'expired'
    assert decide_presentation(notice(kind='progress', priority=3), SpeakingContext(), now=NOW,
        current_revision=2, current_status='completed').action == 'screen_only'


def test_question_precedes_result_and_rechecks_after_call_ends():
    question = notice(kind='clarification', status='waiting_clarification', priority=2,
                      expires_at=NOW + timedelta(seconds=20))
    assert notice_order(question, NOW) < notice_order(notice(), NOW)
    assert decide_presentation(question, SpeakingContext(in_call=True), now=NOW,
        current_revision=2, current_status='waiting_clarification').action == 'defer'
    assert decide_presentation(question, SpeakingContext(), now=NOW,
        current_revision=2, current_status='waiting_clarification').action == 'speak'
