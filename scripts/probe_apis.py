"""Configured model/search capability probe; reports selected services, not raw defaults."""
import asyncio
import json
import time
from pathlib import Path
from contextlib import AsyncExitStack
from backend.models import sanitized_error, utcnow
from backend.services.config import load_services_config
from backend.services.factory import create_llm, create_search

async def main():
    config = load_services_config()
    async with AsyncExitStack() as resources:
        ark = create_llm(config)
        resources.push_async_callback(ark.aclose)
        web = create_search(config)
        resources.push_async_callback(web.aclose)
        return await probe(ark, web, config)


async def probe(ark, web, config):
    tool = {'type': 'function', 'function': {'name': 'probe_temperature', 'description': '设置空调温度', 'parameters': {'type': 'object', 'properties': {'value': {'type': 'number'}}, 'required': ['value'], 'additionalProperties': False}}}
    output = {'at': utcnow(), 'talker_model': ark.request([], role='talker').model,
              'thinker_model': ark.request([], role='thinker').model, 'checks': {},
              'services_config': config.source, 'llm': ark.info(), 'web': web.info(),
              'scope': 'selected project LLM roles and search/content profiles'}
    async def check(name, operation):
        start = time.perf_counter()
        try:
            value = await operation()
            output['checks'][name] = {'ok': True, 'elapsed_ms': round((time.perf_counter()-start)*1000,1), **value}
        except Exception as exc:
            output['checks'][name] = {'ok': False, 'elapsed_ms': round((time.perf_counter()-start)*1000,1), 'error': sanitized_error(exc)}
    async def stream_probe():
        first, content, message = None, '', None
        start = time.perf_counter()
        async for item in ark.stream([{'role': 'user', 'content': '用一句话描述秋天的天空。'}]):
            if item['type'] == 'delta':
                if first is None:
                    first = (time.perf_counter()-start)*1000
                content += item['text']
            else:
                message = item['message']
        return {'first_delta_ms': first, 'text': content, 'streaming': message is not None}
    async def tool_probe(thinker):
        msg = await ark.complete([{'role':'system','content':'必须调用probe_temperature工具，不要只用文字。'}, {'role':'user','content':'把空调设为23度'}], tools=[tool], thinker=thinker)
        calls = msg.get('tool_calls', [])
        if not calls:
            raise RuntimeError('模型响应没有工具调用')
        return {'tool_calls': calls, 'has_reasoning_content': bool(msg.get('reasoning_content'))}
    async def search_probe():
        result = await web.search('Tavily Search API documentation', 2)
        urls = [r['url'] for r in result.get('results', [])]
        extracted = await web.extract(urls[:1]) if urls else {'results': []}
        return {'queried_at': result['queried_at'], 'urls': urls, 'search_count': len(urls), 'extract_count':len(extracted.get('results', []))}
    await asyncio.gather(check('talker_stream', stream_probe), check('talker_function_call', lambda:tool_probe(False)), check('thinker_function_call', lambda:tool_probe(True)), check('web_search_extract', search_probe))
    Path('docs/api-probe.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output

if __name__=='__main__':
    asyncio.run(main())
