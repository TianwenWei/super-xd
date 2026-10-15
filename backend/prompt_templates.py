"""Load fixed prompt templates once; substitute named data without recursion."""
from functools import lru_cache
from pathlib import Path

PROMPT_DIRECTORY = Path(__file__).with_name('prompts')


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if not isinstance(name, str) or not name or any(
            char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in name):
        raise ValueError('Prompt name must use lowercase ASCII letters, digits or underscores')
    text = (PROMPT_DIRECTORY / f'{name}.txt').read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError(f'Prompt {name} must not be empty')
    return text


@lru_cache(maxsize=None)
def _parts(name: str) -> tuple[tuple[bool, str], ...]:
    remaining = load_prompt(name)
    parts = []
    while remaining:
        literal, opening, remaining = remaining.partition('{{')
        if '}}' in literal:
            raise ValueError(f'Unexpected closing placeholder in {name}')
        parts.append((False, literal))
        if not opening:
            break
        variable, closing, remaining = remaining.partition('}}')
        if not closing or not variable or any(
                char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in variable):
            raise ValueError(f'Invalid placeholder in {name}')
        parts.append((True, variable))
    return tuple(parts)


def render_prompt(name: str, **variables: str) -> str:
    parts = _parts(name)
    expected = {text for variable, text in parts if variable}
    missing, extra = expected - variables.keys(), variables.keys() - expected
    if missing or extra:
        raise ValueError(f'Prompt {name}: missing={sorted(missing)}, extra={sorted(extra)}')
    if any(not isinstance(value, str) for value in variables.values()):
        raise TypeError('Prompt variables must be serialized strings')
    return ''.join(variables[text] if variable else text for variable, text in parts)


def preload_prompts() -> None:
    # File IO belongs to startup, never the first request in the actor loop.
    for path in sorted(PROMPT_DIRECTORY.glob('*.txt')):
        _parts(path.stem)


preload_prompts()
