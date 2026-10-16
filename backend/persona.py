"""Persona only affects presentation; tool policies and schemas are shared."""
import json
from backend.types import PersonaConfig
from backend.prompt_templates import render_prompt
from backend.speech_config import persona_text
from backend.textutil import collapse_ws

PERSONAS = {
    identity: PersonaConfig(id=identity, **{field: persona_text(identity + '.' + field)
        for field in ('name', 'tone', 'sentence_length', 'catchphrase_frequency')})
    for identity in ('professional', 'warm', 'playful')
}


def style_prompt(persona):
    return render_prompt('persona', persona_json=json.dumps(persona.model_dump(), ensure_ascii=False))


def _sentence(text):
    text = collapse_ws(text)
    return text if not text or text[-1] in '。！？.!?' else text + '。'


def _question_particle(text, particle):
    """Add a tone particle before the first question mark of a clause."""
    for index, character in enumerate(text):
        if character in '？?' and index and text[index - 1] not in '。！？.!?':
            if text[index - 1] in persona_text('clarify.existing_particles'):
                return text
            return text[:index] + particle + text[index:]
    return text


def _soften_success(text, particle):
    """Change spoken presentation, preserving the complete verified fact."""
    text = text.rstrip('。.!！')
    for original, replacement in (('current', 'adjust'), ('window_position', 'window_adjust'), ('enabled', 'enable'), ('disabled', 'disable')):
        text = text.replace(persona_text('success.replacements.' + original), persona_text('success.replacements.' + replacement), 1)
    warm_particle = persona_text('success.warm_particle')
    if particle == warm_particle:
        for original, replacement in (('adjust', 'warm_adjust'), ('open_half', 'warm_half')):
            text = text.replace(persona_text('success.replacements.' + original), persona_text('success.replacements.' + replacement), 1)
    elif text.endswith(warm_particle):
        text = text[:-len(warm_particle)] + particle
    if not text.endswith((warm_particle, persona_text('success.playful_particle'), persona_text('success.retained_particle'))):
        text += particle
    return text + '。'


def render(persona, kind, detail=''):
    style = persona.id if persona.id in PERSONAS else ('professional' if persona.catchphrase_frequency == 'never' else 'playful' if persona.catchphrase_frequency == 'normal' else 'warm')
    detail = collapse_ws(detail)
    if kind == 'success':
        return _sentence(detail) if style == 'professional' else _soften_success(detail, persona_text('success.playful_particle' if style == 'playful' else 'success.warm_particle'))
    if kind == 'delegated':
        return persona_text('delegated.text', start=persona_text('delegated.' + style), goal=detail.rstrip('。.!！'))
    if kind == 'clarify':
        # One key question is a generation contract, never a presentation
        # cutoff: later questions can themselves carry necessary conditions.
        detail = detail.replace(persona_text('clarify.remove_ui_hint'), '').strip()
        if style != 'professional':
            detail = _question_particle(detail, persona_text('clarify.playful_particle' if style == 'playful' else 'clarify.warm_particle'))
        return _sentence(detail)
    if kind in {'refused', 'error'}:
        return persona_text(kind + '.' + style) + _sentence(detail)
    if kind in {'cancel', 'updated'}:
        return persona_text(kind + '.' + style)
    # Neutral result/progress facts remain intact. Repeated result announcements
    # need no catchphrase; chat and verified control endings carry the persona.
    return _sentence(detail)
