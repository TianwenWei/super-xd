"""Narrow literal refusal layers for covered vehicle authorization conflicts.

None means this guard found no covered literal conflict, not that it proved
intent authorization. It never selects a function, modifies its arguments,
executes an action or infers the current vehicle state from user/model text.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

_TARGET = r"氛围灯(?:\s*(?:的\s*)?(?:电源|开关))?"
_OPEN = r"(?:打开|开启|开(?!关))"
_CLOSE = r"(?:关闭|关掉|熄灭|关着|关(?!键))"
_HOLD_PATTERNS = (
    rf"(?:先\s*)?(?:不要|别)\s*{_OPEN}\s*{_TARGET}",
    rf"(?:先\s*)?(?:不要|别)\s*把\s*{_TARGET}\s*{_OPEN}",
    rf"{_TARGET}\s*(?:先\s*)?(?:不要|别)\s*{_OPEN}",
    rf"{_TARGET}\s*(?:先\s*)?(?:别动|不要动|保持原状|保持现状|保持不变|维持原状)",
    rf"(?:先\s*)?(?:别动|不要动)\s*{_TARGET}",
    rf"(?:保持|维持)\s*{_TARGET}\s*(?:原状|现状|不变)",
)
_SETTING_PATTERNS = (
    ("explicit_enable", rf"{_OPEN}\s*{_TARGET}"),
    ("explicit_enable", rf"{_TARGET}\s*(?:设为|设置为|改为|保持)?\s*{_OPEN}"),
    ("explicit_disable", rf"{_CLOSE}\s*{_TARGET}"),
    ("explicit_disable", rf"{_TARGET}\s*(?:设为|设置为|改为|保持)?\s*{_CLOSE}"),
    ("explicit_disable", rf"{_TARGET}\s*(?:不要亮了|别亮了)"),
)
# A local correction/negation immediately before a quoted action is not an
# affirmative instruction. This handles “不是关闭X，是打开X” without routing.
_NEGATED_PREFIX = re.compile(r"(?:不是(?:说|要|说要)?|并非|并不是(?:要)?|不要|别|不想|不需要|无需)\s*(?:把\s*)?$")


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATED_PREFIX.search(text[max(0, start - 16):start]))


def _evidence(text: str, patterns, meaning: str) -> list[dict[str, Any]]:
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            if not _negated(text, match.start()):
                values.append({'start': match.start(), 'end': match.end(),
                               'text': match.group(), 'meaning': meaning})
    return values


def _snapshot_value(snapshot: Any) -> tuple[bool, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('lights'), dict):
        return False, None
    lights = snapshot['lights']
    return ('ambient' in lights, deepcopy(lights.get('ambient')))


def validate_literal_authorization(text: str, tool_name: str, arguments: dict,
                                   snapshot: dict) -> dict[str, Any] | None:
    """Refuse covered power holds/conflicts, with exact source spans as evidence.

    Only ``set_ambient_light`` writes ``lights.ambient`` here. Color and
    brightness changes have separate properties and never match this guard.
    An explicit hold refuses even a same-value power write: “don't open” is
    not permission to send ``enabled=false``. Contradictory affirmative power
    settings or a literal setting that opposes the supplied boolean need
    clarification; no replacement boolean/tool is returned.
    """
    if tool_name != 'set_ambient_light' or not isinstance(text, str) or not text:
        return None
    if not isinstance(arguments, dict) or type(arguments.get('enabled')) is not bool:
        return None  # The existing JSON Schema layer owns malformed arguments.
    holds = _evidence(text, _HOLD_PATTERNS, 'keep_current_power')
    settings = []
    for meaning, pattern in _SETTING_PATTERNS:
        for item in _evidence(text, (pattern,), meaning):
            # “打开” nested inside “不要打开” is not a second setting.
            if not any(item['start'] < hold['end'] and hold['start'] < item['end'] for hold in holds):
                settings.append(item)
    # Deduplicate overlaps from equivalent literal forms without changing text.
    evidence = sorted({(item['start'], item['end'], item['meaning']): item
                       for item in holds + settings}.values(), key=lambda item: (item['start'], item['end']))
    targets = {item['meaning'] == 'explicit_enable' for item in settings}
    contradictory = bool(holds and settings) or len(targets) > 1
    mismatch = len(targets) == 1 and arguments['enabled'] not in targets
    if not holds and not contradictory and not mismatch:
        return None
    conflict = contradictory or mismatch
    known, current = _snapshot_value(snapshot)
    reason = ('原句中的氛围灯电源要求与本次设置冲突，需澄清；未执行电源变更。'
              if conflict else '原句要求氛围灯电源保持原状，不构成开启或关闭授权；未执行电源变更。')
    return {'kind': 'literal_authorization_conflict' if conflict else 'literal_keep_state',
            'executed': False, 'needs_clarification': conflict,
            'state_path': 'lights.ambient', 'current_value_known': known, 'current_value': current,
            'requested_value': arguments['enabled'], 'evidence': evidence,
            'reason': reason,
            'spoken_reason': '氛围灯电源要求有冲突，先保持当前状态，请确认是保持不动还是明确开关？'
                if conflict else '氛围灯电源保持原状，没有调整。'}


# These explicitly covered feature families are rejection boundaries, not a
# tool router. Lighting comparisons do not broaden the original comfort group.
# Keeping the mapping here independent of catalog dispatch means no substitute
# operation or arguments can be produced by this module.
_FEATURES = {
    'set_mirror_heating': ('mirror_heating', '后视镜加热', r'后视镜\s*(?:的\s*)?加热'),
    'set_steering_heating': ('steering_heating', '方向盘加热', r'方向盘\s*(?:的\s*)?加热'),
    'set_seat_heating': ('seat_heating', '座椅加热', r'座椅\s*(?:的\s*)?加热'),
    'set_seat_ventilation': ('seat_ventilation', '座椅通风', r'座椅\s*(?:的\s*)?通风'),
    'set_seat_massage': ('seat_massage', '座椅按摩', r'座椅\s*(?:的\s*)?按摩'),
    'set_ambient_light': ('ambient_power', '氛围灯电源', _TARGET),
    'set_interior_light': ('interior_light', '车内照明灯', r'车内(?:照明)?灯'),
}
_FEATURE_FAMILIES = (
    frozenset({'mirror_heating', 'steering_heating', 'seat_heating', 'seat_ventilation', 'seat_massage'}),
    frozenset({'ambient_power', 'interior_light'}),
)
_FEATURE_LITERAL = re.compile('|'.join(
    rf'(?P<{feature}>{pattern})' for feature, _, pattern in _FEATURES.values()))
_FEATURE_CLAUSE = re.compile(r'[^，,。；;！？!?\n]+')
_FEATURE_DIRECTIVE = re.compile(
    r'打开|开启|关闭|关掉|关上|启用|停用|启动|停止|取消|设置|设为|设成|改为|'
    r'调到|调至|调成|调高|调低|增加|降低|减少|保持')
_FEATURE_NEGATION = re.compile(r'不是(?:说|要|说要)?|并非|并不是(?:要)?|不要|别|不想|不需要|无需')
_FEATURE_CONTRAST = re.compile(r'而是|但是|但|不过')
_FEATURE_QUESTION = re.compile(r'吗|么|有没有|是否|是不是|还是')
_FEATURE_CORRECTION = re.compile(r'^\s*(?:而是|是|应该是|我要的是)')
_FEATURE_RETRACT = re.compile(r'^\s*(?:不|不对|说错了|我说错了|更正一下)\s*$')
_LIGHT_POWER_DIRECTIVE = re.compile(r'打开|开启|关闭|关掉|关上|启用|停用|保持')


def _requested_feature_evidence(text: str, feature_names: frozenset[str],
                                directive_pattern: re.Pattern) -> list[dict[str, Any]]:
    """Read exact affirmative feature spans without interpreting parameters.

    Only explicit directive clauses or literal feature-only corrections count.
    Locally negated alternatives and state questions do not grant a write.
    The small correction handling is deliberately bounded; an uncovered phrase
    produces no authorization proof and remains the model's responsibility.
    """
    evidence: list[dict[str, Any]] = []
    earlier_negated = False
    for clause_match in _FEATURE_CLAUSE.finditer(text):
        clause = clause_match.group()
        if _FEATURE_RETRACT.fullmatch(clause):
            # “关闭X，不，关闭Y” explicitly retracts the preceding target.
            evidence.clear()
            earlier_negated = True
            continue
        matches = [match for match in _FEATURE_LITERAL.finditer(clause) if match.lastgroup in feature_names]
        if not matches or _FEATURE_QUESTION.search(clause):
            continue
        directive = bool(directive_pattern.search(clause))
        corrective = earlier_negated and bool(_FEATURE_CORRECTION.match(clause))
        feature_only = len(matches) == 1 and clause.strip() == matches[0].group()
        negative_scope = False
        previous_end = 0
        for index, match in enumerate(matches):
            prefix = clause[previous_end:match.start()]
            negatives = list(_FEATURE_NEGATION.finditer(prefix))
            contrasts = list(_FEATURE_CONTRAST.finditer(prefix))
            if contrasts and (not negatives or contrasts[-1].start() > negatives[-1].start()):
                negative_scope = False
            elif negatives:
                negative_scope = True
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
            suffix = clause[match.end():next_start]
            suffix_negative = _FEATURE_NEGATION.search(suffix)
            suffix_negated = bool(suffix_negative and re.fullmatch(
                r'\s*(?:先\s*)?', suffix[:suffix_negative.start()]))
            if negative_scope or suffix_negated:
                earlier_negated = True
            elif directive or corrective or feature_only or (earlier_negated and contrasts):
                start, end = clause_match.start() + match.start(), clause_match.start() + match.end()
                evidence.append({'start': start, 'end': end, 'text': text[start:end],
                                 'meaning': 'requested_feature', 'feature': match.lastgroup})
            previous_end = match.end()
    return evidence


def validate_literal_feature_target(text: str, tool_name: str, arguments: dict,
                                    snapshot: dict) -> dict[str, Any] | None:
    """Refuse a covered write aimed at another explicitly requested feature.

    This does not choose the matching operation, authorize its parameters or
    infer intent for pronouns. Multiple explicitly requested features are all
    permitted by this target-only check. The supplied arguments and snapshot
    are never read as user intent or modified.
    """
    selected = _FEATURES.get(tool_name)
    if selected is None or not isinstance(text, str) or not text:
        return None
    family = next(features for features in _FEATURE_FAMILIES if selected[0] in features)
    directive_pattern = _LIGHT_POWER_DIRECTIVE if selected[0] in _FEATURE_FAMILIES[1] else _FEATURE_DIRECTIVE
    evidence = _requested_feature_evidence(text, family, directive_pattern)
    requested = {item['feature'] for item in evidence}
    if not requested or selected[0] in requested:
        return None
    labels = {feature: label for feature, label, _ in _FEATURES.values()}
    requested_labels = list(dict.fromkeys(labels[item['feature']] for item in evidence))
    reason = (f'原句明确请求“{"、".join(requested_labels)}”，本次写操作指向“{selected[1]}”；'
              '部位或功能不一致，未执行。请重新核对原句中的目标。')
    return {'kind': 'literal_target_mismatch', 'executed': False, 'retry_model': True,
            'needs_clarification': False, 'selected_feature': selected[0],
            'evidence': evidence, 'reason': reason,
            'spoken_reason': '部位或功能不一致，这个操作没有执行。'}


_WINDOW_ALIASES = {
    'driver': r'(?:主驾驶(?:位|座|席)?|主驾(?:位|座)?|驾驶位|驾驶席|司机侧|左前(?:排)?|前左|left_front|driver)',
    'front_passenger': r'(?:副驾驶(?:位|座|席)?|副驾(?:位|座)?|右前(?:排)?|前右|right_front|front_passenger)',
    'rear_left': r'(?:后排左侧|后排左边|左后(?:排)?|后左|rear_left)',
    'rear_right': r'(?:后排右侧|后排右边|右后(?:排)?|后右|rear_right)',
}
_WINDOW_CONTEXT = re.compile(r'车窗|窗户|(?<!天)窗')
_WINDOW_SUFFIX = r'\s*(?:的\s*)?(?:车窗|窗户|窗)?'
_WINDOW_KEEP = r'(?:别动|不要动|保持不变|保持原状|保持现状|维持原状|保留原(?:来)?开度)'
_WINDOW_LITERAL = re.compile('|'.join(
    rf'(?P<{window}>{alias})' for window, alias in _WINDOW_ALIASES.items()))
_WINDOW_REFERENT = rf'(?:{"|".join(_WINDOW_ALIASES.values())}){_WINDOW_SUFFIX}'
_WINDOW_GROUP = rf'{_WINDOW_REFERENT}(?:\s*(?:和|及|、|以及)\s*{_WINDOW_REFERENT})*'
_WINDOW_HOLD_PATTERNS = (
    rf'{_WINDOW_GROUP}\s*(?:先\s*)?{_WINDOW_KEEP}',
    rf'{_WINDOW_GROUP}\s*(?:的\s*)?开度\s*(?:保持不变|保持原状|不变|保留)',
    rf'(?:先\s*)?(?:别动|不要动)\s*{_WINDOW_GROUP}',
    rf'(?:保持|维持)\s*{_WINDOW_GROUP}\s*(?:不变|原状|现状)',
    rf'保留\s*{_WINDOW_GROUP}\s*(?:的\s*)?(?:原来|原|当前|现在)?\s*(?:的\s*)?开度',
)


def _window_hold_evidence(text: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for clause_match in _FEATURE_CLAUSE.finditer(text):
        clause = clause_match.group()
        if _FEATURE_RETRACT.fullmatch(clause):
            evidence.clear()
            continue
        for pattern in _WINDOW_HOLD_PATTERNS:
            for match in re.finditer(pattern, clause):
                start, end = clause_match.start() + match.start(), clause_match.start() + match.end()
                if not _negated(text, start):
                    for window_match in _WINDOW_LITERAL.finditer(match.group()):
                        evidence.append({'start': start, 'end': end, 'text': text[start:end],
                                         'meaning': 'keep_current_window_position', 'window': window_match.lastgroup})
    return sorted({(item['start'], item['end'], item['window']): item
                   for item in evidence}.values(), key=lambda item: (item['start'], item['end']))


def validate_literal_window_hold(text: str, tool_name: str, arguments: dict,
                                  snapshot: dict) -> dict[str, Any] | None:
    """Reject a window-position write intersecting an explicit preserved set.

    Seat/side aliases require a literal window context; no thermal preference
    or generic seat hold is inferred. A same-value write is still a write.
    The four existing position writers share this property boundary, while
    child locks, anti-pinch, sunroof and other vehicle properties are untouched.
    """
    if not isinstance(text, str) or not _WINDOW_CONTEXT.search(text):
        return None
    if tool_name in {'close_all_windows', 'open_all_windows'}:
        written = set(_WINDOW_ALIASES)
    elif tool_name in {'set_window_position', 'adjust_window_position'}:
        if not isinstance(arguments, dict) or arguments.get('window') not in _WINDOW_ALIASES:
            return None  # Invalid arguments belong to the JSON Schema layer.
        written = {arguments['window']}
    else:
        return None
    evidence = _window_hold_evidence(text)
    preserved = [window for window in _WINDOW_ALIASES if any(item['window'] == window for item in evidence)]
    affected = [window for window in preserved if window in written]
    if not affected:
        return None
    windows = snapshot.get('windows') if isinstance(snapshot, dict) else None
    positions = windows.get('positions') if isinstance(windows, dict) else None
    positions = positions if isinstance(positions, dict) else {}
    current = {window: deepcopy(positions[window]) for window in preserved if window in positions}
    return {'kind': 'literal_window_hold', 'executed': False, 'retry_model': True,
            'needs_clarification': False, 'preserved_windows': preserved,
            'affected_windows': affected, 'current_positions': current,
            'unknown_windows': [window for window in preserved if window not in positions],
            'evidence': evidence,
            'reason': '原句要求保留的车窗在本次位置写入集合中；即使开度相同也未授权该写入，未执行。请重新核对保留约束。',
            'spoken_reason': '这个操作会覆盖要求保持不动的车窗，没有执行。'}
