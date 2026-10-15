"""Focused real-model regression; retain the original failed benchmark separately."""
import asyncio
import json
from pathlib import Path

from backend.models import utcnow
from scripts.benchmark import Harness


async def main():
    report = {'at': utcnow(), 'real_model': True,
              'method': 'Fresh session per persona; exact destination and committed mock state checked. No corrected or substituted model arguments.',
              'before_fix_evidence': 'docs/latency-live.json', 'records': []}
    for persona in ('professional', 'warm', 'playful'):
        harness = Harness('http://127.0.0.1:8000')
        try:
            await harness.connect()
            await harness.send({'type': 'persona.set', 'preset': persona})
            sample = await harness.ask('把模拟导航目的地设为上海虹桥站', 'control',
                                       'set_navigation_destination',
                                       lambda state: state['navigation']['destination'] == '上海虹桥站')
            sample.update(persona=persona, session_id=harness.sid)
            report['records'].append(sample)
        except Exception as exc:
            report['records'].append({'persona': persona, 'success': False, 'error': str(exc)})
        finally:
            if hasattr(harness, 'ws'):
                await harness.close()
            else:
                await harness.http.aclose()
    report['successful'] = sum(record['success'] for record in report['records'])
    report['attempts'] = len(report['records'])
    Path('docs/navigation-live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'successful': report['successful'], 'attempts': report['attempts'],
                      'samples': [{key: record.get(key) for key in ('persona', 'success', 'receipt_ms', 'calls', 'text', 'error')}
                                  for record in report['records']]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
