"""Persona only affects presentation; tool policies and schemas are shared."""
import json
from backend.types import PersonaConfig
from backend.prompt_templates import render_prompt
from backend.textutil import collapse_ws

PERSONAS = {
    'professional': PersonaConfig(id='professional', name='简洁专业', tone='准确、克制、直达重点', sentence_length='short', catchphrase_frequency='never'),
    'warm': PersonaConfig(id='warm', name='温暖自然', tone='温和自然、像熟悉的同行者', sentence_length='short', catchphrase_frequency='low'),
    'playful': PersonaConfig(id='playful', name='轻松活泼', tone='轻松有活力，少量俏皮，不夸大事实，不重复口头禅', sentence_length='short', catchphrase_frequency='low'),
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
            if text[index - 1] in '吗呢呀吧':
                return text
            return text[:index] + particle + text[index:]
    return text


def _soften_success(text, particle):
    """Change spoken presentation, preserving the complete verified fact."""
    text = text.rstrip('。.!！')
    text = text.replace('现在是', '调到', 1).replace('车窗开度', '车窗开到', 1)
    text = text.replace('已开启', '开', 1).replace('已关闭', '关', 1)
    if particle == '了':
        text = text.replace('调到', '调成', 1).replace('打开一半', '开到一半', 1)
    elif text.endswith('了'):
        text = text[:-1] + particle
    if not text.endswith(('了', '啦', '着')):
        text += particle
    return text + '。'


def render(persona, kind, detail=''):
    style = persona.id if persona.id in PERSONAS else ('professional' if persona.catchphrase_frequency == 'never' else 'playful' if persona.catchphrase_frequency == 'normal' else 'warm')
    detail = collapse_ws(detail)
    if kind == 'success':
        return _sentence(detail) if style == 'professional' else _soften_success(detail, '啦' if style == 'playful' else '了')
    if kind == 'delegated':
        goal = detail.rstrip('。.!！')
        start = {'professional': '我来', 'warm': '我帮你', 'playful': '我去'}[style]
        return f'{start}{goal}，有结果告诉你。'
    if kind == 'clarify':
        # One key question is a generation contract, never a presentation
        # cutoff: later questions can themselves carry necessary conditions.
        detail = detail.replace('可以在对应的任务卡里补充。', '').strip()
        if style != 'professional':
            particle = '呀' if style == 'playful' else '呢'
            detail = _question_particle(detail, particle)
        return _sentence(detail)
    if kind in {'refused', 'error'}:
        start = ({'professional': '未执行，', 'warm': '这次没能执行，', 'playful': '这次没做成，'}
                 if kind == 'refused' else
                 {'professional': '这次未完成，', 'warm': '这次没能完成，', 'playful': '这次没完成，'})[style]
        return start + _sentence(detail)
    if kind == 'cancel':
        return {'professional': '正在取消这项任务。', 'warm': '我正在取消这件事。', 'playful': '正在停下这件事。'}[style]
    if kind == 'updated':
        return {'professional': '补充条件已记下。', 'warm': '新条件记下了。', 'playful': '新要求记下啦。'}[style]
    # Neutral result/progress facts remain intact. Repeated result announcements
    # need no catchphrase; chat and verified control endings carry the persona.
    return _sentence(detail)
