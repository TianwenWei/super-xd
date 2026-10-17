"""Format helpers without regular expressions (AGENTS.md: 必须保持的不变量).

Only syntax is handled here: identifiers, domains, URLs, secret redaction and
whitespace. Nothing in this module interprets what a user or model meant.
"""
from __future__ import annotations

import json
import math
import os
from typing import Iterable

from markdown_it import MarkdownIt

ASCII_ALNUM = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
SECRET_ENV = ('ARK_API_KEY', 'ARK_EMBEDDING_API_KEY', 'TAVILY_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_MAPS_API_KEY',
              'VOLCENGINE_SPEECH_API_KEY', 'VOLCENGINE_SPEECH_APP_ID', 'VOLCENGINE_SPEECH_ACCESS_TOKEN',
              'VOLCENGINE_ASR_API_KEY', 'VOLCENGINE_ASR_ACCESS_TOKEN')
URL_TERMINATORS = frozenset(' \t\r\n<>"\'，。！？；）)]】》」』')
_JSON_MARKDOWN = MarkdownIt('commonmark')


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON object key')
        value[key] = item
    return value


def _invalid_json_constant(value):
    raise ValueError('Nonfinite JSON number')


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite JSON number')
    return number


def decode_json_object(raw):
    """Decode a whole JSON object or one closed JSON fence, never an excerpt.

    Markdown accepts unterminated fences and discards reference definitions;
    verify the source span and closing delimiter so neither hides extra material.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('JSON response must be nonempty text')
    text = raw.strip()
    if not text.startswith('{'):
        tokens = _JSON_MARKDOWN.parse(text)
        lines = text.splitlines()
        if (len(tokens) != 1 or tokens[0].type != 'fence'
                or tokens[0].info.strip().lower() != 'json'
                or tokens[0].map != [0, len(lines)]):
            raise ValueError('Expected one JSON object or one complete JSON fence')
        token = tokens[0]
        closing = lines[-1].strip()
        if (len(lines) < 3 or len(closing) < len(token.markup)
                or set(closing) != {token.markup[0]}
                or len(lines[-1]) - len(lines[-1].lstrip(' ')) > 3):
            raise ValueError('JSON fence must be explicitly closed')
        text = token.content
    value = json.loads(text, object_pairs_hook=_unique_json_object,
                       parse_constant=_invalid_json_constant, parse_float=_finite_json_float)
    if not isinstance(value, dict):
        raise ValueError('JSON response must be an object')
    return value


def halfwidth_alnum(text: str) -> str:
    """Full-width letters/digits (ＣＡＦＥ１７) to ASCII; CJK punctuation is kept.

    unicodedata NFKC would also turn ，！？： into ASCII, which the speech
    path relies on for sentence boundaries.
    """
    return ''.join(chr(ord(c) - 0xFEE0) if ('０' <= c <= '９' or 'Ａ' <= c <= 'Ｚ' or 'ａ' <= c <= 'ｚ')
                   else ' ' if c == '\u3000' else c for c in text)


def collapse_ws(text) -> str:
    return ' '.join(str(text or '').split())


def is_identifier(value, *, max_length: int, extra: str = '_-', alnum_first: bool = False) -> bool:
    """ASCII letters/digits plus ``extra``; used for request and capture IDs."""
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        return False
    if alnum_first and value[0] not in ASCII_ALNUM:
        return False
    allowed = ASCII_ALNUM | set(extra)
    return all(character in allowed for character in value)


def is_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdefABCDEF' for c in value)


def is_alpha2(value) -> bool:
    return isinstance(value, str) and len(value) == 2 and all(c in ASCII_ALNUM and not c.isdigit() for c in value)


def is_snake_code(value) -> bool:
    """Machine error codes such as ``planner_model_step_limit``."""
    return (isinstance(value, str) and bool(value) and 'a' <= value[0] <= 'z'
            and all('a' <= c <= 'z' or c.isdigit() and c.isascii() or c == '_' for c in value))


def has_cjk(text: str) -> bool:
    return any('㐀' <= character <= '鿿' for character in text)


def is_domain(domain) -> bool:
    """Bare host name with at least two labels; no scheme, port, path or IP."""
    if not isinstance(domain, str) or not domain or len(domain) > 253:
        return False
    labels = domain.lower().split('.')
    if len(labels) < 2 or labels[-1].isdigit():
        return False
    allowed = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')
    return all(1 <= len(label) <= 63 and set(label) <= allowed
               and label[0] != '-' and label[-1] != '-' for label in labels)


def is_placeholder(value: str) -> bool:
    """Template values like ``your-key``, ``xxx``, ``<token>`` or ``${VAR}``."""
    if '$(' in value or '${' in value:
        return True
    lowered = value.lower()
    if len(lowered) >= 3 and (set(lowered) == {'x'} or set(lowered) == {'*'}):
        return True
    if lowered in {'changeme', 'placeholder', 'todo'}:
        return True
    for prefix in ('your', 'replace'):
        if lowered == prefix or (lowered.startswith(prefix) and lowered[len(prefix)] in '-_ '):
            return True
    return len(value) >= 2 and value[0] == '<' and value[-1] == '>'


def find_urls(text: str) -> list[str]:
    """Bare http(s) URLs, cut at whitespace or CJK/closing punctuation."""
    urls, cursor = [], 0
    lowered = text.lower()
    while True:
        starts = [index for index in (lowered.find('http://', cursor), lowered.find('https://', cursor)) if index >= 0]
        if not starts:
            return urls
        start = min(starts)
        end = start
        while end < len(text) and text[end] not in URL_TERMINATORS:
            end += 1
        urls.append(text[start:end])
        cursor = max(end, start + 1)


def remove_urls(text: str) -> tuple[str, bool]:
    """Drop bare http(s)/www URLs; return the text and whether any was found."""
    found, output, index = False, [], 0
    lowered = text.lower()
    while index < len(text):
        if lowered.startswith(('http://', 'https://', 'www.'), index):
            found = True
            while index < len(text) and text[index] not in URL_TERMINATORS:
                index += 1
            continue
        output.append(text[index])
        index += 1
    return ''.join(output), found


def _redact_after(text: str, marker: str, stop: str, replacement: str, *, separator: str | None = None) -> str:
    """Replace the value following each case-insensitive ``marker``."""
    lowered, output, cursor = text.lower(), [], 0
    marker = marker.lower()
    while True:
        index = lowered.find(marker, cursor)
        if index < 0:
            output.append(text[cursor:])
            return ''.join(output)
        start = index + len(marker)
        position = start
        if separator is None:
            # "Bearer <token>": at least one whitespace character is required.
            while position < len(text) and text[position].isspace():
                position += 1
            if position == start:
                output.append(text[cursor:start]); cursor = start
                continue
        else:
            while position < len(text) and text[position] in '"\' \t':
                position += 1
            if position >= len(text) or text[position] not in separator:
                output.append(text[cursor:start]); cursor = start
                continue
            position += 1
            while position < len(text) and text[position] in ' \t':
                position += 1
        end = position
        while end < len(text) and text[end] not in stop and not (separator is None and text[end].isspace()):
            end += 1
        if end == position:
            output.append(text[cursor:position]); cursor = position
            continue
        output.append(text[cursor:index] + replacement)
        cursor = end


def redact_bearer(text: str) -> str:
    return _redact_after(text, 'bearer', ',;', 'Bearer [redacted]')


def redact_header_values(text: str, names: Iterable[str]) -> str:
    for name in names:
        text = _redact_after(text, name, '\r\n,}', f'{name}: [redacted]', separator=':=')
    return text


def redact_prefixed_tokens(text: str, prefixes=('sk-', 'tvly-'), minimum: int = 10) -> str:
    """Mask provider-shaped keys that may not be present in this environment."""
    allowed = ASCII_ALNUM | {'_', '-'}
    output, index = [], 0
    while index < len(text):
        prefix = next((p for p in prefixes if text.startswith(p, index)
                       and (index == 0 or text[index - 1] not in allowed)), None)
        if prefix:
            end = index + len(prefix)
            while end < len(text) and text[end] in allowed:
                end += 1
            if end - index - len(prefix) >= minimum:
                output.append('[redacted]')
                index = end
                continue
        output.append(text[index])
        index += 1
    return ''.join(output)


def redact_known_secrets(text: str, extra: Iterable[str] = ()) -> str:
    secrets = {os.getenv(name) for name in SECRET_ENV} | set(extra)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    return text


def redact(text: str, extra: Iterable[str] = ()) -> str:
    return redact_prefixed_tokens(redact_bearer(redact_known_secrets(str(text), extra)))
