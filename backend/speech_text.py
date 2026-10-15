"""A display-syntax guard for real speech, never a canned response generator."""
import json
import re


def spoken_number(value):
    number = str(value)
    negative = number.startswith('-')
    if negative: number = number[1:]
    whole, _, fraction = number.partition('.')
    if not whole.isdigit() or int(whole) > 100: return str(value)
    digits = '零一二三四五六七八九'
    n = int(whole)
    if n == 100: words = '一百'
    elif n < 10: words = digits[n]
    else: words = (digits[n // 10] if n >= 20 else '') + '十' + (digits[n % 10] if n % 10 else '')
    fraction = fraction.rstrip('0')
    if fraction: words += '点' + ''.join(digits[int(d)] for d in fraction)
    return ('负' if negative else '') + words


def _json_words(value):
    if isinstance(value, dict):
        labels = {'summary': '结论', 'answer': '回答', 'spoken_summary': '结论',
                  'reason': '原因', 'error': '问题', 'temperature': '温度', 'status': '状态'}
        return '；'.join(f'{labels.get(str(k), str(k))}是{_json_words(v)}' for k, v in value.items())
    if isinstance(value, list):
        return '、'.join(_json_words(item) for item in value)
    if value is True: return '是'
    if value is False: return '否'
    if value is None: return '未提供'
    return str(value)


def _table_words(lines):
    result, index = [], 0
    while index < len(lines):
        line = lines[index]
        if '|' not in line or index + 1 >= len(lines) or not re.fullmatch(r'[\s|:\-]+', lines[index + 1]):
            result.append(line)
            index += 1
            continue
        headers = [v.strip() for v in line.strip().strip('|').split('|')]
        index += 2
        while index < len(lines) and '|' in lines[index]:
            cells = [v.strip() for v in lines[index].strip().strip('|').split('|')]
            result.append('，'.join(f'{headers[i]}是{cell}' if i < len(headers) else cell
                                   for i, cell in enumerate(cells) if cell) + '。')
            index += 1
    return result


def speech_text(text: str) -> str:
    """Remove markup while preserving statements, negation and conditions.

    Called only on complete eligible text, so split URL/Markdown deltas, numbers
    and units cannot leak through an incremental boundary. No semantic length
    cutoff is applied here; the model/receipt/result contract controls length.
    """
    text = str(text or '').strip()
    text = re.sub(r'^```[^\n]*\n?', '', text)
    text = re.sub(r'```\s*$', '', text)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        if isinstance(parsed, (dict, list)):
            text = _json_words(parsed)
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        match = re.search(r'[\{\[]', text[offset:])
        if not match: break
        start = offset + match.start()
        if text[start] == '[' and text[:start].rstrip()[-1:] not in {':', '：', '='}:
            offset = start + 1
            continue
        try:
            value, length = decoder.raw_decode(text[start:])
        except ValueError:
            offset = start + 1
            continue
        if isinstance(value, (dict, list)):
            words = _json_words(value)
            text = text[:start] + words + text[start + length:]
            offset = start + len(words)
        else:
            offset = start + length
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1', text)
    had_url = bool(re.search(r'https?://|www\.', text))
    text = re.sub(r'(?:https?://|www\.)[^\s<>，。！？；）)\]]+', '', text)
    text = re.sub(r'[^]*|【[^】]*†[^】]*】|\[(?:\d+(?:\s*[,，-]\s*\d+)*|cite:[^\]]*|turn\d+[^\]]*)\]', '', text)
    text = re.sub(r'(?m)^\s*\[\d+\]:.*$', '', text)
    lines = _table_words(text.splitlines())
    output = []
    for line in lines:
        line = re.sub(r'^\s*(?:#{1,6}\s*|>\s*|[-*+•]\s+|\d+(?:[.)]\s+|、\s*))', '', line)
        line = re.sub(r'[`*_~]+', '', line)
        line = re.sub(r'<[^>]+>', '', line)
        line = re.sub(r'\[([^\]]+)\]', r'\1', line)
        line = line.replace('|', '，')
        line = re.sub(r'\s+', ' ', line).strip(' |\t')
        if not line or re.fullmatch(r'[-:|\s]+', line): continue
        if line.strip('：:。') in {'结论', '调研结果', '查询结果', '详细报告', '来源', '参考来源', '回答', '建议', '核查结果', '结果'}:
            continue
        if line.endswith(('：', ':')):
            line = line[:-1]
        if line:
            output.append(line if line[-1] in '。！？!?；;' else line + '。')
    text = ''.join(output)
    text = re.sub(r'(-?\d+(?:\.\d+)?)\s*(?:℃|°C)', r'\1度', text)
    text = re.sub(r'(-?\d+(?:\.\d+)?)\s*%', r'百分之\1', text)
    text = re.sub(r'[。；]{2,}', '。', text).strip(' 。；')
    text = re.sub(r'^(?:(?:好的|好呀|收到)[，。！\s]*|(?:以下是结果|以下为你整理|查询结果)[：:。\s]*)+', '', text)
    if text: return text + ('' if text[-1] in '。！？!?' else '。')
    return '相关链接在详情里。' if had_url else ''


def brief_result(text: str, max_chars: int = 120, max_sentences: int = 3) -> str:
    """Compatibility guard; length budgets belong to the generation contract.

    A deterministic guard cannot know which later facts qualify an earlier
    recommendation. Preserve all statements when a legacy report lacks its
    own generated spoken summary. The old budget arguments remain accepted
    for callers, but never cause sentence or character truncation.
    """
    return speech_text(text)
