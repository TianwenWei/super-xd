"""Configured model/search capability probe; reports selected services, not raw defaults."""
from super_xd.nlp.contracts.providers import ModelRequest
if __name__ == "__main__":
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

import asyncio
from scripts.reports import report_path, write_report
import json
import time
from pathlib import Path
from contextlib import AsyncExitStack
from super_xd.nlp.adapters.common import sanitized_error, utcnow
from super_xd.nlp.config.services import load_services_config
from super_xd.nlp.adapters.factory import create_llm, create_search

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
    output = {'at': utcnow(), 'talker_model': ark.resolve(ModelRequest([], role='talker')).model,
              'thinker_model': ark.resolve(ModelRequest([], role='thinker')).model, 'checks': {},
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
        async for item in ark.stream(ModelRequest([{'role': 'user', 'content': '用一句话描述秋天的天空。'}])):
            if item['type'] == 'delta':
                if first is None:
                    first = (time.perf_counter()-start)*1000
                content += item['text']
            else:
                message = item['message']
        return {'first_delta_ms': first, 'text': content, 'streaming': message is not None}
    async def tool_probe(thinker):
        msg = await ark.complete(ModelRequest([{'role':'system','content':'必须调用probe_temperature工具，不要只用文字。'}, {'role':'user','content':'把空调设为23度'}], tools=[tool], role='thinker' if thinker else 'talker', reasoning=thinker))
        calls = msg.message.get('tool_calls', [])
        if not calls:
            raise RuntimeError('模型响应没有工具调用')
        return {'tool_calls': calls, 'has_reasoning_content': bool(msg.message.get('reasoning_content'))}
    async def search_probe():
        result = await web.search('Tavily Search API documentation', 2)
        urls = [r['url'] for r in result.get('results', [])]
        extracted = await web.extract(urls[:1]) if urls else {'results': []}
        return {'queried_at': result['queried_at'], 'urls': urls, 'search_count': len(urls), 'extract_count':len(extracted.get('results', []))}
    await asyncio.gather(check('talker_stream', stream_probe), check('talker_function_call', lambda:tool_probe(False)), check('thinker_function_call', lambda:tool_probe(True)), check('web_search_extract', search_probe))
    write_report(output, report_path('api-probe'))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output

if __name__=='__main__':
    report = asyncio.run(main())
    raise SystemExit(0 if all(check['ok'] for check in report['checks'].values()) else 1)
