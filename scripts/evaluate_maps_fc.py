"""Configured Talker + selected Google Places/Weather with simulated receipts."""
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
import tempfile

from backend.db import Database
from backend.models import utcnow
from backend.session import SessionActor
from backend.services.config import load_services_config
from backend.services.factory import create_llm, create_maps


async def main():
    config = load_services_config()
    if any(config.profile(domain)['provider'] != 'google' for domain in ('places', 'weather')):
        raise ValueError('This Google acceptance requires selected Google places and weather profiles')
    if config.profile('weather').get('coverage_fallback', 'open_meteo') != 'open_meteo':
        raise ValueError('This Google coverage acceptance requires the configured Open-Meteo fallback')
    async with AsyncExitStack() as resources:
        maps = create_maps(config)
        resources.push_async_callback(maps.aclose)
        ark = create_llm(config)
        resources.push_async_callback(ark.aclose)
        return await evaluate(maps, ark, config)


async def evaluate(maps, ark, config):
    cases = [
        ('paris_weather', ['巴黎明天天气怎么样？']),
        ('shenzhen_weather', ['深圳明天天气怎么样？']),
        ('poi_selection', ['帮我找巴黎卢浮宫附近的咖啡馆', '导航去刚才搜索结果的第一个']),
        ('direct_navigation', ['帮我导航到巴黎卢浮宫']),
        ('weather_and_control', ['巴黎今天天气怎么样？再把空调调到23度']),
    ]
    report = {'observed_at': utcnow(), 'real_model': True, 'real_google': True,
              'simulated_vehicle': True, 'audio_tested': False, 'cases': [],
              'scope': 'configured LLM roles; Google Places/Weather with configured Open-Meteo coverage fallback',
              'services_config': config.source, 'llm': ark.info(), 'maps': maps.info()}
    with tempfile.TemporaryDirectory() as directory:
        async def run(name, inputs):
            db = Database(Path(directory) / f'{name}.sqlite')
            actor = SessionActor(name, db, ark, maps=maps)
            q = await actor.attach()
            await q.get()
            record = {'case': name, 'turns': []}
            try:
                for query in inputs:
                    await actor.post({'type': 'input', 'text': query})
                    async with asyncio.timeout(100):
                        while True:
                            event = await q.get()
                            if event['type'] == 'vehicle.command':
                                receipt = await actor.simulator.execute(event['command_id'])
                                await actor.post({'type': 'vehicle.receipt', 'command_id': event['command_id'], 'receipt_id': receipt['receipt_id']})
                            if event['type'] == 'stream.done':
                                turn = actor.current
                                record['turns'].append({'input': query, 'output': event['text'], 'metrics': event['metrics'],
                                    'calls': [call['function'] for msg in turn.messages for call in msg.get('tool_calls', [])],
                                    'lookups': turn.lookup_results})
                                break
                record['task_count'] = len(db.list_tasks(actor.id))
                record['navigation'] = actor.vehicle['navigation']
                record['climate'] = actor.vehicle['climate']
                calls = [c['name'] for t in record['turns'] for c in t['calls']]
                lookups = [r['result'] for t in record['turns'] for r in t['lookups']]
                record['passed'] = record['task_count'] == 0 and bool(lookups) and all(r.get('ok') for r in lookups)
                if 'weather' in name:
                    record['passed'] &= 'get_weather' in calls
                    wanted = 'Open-Meteo' if name.startswith('shenzhen') else 'Google Weather'
                    record['passed'] &= any(r.get('provider') == wanted for r in lookups)
                if name in ('poi_selection', 'direct_navigation'):
                    record['passed'] &= 'search_places' in calls and 'start_poi_navigation' in calls and bool(actor.vehicle['navigation'].get('destination_place'))
                if name == 'weather_and_control':
                    record['passed'] &= actor.vehicle['climate']['temperatures']['driver'] == 23
            except Exception as exc:
                record.update(passed=False, error=type(exc).__name__)
            finally:
                await actor.close()
            report['cases'].append(record)
            print(json.dumps({k: v for k, v in record.items() if k not in {'climate', 'navigation', 'turns'}} |
                {'turns': [{k: v for k, v in t.items() if k != 'lookups'} for t in record['turns']]}, ensure_ascii=False), flush=True)
        # Bound live concurrency to avoid overwhelming the provider/model account.
        for start in range(0, len(cases), 2):
            await asyncio.gather(*(run(*case) for case in cases[start:start+2]))
    report['passed'] = sum(c['passed'] for c in report['cases'])
    report['total'] = len(cases)
    output = Path('docs/maps-fc-live.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'report': str(output)}))


if __name__ == '__main__':
    asyncio.run(main())
