"""Grade manual retrieval by source page IDs, never by generated answer text."""
import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from statistics import median
from types import SimpleNamespace

from backend.settings import ROOT
from scripts.manual_index import DEFAULT_DB, search


async def run(args):
    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        for repeat in range(args.repeat):
            output = io.StringIO()
            with redirect_stdout(output):
                await search(SimpleNamespace(db=args.db, question=case['query'], top_k=5))
            result = json.loads(output.getvalue())
            pages = [hit['page'] for hit in result['hits']]
            passed = bool(set(pages) & set(case['expected_pages']))
            results.append({'id': case['id'], 'repeat': repeat + 1, 'passed': passed,
                            'expected_pages': case['expected_pages'], 'retrieval': result})
            print(json.dumps({'id': case['id'], 'repeat': repeat + 1,
                              'passed': passed, 'retrieved_pages': pages}), flush=True)
    report = {'passed': sum(item['passed'] for item in results), 'total': len(results),
              'embedding_ms_p50': median(item['retrieval']['embedding_ms'] for item in results),
              'local_search_ms_p50': median(item['retrieval']['local_search_ms'] for item in results),
              'results': results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}))
    return report['passed'] == report['total']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('manual_retrieval_cases.json'))
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--repeat', type=int, choices=range(1, 11), default=1)
    parser.add_argument('--out', type=Path, default=ROOT / 'data/evals/manual_retrieval.json')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
