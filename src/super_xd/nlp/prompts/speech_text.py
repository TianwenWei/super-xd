"""A display-syntax guard for real speech, never a canned response generator.

Markdown is parsed with markdown-it-py and rendered to plain sentences; the
statements themselves (facts, negation, conditions) are never edited.
"""
from super_xd.nlp.prompts.speech import feedback_text
import json

from markdown_it import MarkdownIt

from super_xd.nlp.textutil import collapse_ws, halfwidth_alnum, remove_urls

_MARKDOWN = MarkdownIt('commonmark').enable('table').enable('strikethrough')
_LABELS = {'结论', '调研结果', '查询结果', '详细报告', '来源', '参考来源', '回答', '建议', '核查结果', '结果'}
_NUMBER_CHARS = frozenset('0123456789.')


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
        labels = {key: feedback_text('format.labels.' + key) for key in ('summary', 'answer', 'spoken_summary', 'reason', 'error', 'temperature', 'status')}
        return '；'.join(feedback_text('format.json_item', label=labels.get(str(k), str(k)), value=_json_words(v)) for k, v in value.items())
    if isinstance(value, list):
        return '、'.join(_json_words(item) for item in value)
    if value is True: return feedback_text('format.true')
    if value is False: return feedback_text('format.false')
    if value is None: return feedback_text('format.none')
    return str(value)


def _inline_json(text):
    """Speak embedded JSON objects; a bare ``[1]`` stays for citation removal."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        if isinstance(parsed, (dict, list)):
            return _json_words(parsed)
    decoder, offset = json.JSONDecoder(), 0
    while True:
        candidates = [i for i in (text.find('{', offset), text.find('[', offset)) if i >= 0]
        if not candidates:
            return text
        start = min(candidates)
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


def _is_citation(inner):
    if inner.startswith(('cite:', 'turn')):
        return True
    compact = inner.replace(' ', '')
    return bool(compact) and compact[0].isdigit() and all(c.isdigit() or c in ',，-' for c in compact)


def _remove_citations(text):
    """Drop provider citation markers: private-use spans, 【…†…】, [1], [cite:…]."""
    output, index = [], 0
    while index < len(text):
        character = text[index]
        if character == '':
            end = text.find('', index + 1)
            if end >= 0:
                index = end + 1
                continue
        if character in '【[':
            close = '】' if character == '【' else ']'
            end = text.find(close, index + 1)
            if end >= 0:
                inner = text[index + 1:end]
                if (character == '【' and '†' in inner and '】' not in inner) or (
                        character == '[' and '[' not in inner and _is_citation(inner)):
                    index = end + 1
                    continue
        output.append(character)
        index += 1
    return ''.join(output)


def _inline_text(token):
    """Plain text of an inline token; soft/hard breaks start new lines."""
    parts = []
    for child in token.children or []:
        if child.type in {'text', 'code_inline'}:
            parts.append(child.content)
        elif child.type == 'image':
            parts.append(child.content)
        elif child.type in {'softbreak', 'hardbreak'}:
            parts.append('\n')
    return ''.join(parts)


def _blocks(text):
    """Render Markdown block tokens into plain lines, tables as spoken rows."""
    lines, table, row, cell = [], None, None, None
    for token in _MARKDOWN.parse(text):
        if token.type == 'table_open':
            table = {'head': [], 'body': []}
        elif token.type == 'table_close':
            headers = table['head'][0] if table['head'] else []
            for cells in table['body']:
                lines.append('，'.join(feedback_text('format.json_item', label=headers[i], value=value) if i < len(headers) and headers[i] else value
                                      for i, value in enumerate(cells) if value) + '。')
            table = None
        elif token.type == 'thead_open':
            cell = 'head'
        elif token.type == 'tbody_open':
            cell = 'body'
        elif token.type == 'tr_open':
            row = []
        elif token.type == 'tr_close' and table is not None:
            table[cell].append(row)
        elif token.type == 'inline':
            content = _inline_text(token)
            if table is not None:
                row.append(collapse_ws(content))
            else:
                lines.extend(content.split('\n'))
        elif token.type in {'fence', 'code_block'}:
            lines.extend(token.content.split('\n'))
    return lines


def _strip_list_marker(line):
    """Plain-text list markers that Markdown does not parse (• and 1、)."""
    line = line.lstrip()
    if line.startswith('•'):
        return line[1:].lstrip()
    digits = len(line) - len(line.lstrip('0123456789'))
    if digits and line[digits:digits + 1] == '、':
        return line[digits + 1:].lstrip()
    return line


def _number_before(text, end):
    """Start index of a decimal number ending at ``end`` (spaces skipped), or None."""
    position = end
    while position > 0 and text[position - 1] == ' ':
        position -= 1
    start = position
    while start > 0 and text[start - 1] in _NUMBER_CHARS:
        start -= 1
    if start > 0 and text[start - 1] == '-':
        start -= 1
    number = text[start:position]
    return (start, position) if any(c.isdigit() for c in number) else None


def _spoken_units(text):
    """``3.5℃`` → ``3.5度`` and ``50%`` → ``百分之50`` for TTS."""
    output, index = [], 0
    while index < len(text):
        unit = '℃' if text[index] == '℃' else '°C' if text.startswith('°C', index) else '%' if text[index] == '%' else None
        if unit:
            joined = ''.join(output)
            span = _number_before(joined, len(joined))
            if span:
                start, end = span
                number = joined[start:end]
                output = [joined[:start] + (number + '度' if unit != '%' else '百分之' + number)]
                index += len(unit)
                continue
        output.append(text[index])
        index += 1
    return ''.join(output)


def _collapse_stops(text):
    output = []
    for character in text:
        if character in '。；' and output and output[-1] in '。；':
            output[-1] = '。'
            continue
        output.append(character)
    return ''.join(output).strip(' 。；')


def speech_text(text: str) -> str:
    """Remove markup while preserving statements, negation and conditions.

    Called only on complete eligible text, so split URL/Markdown deltas, numbers
    and units cannot leak through an incremental boundary. No semantic length
    cutoff is applied here; the model/receipt/result contract controls length.
    """
    text = _inline_json(halfwidth_alnum(str(text or '')).strip())
    lines = _blocks(text)
    output, had_url = [], False
    for line in lines:
        line, found = remove_urls(_remove_citations(line))
        had_url = had_url or found
        line = _strip_list_marker(line)
        for marker in '`*_~[]|':
            line = line.replace(marker, '' if marker != '|' else '，')
        line = collapse_ws(line).strip(' ，\t')
        if not line or all(c in '-:，' for c in line):
            continue
        if line.strip('：:。') in _LABELS:
            continue
        if line.endswith(('：', ':')):
            line = line[:-1]
        if line:
            output.append(line if line[-1] in '。！？!?；;' else line + '。')
    text = _collapse_stops(_spoken_units(''.join(output)))
    if text: return text + ('' if text[-1] in '。！？!?' else '。')
    return feedback_text('format.url_fallback') if had_url else ''
