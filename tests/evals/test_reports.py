"""Saving a later diagnostic batch must preserve earlier evidence."""
from datetime import datetime, timezone
import json

from scripts import reports


def test_batches_started_at_the_same_instant_preserve_both_reports(monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    class FixedClock:
        @staticmethod
        def now(zone):
            return fixed.astimezone(zone)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reports, 'datetime', FixedClock)
    first_path = reports.report_path('provider-check')
    first = {'passed': False, 'error': '第一批真实失败'}
    reports.write_report(first, first_path)
    original_bytes = first_path.read_bytes()

    second_path = reports.report_path('provider-check')
    second = {'passed': True, 'note': '下一批结果'}
    reports.write_report(second, second_path)

    assert first_path.parent != second_path.parent
    assert first_path.read_bytes() == original_bytes
    assert json.loads(first_path.read_text(encoding='utf-8')) == first
    assert json.loads(second_path.read_text(encoding='utf-8')) == second
