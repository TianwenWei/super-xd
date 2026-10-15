"""Single-writer session actor. Network/model callbacks only enqueue events."""
import asyncio
import copy
import json
import logging
import math
import time
import unicodedata
from dataclasses import dataclass, field
from uuid import uuid4
from zoneinfo import ZoneInfo
from jsonschema import validate, ValidationError
from backend.catalog import TOOLS, SKILLS, HOT_TOOL_NAMES, initial_vehicle_state, execute_tool
from backend.vehicle_planner import (VehicleControlPlanner, destination_grounded, validate_vehicle_plan,
                                     validate_vehicle_action_keep, _write_paths, _overlap)
from backend.persona import PERSONAS, style_prompt, render
from backend.prompt_templates import render_prompt
from backend.types import PersonaConfig, SessionSnapshot
from backend.settings import TOOL_TIMEOUT
from backend.models import utcnow, sanitized_error
from backend.model_context import compact_message, compact_tool_result
from backend.speech_text import speech_text, brief_result, spoken_number
from backend.google_maps import GoogleMapsClient, MapsError
from backend.vehicle_simulator import VehicleSimulator
from backend.vehicle_feedback import dispatch_feedback
from backend.maps_tools import MAPS_TOOLS, MAPS_BY_NAME, READ_TOOLS
from backend.textutil import is_domain, is_identifier, is_snake_code
from backend.db import TERMINAL as TERMINAL_TASK, step_summary, task_card


def function(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}}}

STR = {'type': 'string', 'minLength': 1, 'maxLength': 4000}
TASK_ID = {'type': 'string', 'minLength': 1, 'pattern': r'[\s\S]*\S[\s\S]*', 'description': '明确任务id；仅有一个活跃任务可省略'}
CONTROL_TOOLS = [
    function('delegate_vehicle_control', '委托专门车控规划层处理非热工具或复杂组合车控。只选择涉及的子系统；系统传递未经改写的本轮原句，不要在参数里重写需求。混合热与非热车控必须整句只委托一次，不能同时调用任何车控函数。此工具等待实际执行回执，不是后台调研建议。',
             {'skill_ids': {'type': 'array', 'items': {'type': 'string', 'enum': list(SKILLS)},
                            'maxItems': 10, 'uniqueItems': True,
                            'description': '涉及的车辆子系统；不能确定可传空数组，由规划层选择。'}}, ['skill_ids']),
    function('delegate_task', '复杂隐含意图、多步推理、实时信息委托后台，保留goal完整要求。',
             {'goal': STR, 'spoken_goal': {'type': 'string', 'maxLength': 80, 'description': '自然短称，概括主题约8–15字；不复述查询过程/来源要求，完整条件全部写goal；不能称已完成'},
              'read_details': {'type': 'boolean', 'description': '仅用户明确要求详细朗读最终报告时为true，默认false'},
              'query_domain': {'type': 'string', 'enum': ['weather', 'general'], 'description': '城市天气预报/气温查询填weather，以核对目标日预报证据；本原型weather入口仅支持quick，必须同时显式填写query_mode=quick，包括指定官网天气查询。车内温控不是天气。其他任务general。'},
              'query_mode': {'type': 'string', 'enum': ['quick', 'research'], 'description': 'quick用于一个明确地点/日期的简单事实查询；query_domain=weather在本原型只能显式选quick，不能research或省略模式。其他领域的比较、调研、多条件规划或复杂推理可research。缺少必要地点先询问，不委托空目标。'},
              'required_source_domains': {'type': 'array', 'maxItems': 5, 'uniqueItems': True,
                                          'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                                          'description': '仅当用户要求核实某个官方网站且你确知其域名时填写，如metoffice.gov.uk；不确定就不填，不要问用户要网址。'}}, ['goal']),
    function('manage_task', '处理已有任务。等待澄清时，用户提供该任务缺少的城市/日期等条件必须reply原任务，不另delegate；补充或修改已有任务的条件用update。多个明确任务的进度用task_ids一次读取全部。车况与新话题不使用本工具。',
             {'action': {'type':'string','enum':['get','reply','update','cancel'], 'description':'get读取进度或结果；reply回答等待补充任务的问题；update补充或修改已有任务条件；cancel取消任务。'}, 'task_id': TASK_ID,
              'task_ids': {'type':'array','items':TASK_ID,'minItems':1,'maxItems':8,'uniqueItems':True,'description':'仅get可用，与task_id互斥；用户指定的所有任务ID，不漏任何一项'},
              'text': {**STR, 'description':'reply时完整保留对等待补充问题的回答；update时完整保留新增或修改条件，不丢失地点、日期、筛选和来源要求。'},
              'read_details': {'type':'boolean','description':'仅用户明确要求详细朗读已有结果时为true，默认false'}}, ['action']),
]
CONTROL_BY_NAME = {t['function']['name']: t for t in CONTROL_TOOLS}
CORE_PROMPT = render_prompt(
    'talker', hot_tool_names=', '.join(HOT_TOOL_NAMES),
    skill_index='; '.join(f'{key}:{value.name}，{value.description}' for key, value in SKILLS.items()))
SPEECH_ROUTE_PROMPT = render_prompt('speech_route')
CHAT_SPEECH_PROMPT = render_prompt('speech_chat')
CURRENT_TURN_PROMPT = render_prompt('current_turn')
TASK_SNAPSHOT_NOTICE = render_prompt('task_snapshot_notice')


DESTINATION_NOT_GROUNDED = {
    'ok': False, 'executed': False, 'retry_model': True, 'code': 'destination_not_in_request',
    'reason': '目的地必须逐字取自用户原话、近期对话或本会话查到的地点名，不能改写、补全或猜测；未执行。'
              '用户说的是相对地点（附近、最近）且没有给出位置时，直接问用户在哪里。',
    'spoken_reason': '目的地还没确认，暂时没有修改。你要去哪里？'}


def effective_text(text):
    """Metric only: at least four letters/digits/CJK characters have streamed."""
    return sum(1 for c in text if unicodedata.category(c)[0] in 'LN') >= 4


def state_path_value(state, path):
    node = state
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f'保持项状态路径不存在：{path}')
        node = node[key]
    return copy.deepcopy(node)


def same_state_value(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, allow_nan=False)


def receipt_detail(name, args, result):
    seat = {'driver': '主驾', 'front_passenger': '副驾', 'rear_left': '左后排', 'rear_right': '右后排'}
    zone = {'all': '所有', 'driver': '主驾', 'front_passenger': '副驾', 'rear': '后排', 'front': '前排'}
    enabled = '开启' if args.get('enabled') else '关闭'
    if name == 'set_climate_power': return f'空调已{enabled}。'
    if name == 'set_temperature': return f"{'' if args['zone'] == 'all' else zone.get(args['zone'], args['zone'])}空调调到{spoken_number(args['temperature'])}度了。"
    if name == 'adjust_temperature':
        temperatures = result['value']
        selected = temperatures if args['zone'] == 'all' else {args['zone']: temperatures[args['zone']]}
        if len(set(selected.values())) == 1:
            label = '所有' if args['zone'] == 'all' else zone[args['zone']]
            return f"{label}空调调到{spoken_number(next(iter(selected.values())))}度了。"
        return '空调已调整为' + '、'.join(f"{zone[key]}{spoken_number(value)}度" for key, value in selected.items()) + '。'
    if name == 'set_fan_speed': return f"风量设为{args['level']}档。"
    if name == 'set_window_position':
        position = args['position']
        detail = '关好了' if position == 0 else '全打开了' if position == 100 else '打开一半了' if position == 50 else f'开到百分之{position:g}了'
        return f"{seat.get(args['window'],args['window'])}车窗{detail}。"
    if name == 'close_all_windows': return '全部车窗已关闭。'
    if name == 'set_window_child_lock': return f"后排车窗童锁已{'开启' if result['value'] else '关闭'}。"
    if name == 'set_seat_heating': return f"{seat.get(args['seat'],args['seat'])}座椅加热" + (f"设为{args['level']}档。" if args['level'] else '已关闭。')
    if name == 'set_seat_ventilation': return f"{seat.get(args['seat'],args['seat'])}座椅通风" + (f"设为{args['level']}档。" if args['level'] else '已关闭。')
    if name == 'set_seat_massage':
        mode = {'relax': '放松', 'lumbar': '腰部', 'wave': '波浪', 'off': '关闭'}[args['mode']]
        return f"{seat.get(args['seat'],args['seat'])}座椅按摩已关闭。" if args['mode'] == 'off' else f"{seat.get(args['seat'],args['seat'])}座椅按摩设为{mode}模式。"
    if name == 'set_seat_position':
        axis = {'fore_aft': '前后位置', 'height': '高度', 'backrest': '靠背角度', 'lumbar': '腰托位置'}[args['axis']]
        value = spoken_number(result['value'])
        target = f'{value}度' if args['axis'] == 'backrest' else f'百分之{value}'
        return f"{seat.get(args['seat'], args['seat'])}座椅{axis}设为{target}。"
    if name == 'set_interior_light': return f"{zone.get(args.get('zone'), '')}车内灯已{enabled}。"
    if name == 'set_volume': return f"音量调到百分之{args['volume']}了。"
    if name == 'control_playback': return {'play':'已开始播放。','pause':'已暂停播放。','next':'已切到下一首。','previous':'已切到上一首。','stop':'已停止播放。'}.get(args.get('action'), '播放状态已更新。')
    if name == 'set_navigation_destination': return f"模拟导航目的地已设为{args['destination']}。"
    if name == 'set_mirror_heating': return f'后视镜加热已{enabled}。'
    if name == 'set_mirror_fold': return f"左右后视镜已{'折叠' if result['value'] else '展开'}。"
    if name == 'set_steering_heating': return f'方向盘加热已{enabled}。'
    if name == 'set_reading_light': return f"{seat.get(args['seat'], args['seat'])}阅读灯已{enabled}。"
    if name == 'set_ambient_light': return f'氛围灯已{enabled}。'
    if name == 'set_ambient_color': return '氛围灯颜色设为' + {'white':'白色','warm':'暖色','red':'红色','blue':'蓝色','green':'绿色','purple':'紫色'}[args['color']] + '。'
    if name == 'set_ambient_brightness': return f"氛围灯亮度设为百分之{args['brightness']}。"
    if name == 'set_child_lock': return f"{'左' if args['side'] == 'left' else '右'}后门儿童安全锁已{enabled}。"
    if name == 'set_door_lock': return f"{'全部' if args['door'] == 'all' else seat.get(args['door'],args['door'])}车门已{'锁定' if args['locked'] else '解锁'}。"
    if name in {'lock_all_doors', 'unlock_all_doors'}: return f"全部车门已{'锁定' if name == 'lock_all_doors' else '解锁'}。"
    if name == 'set_tailgate_open': return f"尾门已{'开启' if args['open'] else '关闭'}。"
    if name == 'set_tailgate_height': return f"尾门下次开启高度设为百分之{args['height']}。"
    if name == 'set_wiper_mode': return '前雨刷已关闭。' if args['mode'] == 'off' else '前雨刷设为' + {'auto':'自动','intermittent':'间歇','low':'低速','high':'高速'}[args['mode']] + '模式。'
    if name == 'set_wiper_interval': return f"间歇雨刷间隔设为{args['seconds']}秒。"
    if name == 'set_rain_sensitivity': return f"自动雨刷雨量灵敏度设为{spoken_number(result['value'])}档。"
    if name == 'set_rear_wiper': return f'后雨刷已{enabled}。'
    if name == 'wash_windshield': return f"{'前' if args['target'] == 'front' else '后'}挡风玻璃已清洗。"
    if name in {'set_sunroof_position', 'set_sunshade_position'}: return f"{'天窗' if name == 'set_sunroof_position' else '遮阳帘'}开度设为百分之{args['position']}。"
    description = TOOLS[name].description.replace('（模拟车控）', '')
    if name.startswith('get_'): return description + '已完成，可在车况面板核对。'
    return description + '已完成，可在车况面板核对。'


def spoken_reason(reason):
    """Keep physical refusal conditions; move protocol/schema detail to trace."""
    text = str(reason)
    if text.startswith('参数校验失败'):
        return '设置参数没确认清楚，这次没有调整。请把要求说具体一点。'
    if any(marker in text for marker in ('load_skill', 'skill_id', '未知工具', '未知函数')):
        return '这项功能暂时没能执行，请换个说法再试。'
    return text


def validation_feedback(exc, name):
    if isinstance(exc, ValidationError):
        field_name = next(iter(exc.path), None)
        spec = TOOLS[name].parameters.get('properties', {}).get(field_name, {}) if name in TOOLS else {}
        if exc.validator in {'minimum', 'maximum'} and 'minimum' in spec and 'maximum' in spec:
            label = {'volume': '音量', 'temperature': '温度', 'level': '档位', 'position': '车窗开度'}.get(field_name, '这个数值')
            unit = '度' if field_name == 'temperature' else ''
            return f'{label}只能调到{spoken_number(spec["minimum"])}到{spoken_number(spec["maximum"])}{unit}，这次没有调整。'
        return '这次的执行指令格式出了问题，没有调整。可以再试一次。'
    return spoken_reason(f'参数校验失败：{str(exc).splitlines()[0][:180]}')


@dataclass
class Turn:
    id: str
    text: str
    persona: PersonaConfig
    persona_version: int
    started: float = field(default_factory=time.perf_counter)
    messages: list = field(default_factory=list)
    output: str = ''
    first_effective_ms: float | None = None
    model_calls: int = 0
    loaded: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    pending_results: dict = field(default_factory=dict)
    group: list = field(default_factory=list)
    group_results: dict = field(default_factory=dict)
    tool_results: dict = field(default_factory=dict)
    has_reads: bool = False
    needs_model: bool = False
    feedback: list = field(default_factory=list)
    finished: bool = False
    model_task: object = None
    route: str = 'chat'
    audio_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    tool_model_calls: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    proactive: bool = False
    task_id: str | None = None
    interrupted: bool = False
    client_request_id: str | None = None
    playback_id: str | None = None
    audio_queue: object = None
    audio_task: object = None
    audio_chunks: int = 0
    audio_bytes: int = 0
    audio_input_closed: bool = False
    model_text_pending: str = ''
    spoken_parts: list = field(default_factory=list)
    sentences: list = field(default_factory=list)
    sentence_start_ms: float = 0
    played_ms: float = 0
    played_text: str = ''
    dispatch_announced: bool = False
    chat_streaming: bool = False
    speech_routing: bool = False
    sentence_buffer: str = ''
    vehicle_plans: dict = field(default_factory=dict)
    vehicle_plan_tasks: dict = field(default_factory=dict)
    lookup_tasks: dict = field(default_factory=dict)
    lookup_results: list = field(default_factory=list)
    poi_commands: dict = field(default_factory=dict)


class SessionActor:
    def __init__(self, session_id, db, ark, tts=None, vehicle_planner=None, maps=None):
        self.id, self.db, self.ark = session_id, db, ark
        self.tts = tts
        self.vehicle_planner = vehicle_planner or VehicleControlPlanner(ark)
        self.vehicle_planning_timeout = max(TOOL_TIMEOUT, 45)
        self.maps = maps or GoogleMapsClient()
        self.owns_maps = maps is None
        self.lookup_timeout = 25
        self.places = {}  # Session-local identities from successful, adopted search results.
        self.recent_places = []
        self.audio_clients = set()
        self.audio_enabled = False
        self.queue = asyncio.Queue()
        self.clients = set()
        self.persona = PERSONAS['warm'].model_copy(deep=True)
        self.persona_version = 1
        self.vehicle = initial_vehicle_state()
        self.simulator = VehicleSimulator(self.vehicle)
        self.vehicle_version = 0
        self.turns = []
        self.loaded_tools = []
        self.current = None
        self.generating = False
        self.speaking = False
        self.asr_capture_id = None
        self.pending_commands = {}
        self.completed_commands = {}
        self.event_id = 0
        self.announced_keys = set()
        self.announcements = {}  # task id -> newest revision terminal/clarification snapshot
        # Read-side mirror of this session's tasks, fed by the event poller and
        # by this actor's own writes, so handle() never blocks on SQLite.
        self.tasks = {}
        self.background = set()
        self.trace_queue = asyncio.Queue()
        self.trace_error = None
        self.trace_writer = asyncio.create_task(self.write_traces())
        self.runner = asyncio.create_task(self.run())
        self.poller = asyncio.create_task(self.poll_events())

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def trace(self, kind, data, turn=None, *, call_id=None, status=None, task_id=None):
        # Trace writes have a separate ordered queue; SQLite never blocks the
        # actor's model deltas, command dispatch or receipt handling.
        self.trace_queue.put_nowait((kind, copy.deepcopy({'agent': 'talker', 'observed_at': utcnow(), **data}),
                                    {'turn_id': turn.id if turn else None,
                                     'task_id': task_id or (turn.task_id if turn else None),
                                     'call_id': call_id, 'status': status}))

    async def write_traces(self):
        while True:
            kind, data, fields = await self.trace_queue.get()
            try:
                await asyncio.to_thread(self.db.append_trace, self.id, kind, data, **fields)
            except Exception as exc:
                self.trace_error = sanitized_error(exc)
                logging.getLogger(__name__).error('Trace recording failed for session %s', self.id)
            finally:
                self.trace_queue.task_done()

    def emit(self, type_, **payload):
        message = {'type': type_, **payload}
        for client in list(self.clients):
            if client.qsize() > 2000:
                self.clients.discard(client)
            else:
                client.put_nowait(message)

    def emit_audio(self, message):
        # Audio never shares the control/vehicle queue. Slow consumers are
        # disconnected instead of growing an unbounded PCM backlog.
        for client in list(self.audio_clients):
            if client.qsize() >= 256:
                self.audio_clients.discard(client)
                while not client.empty(): client.get_nowait()
                client.put_nowait({'type': 'audio.error', 'message': '音频接收过慢，请重新连接'})
                client.put_nowait(None)
            else:
                client.put_nowait(message)

    async def attach_audio(self):
        outgoing, future = asyncio.Queue(), asyncio.get_running_loop().create_future()
        await self.post({'type': '_audio_attach', 'outgoing': outgoing, 'future': future})
        await future
        return outgoing

    def audio_valid(self, turn):
        return self.current is turn and not turn.audio_cancelled.is_set()

    def cancel_audio(self, turn, reason):
        if turn.audio_cancelled.is_set():
            return
        turn.audio_cancelled.set()
        if turn.audio_task and not turn.audio_task.done():
            turn.audio_task.cancel()
        if turn.playback_id:
            self.emit_audio({'type': 'audio.cancelled', 'turn_id': turn.id,
                             'playback_id': turn.playback_id, 'reason': reason})
            self.trace('audio.cancelled', {'playback_id': turn.playback_id, 'reason': reason}, turn,
                       status='cancelled')

    def start_audio(self, turn):
        if self.asr_capture_id or not (self.audio_enabled and self.audio_clients and self.tts):
            return
        turn.playback_id, turn.audio_queue = uuid4().hex, asyncio.Queue()
        self.speaking = True  # Includes synthesis and queued PCM, until browser drain.
        identity = {'session_id': self.id, 'turn_id': turn.id, 'playback_id': turn.playback_id,
                    'client_request_id': turn.client_request_id}
        self.emit_audio({'type': 'audio.started', **identity, 'sample_rate': 24000,
                         'channels': 1, 'format': 'pcm_s16le'})
        self.trace('audio.started', identity, turn, status='running')

        async def texts():
            while True:
                text = await turn.audio_queue.get()
                if text is None: return
                yield text

        async def synthesize():
            try:
                async for item in self.tts.stream_synthesize(texts()):
                    await self.post({'type': '_audio_item', 'turn': turn, 'item': item})
                await self.post({'type': '_audio_finished', 'turn': turn})
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                await self.post({'type': '_audio_error', 'turn': turn, 'error': sanitized_error(exc)})
        turn.audio_task = self.spawn(synthesize())

    def feed_audio(self, turn, text, source='persona_template', original_text=None):
        clean = speech_text(text)
        if clean and turn.audio_queue is not None and self.audio_valid(turn) and not turn.audio_input_closed:
            if 'server_first_text_ms' not in turn.metrics:
                turn.metrics['server_first_text_ms'] = (time.perf_counter() - turn.started) * 1000
            turn.spoken_parts.append(clean)
            self.trace('audio.text', {'playback_id': turn.playback_id, 'source': source, 'text': clean,
                       'original_chars': len(original_text if original_text is not None else text),
                       'selected_chars': len(text), 'spoken_chars': len(clean),
                       'normalization_applied': clean != text}, turn, status='submitted')
            turn.audio_queue.put_nowait(clean)

    def feed_sentences(self, turn, final=False):
        end = 0
        for index, character in enumerate(turn.sentence_buffer):
            if character in '。！？!?\n':
                self.feed_audio(turn, turn.sentence_buffer[end:index + 1], 'model_chat_stream')
                end = index + 1
        turn.sentence_buffer = turn.sentence_buffer[end:]
        if final and turn.sentence_buffer:
            self.feed_audio(turn, turn.sentence_buffer, 'model_chat_stream')
            turn.sentence_buffer = ''

    def close_audio_input(self, turn):
        if turn.audio_queue is not None and not turn.audio_input_closed:
            turn.audio_input_closed = True
            turn.audio_queue.put_nowait(None)

    def history(self):
        messages = []
        for turn in self.turns[-12:]:
            messages.extend(copy.deepcopy(turn.messages))
        return messages

    def snapshot(self):
        result = SessionSnapshot(session_id=self.id, persona=self.persona, persona_version=self.persona_version,
                                 vehicle=self.vehicle, tasks=self.task_list(), history=self.history()).model_dump()
        result['turns'] = [{'turn_id': turn.id, 'text': turn.text, 'output': turn.output,
                            'finished': turn.finished, 'proactive': turn.proactive, 'task_id': turn.task_id,
                            'state': 'interrupted' if turn.interrupted else 'done' if turn.finished else 'streaming',
                            'persona_version': turn.persona_version, 'metrics': turn.metrics,
                            'lookup_results': copy.deepcopy(turn.lookup_results),
                            'vehicle_plans': [{'parent_call_id': parent_id, 'plan': copy.deepcopy(execution['plan']),
                                               'ledger': copy.deepcopy(turn.tool_results.get(parent_id)),
                                               'action_receipts': copy.deepcopy(execution['action_receipts'])}
                                              for parent_id, execution in turn.vehicle_plans.items()],
                            'vehicle_calls': [call for message in turn.messages for call in message.get('tool_calls', [])
                                              if call.get('function', {}).get('name') in TOOLS]} for turn in self.turns]
        return result

    async def post(self, event):
        await self.queue.put(event)

    async def attach(self):
        outgoing, future = asyncio.Queue(), asyncio.get_running_loop().create_future()
        await self.post({'type': '_attach', 'outgoing': outgoing, 'future': future})
        await future
        return outgoing

    async def poll_events(self):
        tasks = await asyncio.to_thread(self.db.list_tasks, self.id)
        await self.post({'type': '_tasks_loaded', 'tasks': [task_card(task) for task in tasks]})
        cursor = 0
        while True:
            for event in await asyncio.to_thread(self.db.events_after, cursor, self.id):
                cursor = event['event_id']
                if not isinstance(event['payload'].get('task'), dict):
                    event['payload']['task'] = task_card(await asyncio.to_thread(self.db.get_task, event['task_id']))
                await self.post({'type': '_task_event', 'event': event})
            await asyncio.sleep(.15)

    def task_list(self):
        return sorted(self.tasks.values(), key=lambda task: (task['created_at'], task['id']))

    def remember_task(self, task):
        """Keep the newest revision. A terminal status is final for its revision,
        mirroring db.save_task, so a replayed event cannot revive a task."""
        known = self.tasks.get(task['id']) if task else None
        if not task or (known and (task['revision'] < known['revision'] or (
                task['revision'] == known['revision'] and known['status'] in TERMINAL_TASK
                and task['status'] != known['status']))):
            return
        self.tasks[task['id']] = task_card(task)

    async def run(self):
        while True:
            event = await self.queue.get()
            try:
                self.handle(event)
            except Exception as exc:
                self.emit('error', code='session_error', message=str(exc)[:300])
                future = event.get('future')
                if future and not future.done():
                    future.set_exception(exc)
            finally:
                self.queue.task_done()

    def handle(self, e):
        kind = e['type']
        if kind == '_attach':
            self.clients.add(e['outgoing'])
            e['outgoing'].put_nowait({'type': 'session.snapshot', **self.snapshot()})
            for command in self.pending_commands.values():
                e['outgoing'].put_nowait({'type': 'vehicle.command', **command['wire']})
            e['future'].set_result(True)
            self.schedule_announce()
        elif kind == '_detach':
            self.clients.discard(e['outgoing'])
            if not self.clients:
                self.asr_capture_id = None
                self.speaking = False
                if self.current: self.cancel_audio(self.current, 'control_disconnected')
        elif kind == '_audio_attach':
            self.audio_clients.add(e['outgoing'])
            e['future'].set_result(True)
        elif kind == '_audio_detach':
            self.audio_clients.discard(e['outgoing'])
            if not self.audio_clients:
                if self.current: self.cancel_audio(self.current, 'audio_disconnected')
                self.speaking = False
                self.schedule_announce()
        elif kind == 'input':
            text = str(e.get('text', '')).strip()
            if not text or len(text) > 4000:
                raise ValueError('输入需为1–4000字')
            self.asr_capture_id = None
            self.start_turn(text, str(e.get('client_request_id', ''))[:80] or None)
        elif kind == 'asr.capture':
            capture_id = e.get('capture_id')
            if not is_identifier(capture_id, max_length=80):
                raise ValueError('无效语音录音标识')
            if e.get('active') is True:
                if self.asr_capture_id == capture_id:
                    return
                self.asr_capture_id = capture_id
                if self.current:
                    self.cancel_audio(self.current, 'asr_capture')
                self.speaking = False
                async def expire_capture():
                    await asyncio.sleep(90)
                    await self.post({'type': 'asr.capture', 'capture_id': capture_id, 'active': False})
                self.spawn(expire_capture())
            elif self.asr_capture_id == capture_id:
                self.asr_capture_id = None
                self.schedule_announce()
        elif kind == 'persona.set':
            if e.get('preset'):
                if e['preset'] not in PERSONAS:
                    raise ValueError('未知人设')
                self.persona = PERSONAS[e['preset']].model_copy(deep=True)
            else:
                self.persona = PersonaConfig.model_validate(e.get('config'))
            self.persona_version += 1
            self.emit('persona.changed', persona=self.persona.model_dump(), persona_version=self.persona_version)
        elif kind == 'audio.played':
            target = next((turn for turn in self.turns if turn.id == e.get('turn_id')
                           and turn.playback_id == e.get('playback_id')), None)
            elapsed = e.get('played_ms')
            if target and type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                target.played_ms = max(target.played_ms, min(elapsed, target.audio_bytes / 48))
                target.played_text = ''.join(item['text'] for item in target.sentences
                                            if item['end_ms'] <= target.played_ms)
                self.trace('audio.played', {'playback_id': target.playback_id,
                    'played_ms': target.played_ms, 'played_text': target.played_text,
                    'precision': 'completed_sentences'}, target)
        elif kind == 'audio.cancel':
            target = next((turn for turn in reversed(self.turns) if turn.id == e.get('turn_id')), None)
            if target and (not e.get('playback_id') or e['playback_id'] == target.playback_id):
                self.cancel_audio(target, 'client_cancelled')
                if self.current is target:
                    self.speaking = False
                    self.schedule_announce()
        elif kind == 'audio.configure':
            self.audio_enabled = e.get('enabled') is True
            if self.audio_enabled and self.tts and hasattr(self.tts, 'prewarm_stream'):
                async def warm():
                    try:
                        status = await self.tts.prewarm_stream()
                    except Exception as exc:
                        status = {'ok': False, 'error': sanitized_error(exc)}
                    await self.post({'type': '_audio_connection', 'status': status})
                self.spawn(warm())
            elif not self.audio_enabled:
                if self.current: self.cancel_audio(self.current, 'audio_disabled')
                self.speaking = False
                self.schedule_announce()
        elif kind == '_audio_connection':
            self.emit('audio.connection', **e['status'])
        elif kind == 'audio.client_metrics':
            target = next((t for t in reversed(self.turns) if t.id == e.get('turn_id') and
                           t.playback_id == e.get('playback_id')), None)
            if target and isinstance(e.get('metrics'), dict):
                metrics = {str(k)[:80]: v for k, v in list(e['metrics'].items())[:40]
                           if isinstance(v, (int, float, str, bool)) or v is None}
                target.metrics['audio_client'] = metrics
                self.trace('audio.client_metrics', {'playback_id': target.playback_id,
                           'client_request_id': target.client_request_id, 'metrics': metrics}, target,
                           status='completed' if self.audio_valid(target) else 'stale')
        elif kind == 'playback':
            if self.current and self.current.playback_id and (e.get('turn_id') != self.current.id or
                                                            e.get('playback_id') != self.current.playback_id):
                return
            if e.get('speaking') and self.current and self.current.audio_cancelled.is_set():
                return
            self.speaking = bool(e.get('speaking'))
            if not self.speaking:
                self.schedule_announce()
        elif kind == '_announce':
            self.announce()
        elif kind == '_delta':
            turn = e['turn']
            if self.is_current(turn):
                turn.metrics.setdefault('server_first_model_text_ms', (time.perf_counter() - turn.started) * 1000)
                # SSE content can precede tool_calls in the same response. It
                # is displayed immediately, but speech eligibility waits for
                # the completed routing decision to avoid premature success.
                turn.model_text_pending += e['text']
                if not turn.speech_routing:
                    self.append_text(turn, e['text'], speak=False)
                if turn.chat_streaming:
                    turn.sentence_buffer += e['text']
                    self.feed_sentences(turn)
        elif kind == '_audio_item':
            turn, item = e['turn'], e['item']
            if not self.audio_valid(turn): return
            audio = item.get('audio')
            if isinstance(audio, bytes) and audio:
                if len(audio) % 2:
                    self.handle({'type': '_audio_error', 'turn': turn, 'error': '无效PCM音频帧'})
                    return
                elapsed = (time.perf_counter() - turn.started) * 1000
                if not turn.audio_chunks: turn.metrics['server_first_audio_ms'] = elapsed
                if any(audio) and 'server_first_nonzero_audio_ms' not in turn.metrics:
                    turn.metrics['server_first_nonzero_audio_ms'] = elapsed
                turn.audio_chunks += 1
                turn.audio_bytes += len(audio)
                self.emit_audio(turn.playback_id.encode('ascii') + audio)
                if not self.audio_clients:
                    self.cancel_audio(turn, 'audio_backpressure')
                    self.speaking = False
                    self.schedule_announce()
            else:
                if item.get('event') == 'sentence.started':
                    turn.sentence_start_ms = turn.audio_bytes / 48
                elif item.get('event') == 'sentence.finished':
                    data = item.get('data') or {}
                    if isinstance(data.get('text'), str) and data['text']:
                        turn.sentences.append({'text': data['text'], 'start_ms': turn.sentence_start_ms,
                                               'end_ms': turn.audio_bytes / 48})
                diagnostics = {k: v for k, v in item.items() if k != 'audio'}
                self.emit_audio({'type': 'audio.metrics', 'turn_id': turn.id,
                                 'playback_id': turn.playback_id, 'client_request_id': turn.client_request_id,
                                 'metrics': diagnostics})
        elif kind == '_audio_finished':
            turn = e['turn']
            if not self.audio_valid(turn): return
            if not turn.audio_chunks:
                self.handle({'type': '_audio_error', 'turn': turn, 'error': 'Speech 未返回有效音频帧'})
                return
            metrics = {k: v for k, v in turn.metrics.items() if k.startswith('server_')}
            metrics.update({'audio_chunks': turn.audio_chunks, 'audio_bytes': turn.audio_bytes,
                            'speech_elapsed_ms': (time.perf_counter() - turn.started) * 1000})
            self.trace('audio.completed', {'playback_id': turn.playback_id, 'metrics': metrics}, turn,
                       status='completed')
            self.emit_audio({'type': 'audio.metrics', 'turn_id': turn.id,
                             'playback_id': turn.playback_id, 'metrics': metrics})
            self.emit_audio({'type': 'audio.ended', 'turn_id': turn.id, 'playback_id': turn.playback_id})
        elif kind == '_audio_error':
            turn = e['turn']
            if not self.audio_valid(turn): return
            self.trace('audio.error', {'playback_id': turn.playback_id, 'error': e['error']}, turn, status='failed')
            self.emit_audio({'type': 'audio.error', 'turn_id': turn.id, 'playback_id': turn.playback_id,
                             'message': e['error']})
            turn.audio_cancelled.set()
            if turn.audio_task and not turn.audio_task.done(): turn.audio_task.cancel()
            self.speaking = False
            self.schedule_announce()
        elif kind == '_model_done':
            turn = e['turn']
            if self.is_current(turn):
                turn.metrics['server_model_completed_ms'] = (time.perf_counter() - turn.started) * 1000
            self.trace('model.completed', {'model_call_index': e.get('model_call_index'),
                       'message': e['message'], 'meta': e.get('meta', {}),
                       'observed_at': e.get('observed_at', utcnow())}, turn,
                       call_id=e.get('call_id'), status='completed' if self.is_current(turn) else 'stale')
            self.model_done(e['turn'], e['message'], e.get('call_id'))
        elif kind == '_model_error':
            turn = e['turn']
            self.trace('model.error', {'model_call_index': e.get('model_call_index'), 'error': e['error'],
                       'partial': e.get('partial'), 'observed_at': e.get('observed_at', utcnow())},
                       turn, call_id=e.get('call_id'), status='failed' if self.is_current(turn) else 'stale')
            if self.is_current(turn):
                verified = self.receipt_feedback(turn, mutations_only=True)
                if verified:
                    turn.output = ''
                    self.append_text(turn, verified, speak=not turn.dispatch_announced, source='verified_partial_receipts')
                self.append_text(turn, render(turn.persona, 'error', '后续处理连接出了问题，还有请求未确认。请稍后再试。' if verified else '这次连接出了问题，暂时没能回答。请稍后再试。'),
                                 source='error_template', original_text=e['error'])
                self.finish(turn, error=e['error'])
        elif kind == '_tool_result':
            self.tool_result(e['turn'], e['call'], e['result'])
        elif kind == '_lookup_result':
            self.accept_lookup(e['turn'], e['call'], e['result'])
        elif kind == '_vehicle_plan_done':
            self.accept_vehicle_plan(e['turn'], e['call'], e['plan'])
        elif kind == '_vehicle_plan_error':
            turn, call = e['turn'], e['call']
            self.trace('vehicle.plan.failed', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                       'parent_call_id': call['id'], 'error': e['error']}, turn,
                       call_id=call['id'], status='failed' if self.is_current(turn) else 'stale')
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                'code': 'vehicle_planning_failed', 'reason': e['error'],
                'spoken_reason': '这次车控计划未能核实，没有下发其中的动作。请稍后重试。'})
        elif kind == 'vehicle.configure':
            self.vehicle = self.simulator.configure(e.get('settings', {}))
            self.vehicle_version = self.simulator.version
            self.emit('vehicle.state', state=self.vehicle)
            self.trace('vehicle.configured', {'settings': e.get('settings', {})})
        elif kind == 'vehicle.receipt':
            receipt = self.simulator.receipt(e.get('command_id'), e.get('receipt_id'))
            self.execute_command(e.get('command_id'), receipt)
        elif kind == 'vehicle.execute':
            # Legacy in-process fixtures; the public WebSocket rejects this ACK.
            self.execute_command(e.get('command_id', ''))
        elif kind == '_command_timeout':
            self.command_timeout(e['command_id'])
        elif kind == '_task_event':
            self.task_event(e['event'])
        elif kind == '_tasks_loaded':
            for task in e['tasks']:
                self.remember_task(task)
        elif kind == '_task_write':
            self.task_write_done(e)
        elif kind.startswith('task.'):
            self.explicit_task(kind[5:], e)
        else:
            raise ValueError(f'未知消息类型：{kind}')

    def is_current(self, turn):
        return self.current is turn and not turn.finished

    def start_turn(self, text, client_request_id=None):
        if self.current:
            old = self.current
            self.simulator.invalidate(old.id)
            self.cancel_audio(old, 'new_input')
            if old.model_task and not old.model_task.done():
                old.model_task.cancel()
            for planning in old.vehicle_plan_tasks.values():
                if not planning.done():
                    planning.cancel()
            for lookup in old.lookup_tasks.values():
                if not lookup.done():
                    lookup.cancel()
            for plan in old.vehicle_plans.values():
                # Already-dispatched commands retain their real late receipts;
                # no remaining action may be dispatched for an interrupted turn.
                plan['interrupted'] = True
            if not old.finished:
                old.finished = True
                old.interrupted = True
                self.trace('turn.interrupted', {'output': old.output, 'reason': 'new_input'}, old, status='interrupted')
                self.emit('stream.interrupted', turn_id=old.id)
            # Interrupted partial text belongs to that turn; pending tool records stay intact.
            if old.output and not old.group and not any(m.get('role') == 'assistant' for m in old.messages):
                old.messages.append({'role': 'assistant', 'content': old.output + ' [用户打断]'})
        self.speaking = False
        turn = Turn(uuid4().hex[:12], text, self.persona.model_copy(deep=True), self.persona_version)
        turn.loaded = list(self.loaded_tools)
        turn.client_request_id = client_request_id
        turn.messages.append({'role': 'user', 'content': text})
        self.turns.append(turn)
        self.current, self.generating = turn, True
        self.trace('turn.started', {'input': text, 'persona_version': turn.persona_version}, turn, status='running')
        self.emit('turn.started', turn_id=turn.id, text=text, persona_version=turn.persona_version,
                  client_request_id=client_request_id)
        self.start_audio(turn)
        self.launch_model(turn)

    def messages_for(self, turn):
        # Completed results remain attributable alongside unfinished tasks.
        # Reuse the prompt-copy helper; full reports/raw evidence stay in DB.
        tasks = compact_tool_result({'tasks': self.task_list()[-8:]}, 'manage_task')['tasks']
        # Actual read-only state is data, not a cold tool schema. Cold schemas
        # belong exclusively to the dedicated vehicle planner.
        vehicle = {skill_id: {key: copy.deepcopy(value) for key, value in values.items() if key != 'memories'}
                   for skill_id, values in self.vehicle.items()}
        context = {'vehicle': vehicle, 'background_tasks_reference_only': tasks,
                   'recent_places': self.recent_places,
                   'current_utc': utcnow(), 'timezone': self.vehicle['_meta']['timezone'],
                   'capabilities': {'gps': False, 'browser_geolocation': False, 'task_priority_change': False}}
        messages = [{'role': 'system', 'content': CORE_PROMPT},
                    {'role': 'system', 'content': style_prompt(turn.persona)}]
        for previous in [*self.turns[-4:], *([] if turn in self.turns[-4:] else [turn])]:
            if previous is turn:
                messages.append({'role': 'system', 'content': render_prompt('session_snapshot',
                    snapshot_json=json.dumps(context, ensure_ascii=False, separators=(',', ':')))})
                messages.append({'role': 'system', 'content': CURRENT_TURN_PROMPT})
            # Keep protocol-valid complete tool groups even when newer user input arrives before a receipt.
            pending = bool(previous.group and len(previous.group_results) < len(previous.group))
            segment = copy.deepcopy(previous.messages)
            if previous.playback_id and previous.audio_cancelled.is_set():
                # Display/trace keeps the full generated text; model history
                # makes no claim that unrendered speech reached the listener.
                for record in segment:
                    if record.get('role') == 'assistant' and not record.get('tool_calls'):
                        record['content'] = (previous.played_text or '（未确认有完整句子播放）') + ' [朗读已打断]'

            if pending:
                cutoff = next((i for i, m in reversed(list(enumerate(segment))) if m.get('tool_calls')), len(segment))
                segment = segment[:cutoff]
            call_context = {}
            for record in segment:
                for call in record.get('tool_calls', []):
                    fn = call.get('function', {})
                    try:
                        args = json.loads(fn.get('arguments', '{}'))
                    except (TypeError, ValueError):
                        args = {}
                    call_context[call['id']] = (fn.get('name'), isinstance(args, dict) and args.get('read_details') is True)
            compact = [compact_message(record, *call_context.get(record.get('tool_call_id'), (None, False)))
                       for record in segment]
            messages.extend(compact)
        return messages

    def tools_for(self, turn):
        # This bounded interface never grows after delegation or prior turns.
        return [TOOLS[name].openai_tool() for name in HOT_TOOL_NAMES] + CONTROL_TOOLS + MAPS_TOOLS

    def launch_model(self, turn):
        if turn.model_calls >= 5:
            verified = self.receipt_feedback(turn, mutations_only=True)
            if verified:
                self.append_text(turn, verified, speak=not turn.dispatch_announced, source='verified_partial_receipts')
            self.append_text(turn, '这次没能确认可用的功能，请把需求再具体一点。')
            self.finish(turn, error='model_step_limit')
            return
        turn.speech_routing = turn.model_calls == 0 and turn.audio_queue is not None
        turn.model_calls += 1
        messages, tools = self.messages_for(turn), self.tools_for(turn)
        if turn.chat_streaming:
            tools = []
            messages[0] = {'role': 'system', 'content': CHAT_SPEECH_PROMPT}
        elif turn.speech_routing:
            messages[0]['content'] += '\n' + SPEECH_ROUTE_PROMPT
            # Decisions share one fixed policy; persona is applied only to prose.
            messages.pop(1)
        model_call_index, call_id = turn.model_calls, uuid4().hex
        self.trace('model.started', {'model_call_index': model_call_index, 'messages': messages,
                                    'tools': tools}, turn, call_id=call_id, status='running')
        async def generate():
            partial = ''
            try:
                async for item in self.ark.stream(messages, tools):
                    if item['type'] == 'delta':
                        partial += item['text']
                        await self.post({'type': '_delta', 'turn': turn, 'text': item['text']})
                    elif item['type'] == 'message':
                        await self.post({'type': '_model_done', 'turn': turn, 'message': item['message'],
                                         'call_id': call_id, 'model_call_index': model_call_index,
                                         'meta': item.get('meta', {}), 'observed_at': utcnow()})
            except asyncio.CancelledError as exc:
                self.trace('model.cancelled', {'model_call_index': model_call_index,
                           'partial': getattr(exc, 'trace_partial', {'message': {'content': partial}, 'meta': {}})},
                           turn, call_id=call_id, status='cancelled')
            except Exception as exc:
                error = sanitized_error(exc)
                await self.post({'type': '_model_error', 'turn': turn, 'error': error,
                                'partial': getattr(exc, 'trace_partial', {'message': {'content': partial}, 'meta': {}}),
                                'call_id': call_id, 'model_call_index': model_call_index, 'observed_at': utcnow()})
        turn.model_task = self.spawn(generate())

    def append_text(self, turn, text, speak=True, source='persona_template', original_text=None):
        selected_text = text
        if original_text is None: original_text = text
        if speak: text = speech_text(text)
        turn.output += text
        if turn.first_effective_ms is None and effective_text(turn.output):
            turn.first_effective_ms = (time.perf_counter() - turn.started) * 1000
        self.emit('stream.delta', turn_id=turn.id, text=text)
        if speak: self.feed_audio(turn, selected_text, source, original_text)

    def model_done(self, turn, message, model_call_id=None):
        if not self.is_current(turn):
            return
        calls = message.get('tool_calls', [])
        if turn.chat_streaming:
            if calls:
                self.cancel_audio(turn, 'chat_protocol_violation')
                self.finish(turn, error='chat_protocol_violation')
                return
            self.feed_sentences(turn, final=True)
            turn.messages.append(message)
            self.finish(turn)
            return
        if turn.speech_routing and not calls:
            try:
                decision = json.loads(message.get('content') or '')
            except (ValueError, TypeError):
                decision = None
            if decision == {'response_mode': 'chat'}:
                turn.chat_streaming = True
                turn.model_text_pending = turn.output = ''
                self.trace('speech.route_confirmed', {'mode': 'chat', 'tools_disabled': True}, turn)
                self.launch_model(turn)
                return
        turn.messages.append(message)
        if not calls:
            original = turn.model_text_pending or message.get('content') or ''
            lookup_names = {item['function']['name'] for record in turn.messages
                            for item in record.get('tool_calls', [])}
            if lookup_names == {'search_places'} and len(turn.lookup_results) == 1:
                places_result = turn.lookup_results[0]['result']
                places = places_result.get('places', [])
                if places_result.get('ok') and places:
                    # Ordinals must mean the same thing in speech, the result
                    # card and recent_places. The raw model text stays in trace.
                    ordered = '、'.join(f'{i}，{p["name"]}' for i, p in enumerate(places, 1))
                    text = f'找到{len(places)}个地点，按结果卡顺序是：{ordered}。可以告诉我名称或序号。'
                    turn.output = turn.model_text_pending = ''
                    self.append_text(turn, text, source='verified_place_results')
                    turn.metrics['output_source'] = 'verified_place_results'
                    message['content'] = text
                    self.finish(turn)
                    return
            handled = self.handled_vehicle_settings(turn)
            verified = self.receipt_feedback(turn, mutations_only=True) if turn.has_reads or handled or turn.vehicle_plans else ''
            if verified:
                turn.output = ''
                self.append_text(turn, verified, speak=not turn.dispatch_announced, source='verified_partial_receipts')
                if turn.dispatch_announced:
                    self.speak_vehicle_failures(turn)
                if turn.has_reads:
                    self.append_text(turn, original, source='model_content', original_text=original)
            else:
                turn.output = speech_text(original)
                self.feed_audio(turn, original, 'model_content', original)
            turn.model_text_pending = ''
            self.finish(turn, error=self.vehicle_plan_error(turn))
            return
        turn.model_text_pending = ''
        turn.output = ''  # Tool-call prefaces are retained in raw model trace, not final feedback.
        turn.group, turn.group_results, turn.needs_model, turn.feedback = calls, {}, False, []
        turn.route = 'tool'
        names = [call.get('function', {}).get('name') for call in calls]
        delegation_count = names.count('delegate_vehicle_control')
        group_error = None
        if delegation_count and (delegation_count > 1 or any(name in TOOLS or name == 'start_poi_navigation' for name in names)):
            group_error = {'code': 'planned_vehicle_group_conflict',
                           'reason': '整句车控只能由一层执行；委托车控不能与任何直接车控或另一委托混在同组。请只委托一次完整原句。'}
        elif any(name in TOOLS and name not in HOT_TOOL_NAMES or name == 'load_skill' for name in names):
            group_error = {'code': 'cold_vehicle_requires_delegation',
                           'reason': '非热车控只由专门车控层处理；本组没有执行。请整句调用delegate_vehicle_control，不先执行热动作、不调用load_skill。'}
        elif 'start_poi_navigation' in names and ('set_navigation_destination' in names or names.count('start_poi_navigation') > 1):
            group_error = {'code': 'navigation_group_conflict', 'reason': '一次只设置一个导航目的地，不能混用POI导航和文字目的地。'}
        if group_error:
            # Validate the routing choice for the entire group before even its
            # first hot command can be dispatched. No cross-layer replay.
            turn.needs_model = True
            for call in calls:
                turn.tool_model_calls[call['id']] = model_call_id
                self.trace('tool.started', {'name': call.get('function', {}).get('name'),
                           'arguments': call.get('function', {}).get('arguments'), 'model_call_id': model_call_id},
                           turn, call_id=call['id'], status='running')
                self.tool_result(turn, call, {'ok': False, 'executed': False,
                    'retry_model': True, **group_error})
            return
        for call in calls:
            if call.get('function', {}).get('name') in TOOLS:
                self.emit('vehicle.function_call', turn_id=turn.id, call=call)
            turn.tool_model_calls[call['id']] = model_call_id
            self.trace('tool.started', {'name': call.get('function', {}).get('name'),
                       'arguments': call.get('function', {}).get('arguments'), 'model_call_id': model_call_id},
                       turn, call_id=call['id'], status='running')
            try:
                name = call['function']['name']
                arguments = json.loads(call['function'].get('arguments', '{}'))
                if name in TOOLS:
                    if name not in HOT_TOOL_NAMES:
                        raise ValueError('非热车控必须delegate_vehicle_control')
                    validate(arguments, TOOLS[name].parameters)
                    if name == 'set_navigation_destination' and not destination_grounded(
                            arguments['destination'], self.destination_sources(turn)):
                        turn.needs_model = True
                        self.tool_result(turn, call, dict(DESTINATION_NOT_GROUNDED))
                        continue
                    if TOOLS[name].state_mapping.get('operation') == 'read':
                        turn.has_reads = True
                        turn.needs_model = True  # Reading must answer the returned facts, not a generic success template.
                    self.send_command(turn, call, name, arguments)
                elif name in MAPS_BY_NAME:
                    validate(arguments, MAPS_BY_NAME[name]['function']['parameters'])
                    if name in READ_TOOLS:
                        turn.has_reads = turn.needs_model = True
                        turn.route = 'lookup'
                        self.launch_lookup(turn, call, name, arguments)
                    else:
                        self.start_poi_navigation(turn, call, arguments['place_id'])
                else:
                    if name not in CONTROL_BY_NAME:
                        raise ValueError('未知工具')
                    validate(arguments, CONTROL_BY_NAME[name]['function']['parameters'])
                    if name == 'delegate_vehicle_control':
                        self.launch_vehicle_plan(turn, call, arguments)
                    else:
                        if name == 'delegate_task':
                            turn.route = 'delegate'
                        # Writes complete off-loop and come back as _task_write.
                        result = self.task_tool(name, arguments, turn=turn, call=call)
                        if result is not None:
                            if result.get('retry_model'):
                                turn.needs_model = True
                            self.tool_result(turn, call, result)
            except (ValueError, KeyError, ValidationError) as exc:
                self.tool_result(turn, call, {'ok': False, 'reason': f'参数校验失败：{str(exc).splitlines()[0][:180]}',
                                             'spoken_reason': validation_feedback(exc, call.get('function', {}).get('name'))})

    def destination_sources(self, turn):
        """Texts a destination may be copied from: this request, recent dialogue, found places."""
        sources = [turn.text]
        for previous in self.turns[-5:]:
            if previous is not turn:
                sources.extend(m['content'] for m in previous.messages
                               if m.get('role') in {'user', 'assistant'} and isinstance(m.get('content'), str))
        sources.extend(place.get('name', '') for place in self.recent_places)
        return sources

    def launch_lookup(self, turn, call, name, arguments):
        async def lookup():
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self.lookup_timeout):
                    result = await getattr(self.maps, name)(**arguments)
            except asyncio.CancelledError:
                self.trace('tool.cancelled', {'name': name, 'model_call_id': turn.tool_model_calls.get(call['id'])},
                           turn, call_id=call['id'], status='cancelled')
                return
            except MapsError as exc:
                result = {'ok': False, 'code': exc.code, 'reason': str(exc)}
            except TimeoutError:
                result = {'ok': False, 'code': 'timeout', 'reason': '查询超时，未取得结果。'}
            except Exception:
                result = {'ok': False, 'code': 'lookup_error', 'reason': '查询服务返回异常，未取得结果。'}
            result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            await self.post({'type': '_lookup_result', 'turn': turn, 'call': call, 'result': result})
        turn.lookup_tasks[call['id']] = self.spawn(lookup())

    def accept_lookup(self, turn, call, result):
        if self.is_current(turn):
            if call['function']['name'] == 'search_places' and result.get('ok'):
                self.recent_places = copy.deepcopy(result.get('places', []))
                self.places.update({place['place_id']: copy.deepcopy(place) for place in self.recent_places})
                while len(self.places) > 50:
                    self.places.pop(next(iter(self.places)))
            record = {'call_id': call['id'], 'tool': call['function']['name'], 'result': copy.deepcopy(result)}
            turn.lookup_results.append(record)
            self.emit('lookup.result', turn_id=turn.id, **record)
        self.tool_result(turn, call, result)

    def start_poi_navigation(self, turn, call, place_id):
        place = self.places.get(place_id)
        if not place:
            turn.needs_model = True
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'retry_model': True,
                'code': 'unknown_place', 'reason': '该地点不在本会话真实查询结果中，请先search_places或重新选择。'})
            return
        turn.poi_commands[call['id']] = copy.deepcopy(place)
        # Once a search has supplied the selected destination, the vehicle
        # receipt is the final answer. Retain continuation for other read work.
        prior_names = {item['function']['name'] for message in turn.messages
                       for item in message.get('tool_calls', [])}
        if len(turn.group) == 1 and prior_names <= {'search_places', 'start_poi_navigation'}:
            turn.has_reads = False
        self.emit('vehicle.function_call', turn_id=turn.id, call=call)
        self.send_command(turn, call, 'set_navigation_destination', {'destination': place['name']})

    def completed_vehicle_operations(self, turn):
        return [{'operation_id': command['command_id'], 'command_id': command['command_id'],
                 'name': command['name'], 'arguments': copy.deepcopy(command['arguments']),
                 'result': copy.deepcopy(self.completed_commands[command['command_id']])}
                for command in turn.commands if command['command_id'] in self.completed_commands]

    def vehicle_plan_history(self, turn):
        return [copy.deepcopy(message) for previous in self.turns[-4:] if previous is not turn
                for message in previous.messages
                if message.get('role') in {'user', 'assistant'} and not message.get('tool_calls')]

    def launch_vehicle_plan(self, turn, call, arguments):
        snapshot = copy.deepcopy(self.vehicle)
        history = self.vehicle_plan_history(turn)
        completed = self.completed_vehicle_operations(turn)
        self.trace('vehicle.plan.started', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                   'request_text': turn.text, 'skill_ids': arguments['skill_ids'],
                   'parent_call_id': call['id'], 'vehicle': snapshot,
                   'completed_operations': completed}, turn, call_id=call['id'], status='running')

        def trace(kind, data, *, call_id=None, status=None):
            self.trace(kind, {**data, 'agent': 'vehicle_planner', 'source': 'vehicle_plan',
                       'parent_call_id': call['id'],
                       'parent_model_call_id': turn.tool_model_calls.get(call['id'])},
                       turn, call_id=call_id, status=status if self.is_current(turn) else 'stale')

        async def plan():
            try:
                planned = await asyncio.wait_for(self.vehicle_planner.plan(
                    turn.text, snapshot, copy.deepcopy(arguments['skill_ids']), history=history,
                    completed_operations=completed, trace=trace), self.vehicle_planning_timeout)
                await self.post({'type': '_vehicle_plan_done', 'turn': turn, 'call': call, 'plan': planned})
            except asyncio.CancelledError:
                trace('vehicle.plan.cancelled', {'executed': False}, call_id=call['id'], status='cancelled')
                await self.post({'type': '_tool_result', 'turn': turn, 'call': call,
                    'result': {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                               'code': 'vehicle_plan_interrupted', 'status': 'interrupted',
                               'reason': '用户已打断，未下发车控计划。'}})
            except Exception as exc:
                error = '车控规划超过时间上限。' if isinstance(exc, asyncio.TimeoutError) else sanitized_error(exc)
                await self.post({'type': '_vehicle_plan_error', 'turn': turn, 'call': call, 'error': error})

        turn.vehicle_plan_tasks[call['id']] = self.spawn(plan())

    def accept_vehicle_plan(self, turn, call, plan):
        self.trace('vehicle.plan.received', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                   'parent_call_id': call['id'], 'plan': plan}, turn, call_id=call['id'],
                   status='completed' if self.is_current(turn) else 'stale')
        if not self.is_current(turn):
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                'code': 'vehicle_plan_interrupted', 'status': 'interrupted',
                'reason': '用户已打断，迟到计划没有下发。'})
            return
        if (isinstance(plan, dict) and plan.get('status') in {'refused', 'failed'} and plan.get('ok') is False
                and plan.get('intents') == [] and plan.get('actions') == [] and plan.get('keep_paths') == []
                and isinstance(plan.get('reason'), str) and plan['reason'].strip()):
            # Planner-generated transport/step failures are terminal failure
            # records, not executable plans. Preserve their original cause;
            # the execution validator still handles every nonempty action set.
            reason = plan['reason']
            original_code = plan.get('code') or plan.get('error_code')
            code = original_code or (reason if is_snake_code(reason) else 'vehicle_planning_failed')
            self.trace('vehicle.plan.failed', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                       'parent_call_id': call['id'], 'code': code, 'reason': reason,
                       'planner_error': plan}, turn, call_id=call['id'], status='failed')
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                'status': 'failed', 'code': code, 'reason': reason, 'original_error_code': original_code,
                'plan_call_id': plan.get('plan_call_id'), 'model_call_id': plan.get('model_call_id'),
                'model_calls': plan.get('model_calls'), 'intents': [], 'action_receipts': [],
                'keep_paths': [], 'read_results': [],
                'spoken_reason': '车控规划没有完成，计划中的动作没有下发。'})
            return
        try:
            # This is a fresh-state, whole-plan preflight. Simulation is never
            # accepted as an actual receipt and never mutates self.vehicle.
            validated = validate_vehicle_plan(plan, turn.text, copy.deepcopy(self.vehicle),
                completed_operations=self.completed_vehicle_operations(turn), history=self.vehicle_plan_history(turn))
            candidate = copy.deepcopy(self.vehicle)
            for action in validated.get('actions', []):
                self.validate_planned_action(turn, validated, action, candidate)
                result = execute_tool(action['name'], action['arguments'], candidate)
                if not result['ok']:
                    raise ValueError(result.get('reason', '计划动作被最新车况拒绝'))
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            self.trace('vehicle.plan.rejected', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                       'parent_call_id': call['id'], 'reason': str(exc)}, turn,
                       call_id=call['id'], status='rejected')
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                'code': 'vehicle_plan_invalid', 'reason': str(exc),
                'spoken_reason': '车控计划没有通过整组核对，其中的动作都没有下发。'})
            return
        if validated.get('status') != 'planned':
            question = validated.get('question') or next((item.get('question') for item in validated.get('intents', []) if item.get('question')), None)
            reason = validated.get('reason') or next((item.get('reason') for item in validated.get('intents', []) if item.get('reason')), None)
            self.tool_result(turn, call, {'ok': False, 'executed': False, 'source': 'vehicle_plan',
                'status': validated.get('status', 'refused'), 'needs_clarification': validated.get('status') == 'clarification',
                'intents': validated.get('intents', []), 'action_receipts': [],
                'keep_paths': validated.get('keep_paths', []), 'read_results': [],
                'plan_call_id': validated.get('plan_call_id'),
                'reason': question or reason or '车控需求还不能可靠确认。'})
            return
        self.trace('vehicle.plan.accepted', {'agent': 'vehicle_planner', 'source': 'vehicle_planner',
                   'parent_call_id': call['id'], 'plan': validated, 'executed': False}, turn,
                   call_id=call['id'], status='accepted')
        turn.vehicle_plans[call['id']] = {'call': call, 'plan': validated, 'index': 0,
            'action_receipts': [], 'interrupted': False, 'in_flight': set(),
            'keep_baseline': {path: state_path_value(self.vehicle, path) for path in validated.get('keep_paths', [])}}
        self.dispatch_next_planned_action(turn, call['id'])

    def validate_planned_action(self, turn, plan, action, state):
        name, arguments = action['name'], action['arguments']
        if name not in TOOLS:
            raise ValueError('计划包含非车控函数')
        validate(arguments, TOOLS[name].parameters)
        if name == 'set_navigation_destination' and not destination_grounded(
                arguments['destination'], self.destination_sources(turn)):
            raise ValueError(DESTINATION_NOT_GROUNDED['reason'])

    def execute_planned_command(self, turn, parent_id, action):
        """Validate and commit in one synchronous actor operation at real ACK.

        A successful dispatch-time simulation is not authority to commit: old
        already-dispatched commands may receive their ACK in between.
        """
        execution = turn.vehicle_plans[parent_id]
        base = {'ok': False, 'executed': False, 'name': action['name'],
                'arguments': copy.deepcopy(action['arguments']), 'source': 'vehicle_plan'}
        try:
            outcomes = {item['action_id']: item['status'] for item in execution['action_receipts']}
            if any(outcomes.get(dependency) != 'completed' for dependency in action.get('depends_on', [])):
                return {**base, 'code': 'vehicle_plan_dependency_failed',
                        'reason': '依赖动作未取得成功回执，本动作没有执行。', 'state': copy.deepcopy(self.vehicle)}
            constraints = [{'path': path, 'expected': copy.deepcopy(expected),
                            'actual': state_path_value(self.vehicle, path)}
                           for path, expected in execution['keep_baseline'].items()]
            if any(not same_state_value(item['expected'], item['actual']) for item in constraints):
                return {**base, 'code': 'vehicle_plan_keep_changed', 'keep_constraints': constraints,
                        'reason': '要求保持的设置在等待期间发生了变化，本动作没有执行。',
                        'state': copy.deepcopy(self.vehicle)}
            try:
                validate_vehicle_action_keep(action['name'], action['arguments'], self.vehicle,
                                             execution['plan'].get('keep_paths', []))
            except ValueError as exc:
                return {**base, 'code': 'vehicle_plan_keep_conflict', 'validation_reason': str(exc),
                        'reason': '按最新车况执行会写入要求保持的设置，本动作没有执行。',
                        'state': copy.deepcopy(self.vehicle)}
            self.validate_planned_action(turn, execution['plan'], action, self.vehicle)
            candidate = copy.deepcopy(self.vehicle)
            result = execute_tool(action['name'], action['arguments'], candidate)
            if not result.get('ok'):
                return {**result, 'executed': False, 'source': 'vehicle_plan'}
            # This catches dynamic side effects omitted from a static mapping,
            # such as temperature propagation after zone-sync was enabled.
            changed = [{'path': path, 'expected': copy.deepcopy(expected),
                        'proposed': state_path_value(candidate, path)}
                       for path, expected in execution['keep_baseline'].items()
                       if not same_state_value(expected, state_path_value(candidate, path))]
            if changed:
                return {**base, 'code': 'vehicle_plan_keep_conflict', 'keep_constraints': changed,
                        'reason': '按最新车况执行会改变要求保持的设置，本动作没有执行。',
                        'state': copy.deepcopy(self.vehicle)}
            self.vehicle.clear()
            self.vehicle.update(candidate)
            return {**result, 'executed': True, 'source': 'vehicle_plan', 'state': copy.deepcopy(self.vehicle)}
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            return {**base, 'code': 'vehicle_plan_commit_rejected', 'reason': str(exc),
                    'state': copy.deepcopy(self.vehicle)}

    def dispatch_next_planned_action(self, turn, parent_id):
        execution = turn.vehicle_plans[parent_id]
        actions = execution['plan']['actions']
        outcomes = {item['action_id']: item['status'] for item in execution['action_receipts']}
        for action in actions:
            if action['id'] in outcomes or action['id'] in execution['in_flight']:
                continue
            receipt = {'action_id': action['id'], 'intent_id': action['intent_id'],
                       'name': action['name'], 'arguments': copy.deepcopy(action['arguments'])}
            if not self.is_current(turn) or execution['interrupted']:
                execution['interrupted'] = True
                execution['action_receipts'].append({**receipt, 'status': 'interrupted',
                    'result': {'ok': False, 'executed': False, 'reason': '用户已打断，剩余动作没有下发。'}})
                continue
            outcomes = {item['action_id']: item['status'] for item in execution['action_receipts']}
            def paths(item):
                spec = TOOLS[item['name']]
                return ([spec.skill_id] if spec.state_mapping.get('operation') == 'read'
                        else _write_paths(item['name'], item['arguments'], self.vehicle))
            # Shared writes/reads stay ordered even if the planner omitted a
            # dependency. Climate sync has state-dependent cross-zone effects.
            earlier = actions[:actions.index(action)]
            conflicts = [item for item in earlier if
                (TOOLS[item['name']].skill_id == TOOLS[action['name']].skill_id == 'climate')
                or any(_overlap(a, b) for a in paths(item) for b in paths(action))]
            if any(item['id'] not in outcomes for item in conflicts):
                continue
            if any(dependency not in outcomes for dependency in action.get('depends_on', [])):
                continue
            if any(outcomes.get(dependency) != 'completed' for dependency in action.get('depends_on', [])):
                execution['action_receipts'].append({**receipt, 'status': 'blocked',
                    'result': {'ok': False, 'executed': False, 'reason': '依赖动作未成功，未执行本动作。'}})
                continue
            try:
                self.validate_planned_action(turn, execution['plan'], action, self.vehicle)
                probe = execute_tool(action['name'], action['arguments'], copy.deepcopy(self.vehicle))
                if not probe['ok']:
                    raise ValueError(probe.get('reason', '当前车况不允许本动作'))
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                execution['action_receipts'].append({**receipt, 'status': 'failed',
                    'result': {'ok': False, 'executed': False, 'reason': str(exc)}})
                continue
            child = {'id': f"{parent_id}:{action['id']}", 'type': 'function',
                     'function': {'name': action['name'], 'arguments': json.dumps(action['arguments'], ensure_ascii=False)}}
            execution['in_flight'].add(action['id'])
            turn.tool_model_calls[child['id']] = execution['plan'].get('model_call_id') or execution['plan'].get('plan_call_id')
            self.trace('vehicle.plan.action_started', {'agent': 'vehicle_planner', 'source': 'vehicle_plan',
                       'parent_call_id': parent_id, 'plan_call_id': execution['plan'].get('plan_call_id'),
                       'action': action}, turn, call_id=child['id'], status='running')
            self.emit('vehicle.function_call', turn_id=turn.id, call=child, source='vehicle_plan',
                      parent_call_id=parent_id, intent_id=action['intent_id'], action_id=action['id'])
            self.send_command(turn, child, action['name'], action['arguments'],
                              plan_parent_id=parent_id, plan_action=action)
        if not execution['in_flight'] and len(execution['action_receipts']) == len(actions):
            self.finish_vehicle_plan(turn, parent_id)

    def planned_action_result(self, turn, parent_id, action, result):
        execution = turn.vehicle_plans[parent_id]
        if any(item['action_id'] == action['id'] for item in execution['action_receipts']):
            return
        command_id = next((command['command_id'] for command in turn.commands
                           if command.get('action_id') == action['id'] and command.get('parent_call_id') == parent_id), None)
        execution['action_receipts'].append({'action_id': action['id'], 'intent_id': action['intent_id'],
            'command_id': command_id,
            'name': action['name'], 'arguments': copy.deepcopy(action['arguments']),
            'status': 'completed' if result.get('ok') else 'failed', 'result': copy.deepcopy(result)})
        execution['in_flight'].discard(action['id'])
        self.trace('vehicle.plan.action_completed', {'agent': 'vehicle_planner', 'source': 'vehicle_plan',
                   'parent_call_id': parent_id, 'action_id': action['id'], 'intent_id': action['intent_id'],
                   'command_id': command_id, 'result': result}, turn, call_id=f"{parent_id}:{action['id']}",
                   status='stale' if not self.is_current(turn) else 'completed' if result.get('ok') else 'failed')
        self.dispatch_next_planned_action(turn, parent_id)

    def finish_vehicle_plan(self, turn, parent_id):
        execution = turn.vehicle_plans[parent_id]
        plan, receipts = execution['plan'], execution['action_receipts']
        intents = []
        for original in plan['intents']:
            intent = copy.deepcopy(original)
            bound = [item for item in receipts if item['intent_id'] == intent['id']]
            if intent['status'] == 'completed':
                # Validator bound these IDs to actual earlier receipts from
                # this turn; the Pro cannot declare success from prose.
                pass
            elif intent['status'] == 'keep':
                unchanged = all(same_state_value(state_path_value(self.vehicle, path), execution['keep_baseline'][path])
                                for path in intent['keep_paths'])
                intent['status'] = 'kept' if unchanged else 'failed'
                if not unchanged:
                    intent['reason'] = '要求保持的设置在等待期间发生了变化，不能确认保持成功。'
            elif bound and all(item['status'] == 'completed' for item in bound):
                intent['status'] = 'completed'
            else:
                intent['status'] = next((item['status'] for item in bound if item['status'] != 'completed'), 'failed')
                intent['reason'] = next((item['result'].get('reason') for item in bound if not item['result'].get('ok')), '未取得实际执行回执。')
            intents.append(intent)
        reads = [copy.deepcopy(item) for item in receipts if TOOLS[item['name']].state_mapping.get('operation') == 'read'
                 and item['result'].get('ok')]
        ok = all(intent['status'] in {'completed', 'kept'} for intent in intents)
        result = {'ok': ok, 'executed': any(item['result'].get('ok') and
                    TOOLS[item['name']].state_mapping.get('operation') != 'read' for item in receipts),
                  'status': 'completed' if ok else 'interrupted' if execution['interrupted'] else 'partial',
                  'source': 'vehicle_plan', 'plan_call_id': plan.get('plan_call_id'),
                  'intents': intents, 'action_receipts': copy.deepcopy(receipts),
                  'keep_paths': copy.deepcopy(plan.get('keep_paths', [])), 'read_results': reads,
                  'keep_constraints': [{'path': path, 'expected': copy.deepcopy(expected),
                                        'actual': state_path_value(self.vehicle, path),
                                        'unchanged': same_state_value(expected, state_path_value(self.vehicle, path))}
                                       for path, expected in execution['keep_baseline'].items()]}
        if reads and self.is_current(turn):
            turn.has_reads = True
            turn.needs_model = True
        self.tool_result(turn, execution['call'], result)

    def vehicle_plan_feedback(self, turn, result, *, mutations_only=False):
        feedback = []
        for item in result.get('action_receipts', []):
            name, body = item['name'], item['result']
            if TOOLS[name].state_mapping.get('operation') == 'read' and body.get('ok'):
                continue
            if body.get('ok'):
                feedback.append(render(turn.persona, 'success', receipt_detail(name, item['arguments'], body)))
            elif not mutations_only:
                kind = 'result' if body.get('reason_code') == 'receipt_timeout' else 'refused'
                feedback.append(render(turn.persona, kind, body.get('spoken_reason') or spoken_reason(body.get('reason', '动作未执行'))))
        if not mutations_only:
            if any(item.get('status') == 'kept' for item in result.get('intents', [])):
                feedback.append('要求保持的设置未调整。')
            feedback.extend(item['reason'] for item in result.get('intents', [])
                            if item.get('status') == 'failed' and item.get('keep_paths') and item.get('reason'))
            if not result.get('action_receipts') and not result.get('ok'):
                feedback.append(render(turn.persona, 'clarify' if result.get('needs_clarification') else 'refused',
                    result.get('spoken_reason') or spoken_reason(result.get('reason', '车控计划未执行。'))))
        return feedback

    def vehicle_plan_error(self, turn):
        return next((result.get('code') or 'vehicle_plan_partial' for result in turn.tool_results.values()
                     if result.get('source') == 'vehicle_plan' and result.get('ok') is False
                     and not result.get('needs_clarification') and not result.get('retry_model')), None)

    def handled_vehicle_settings(self, turn):
        return sum(1 for message in turn.messages for call in message.get('tool_calls', [])
                   if call['id'] in turn.tool_results and call['function']['name'] in TOOLS
                   and TOOLS[call['function']['name']].state_mapping.get('operation') != 'read'
                   and not turn.tool_results[call['id']].get('retry_model'))



    def resolve_task(self, task_id=None):
        tasks = self.task_list()
        if not tasks:
            return None, {'ok': False, 'code': 'no_task', 'retry_model': True, 'executed': False,
                          'reason': '本会话尚无后台任务，不能manage_task。请根据本轮原句重新选择直接回答或实际可用功能；车控缺功能时delegate_vehicle_control，不要创建无关后台任务。'}
        if task_id:
            found = next((t for t in tasks if t['id'] == task_id), None)
            return found, None if found else {'ok': False, 'reason': '此会话没有该任务'}
        active = [t for t in tasks if t['status'] not in {'completed', 'failed', 'cancelled'}]
        if len(active) == 1:
            return active[0], None
        if not active and len(tasks) == 1:
            return tasks[0], None
        return None, {'ok': False, 'needs_clarification': True, 'reason': '请明确指哪个任务',
                      'candidates': [{'id': t['id'], 'goal': t['goal'], 'status': t['status']} for t in (active or tasks)[-8:]]}

    def task_context(self, origin_turn_id=None, read_details=False, query_mode='research', spoken_goal=None,
                     query_domain='general', required_source_domains=()):
        from backend.models import utcnow
        recent = [{'role': message['role'], 'content': message['content']}
                  for turn in self.turns[-4:] for message in turn.messages
                  if message.get('role') in {'user', 'assistant'} and message.get('content') and not message.get('tool_calls')]
        return {'vehicle': copy.deepcopy(self.vehicle), 'recent_history': recent,
                'origin_turn_id': origin_turn_id, 'query_mode': query_mode, 'spoken_goal': spoken_goal,
                'query_domain': query_domain,
                'required_source_domains': [d.lower() for d in required_source_domains if is_domain(d)],
                'read_details': read_details,
                'active_tasks': [{k:t[k] for k in ('id','goal','status','summary')} for t in self.task_list()
                                 if t['status'] not in {'completed','failed','cancelled'}],
                'captured_at': utcnow(), 'timezone': self.vehicle['_meta']['timezone'],
                'notice': TASK_SNAPSHOT_NOTICE}

    def task_tool(self, name, args, *, turn=None, call=None):
        """Return a result for reads; for writes launch them and return None."""
        if name == 'delegate_task':
            # A typed product profile, not weather detection or a router.
            # Reject the original incompatible parameters before any task
            # exists; only the same Talker may choose corrected arguments.
            if args.get('query_domain') == 'weather' and args.get('query_mode') != 'quick':
                return {'ok': False, 'executed': False, 'retry_model': True,
                        'code': 'weather_quick_required',
                        'delegation_policy_guard': {'kind': 'typed_weather_profile',
                            'query_domain': 'weather', 'requested_query_mode': args.get('query_mode'),
                            'supported_query_mode': 'quick'},
                        'reason': '本原型weather入口只支持显式query_mode=quick，包括官网天气查询。当前参数不符合入口配置，尚未登记任务；请由同一Talker重新核对并选择参数，不能改走非天气领域冒充。'}
            origin = self.current.id if self.current else None
            context = self.task_context(origin, args.get('read_details') is True,
                                        args.get('query_mode', 'research'), args.get('spoken_goal'),
                                        args.get('query_domain', 'general'),
                                        args.get('required_source_domains', ()))
            goal = speech_text(args.get('spoken_goal') or args['goal']).rstrip('。')
            self.launch_task_write(self.db.create_task, (self.id, args['goal'], context), turn=turn, call=call,
                                   meta={'kind': 'delegated', 'origin_turn_id': origin, 'detail': goal})
            return None
        if 'task_ids' in args:
            if args.get('action') != 'get' or args.get('task_id') is not None:
                return {'ok': False, 'reason': 'task_ids只用于批量get，不能同时指定task_id。'}
            identities = args['task_ids']
            if (not isinstance(identities, list) or not 1 <= len(identities) <= 8
                    or any(not isinstance(identity, str) or not identity.strip() for identity in identities)
                    or len(set(identities)) != len(identities)):
                return {'ok': False, 'reason': '请指定一到八个不同任务ID。'}
            selected = []
            for task_id in args['task_ids']:
                task, error = self.resolve_task(task_id)
                if error:
                    return error
                selected.append(task)
            items = [self.task_tool('manage_task', {'action': 'get', 'task_id': task['id'],
                                                   'read_details': args.get('read_details') is True}) for task in selected]
            detail = '\n'.join(f"{(task.get('context') or {}).get('spoken_goal') or task['goal']}：{item['detail']}" for task, item in zip(selected, items))
            return {'ok': True, 'tasks': selected, 'feedback_kind': 'progress', 'detail': detail,
                    'items': items}
        task, error = self.resolve_task(args.get('task_id'))
        if error:
            return error
        if args.get('action') == 'get':
            result = task.get('result')
            if task['status'] == 'completed' and isinstance(result, dict):
                detail = result.get('summary', '') if args.get('read_details') is True else result.get('spoken_summary') or brief_result(result.get('summary', ''))
            else:
                detail = task.get('question') or task['summary']
                if detail == step_summary(task.get('steps')):
                    detail = '还在分析，结果出来就告诉你。'
            return {'ok': True, 'task': task, 'feedback_kind': 'progress', 'detail': detail}
        action = args['action']
        self.launch_task_write(self.db.mutate_task, (task['id'], action, args.get('text', ''), args.get('expected_revision')),
                               turn=turn, call=call,
                               meta={'kind': 'cancel' if action == 'cancel' else 'updated',
                                     'detail': '正在取消这项任务。' if action == 'cancel' else '新条件记下了，会按这些要求继续。'})
        return None

    def launch_task_write(self, operation, arguments, *, turn=None, call=None, meta=None):
        async def write():
            try:
                outcome = {'task': await asyncio.to_thread(operation, *arguments)}
            except ValueError as exc:
                outcome = {'error': str(exc)}
            except Exception as exc:
                outcome = {'error': '任务记录失败：' + sanitized_error(exc)}
            await self.post({'type': '_task_write', 'turn': turn, 'call': call, 'meta': meta or {}, **outcome})
        self.spawn(write())

    def task_write_done(self, e):
        task, meta = e.get('task'), e['meta']
        if task:
            self.remember_task(task)
            self.announcements.pop(task['id'], None)
            card = task_card(task)
            if meta.get('kind') == 'delegated':
                self.emit('ui.card', task=card, origin_turn_id=meta.get('origin_turn_id'))
            else:
                self.emit('ui.card', task=card)
        if e['call'] is None:  # Explicit UI action: no Talker turn waits for it.
            if not task:
                self.emit('error', code='task_error', message=e['error'])
            return
        result = ({'ok': True, 'task': task_card(task), 'feedback_kind': meta['kind'], 'detail': meta['detail']}
                  if task else {'ok': False, 'reason': e['error']})
        self.tool_result(e['turn'], e['call'], result)

    def explicit_task(self, action, event):
        if action == 'create':
            goal = str(event.get('goal', '')).strip()
            if not goal or len(goal) > 4000:
                raise ValueError('任务目标需为1–4000字')
            self.launch_task_write(self.db.create_task, (self.id, goal, self.task_context()), meta={'kind': 'created'})
            return
        task, error = self.resolve_task(event.get('task_id'))
        if error:
            self.emit('error', message=error['reason'], candidates=error.get('candidates', []))
            return
        if action == 'get':
            self.emit('ui.card', task=task)
            return
        self.launch_task_write(self.db.mutate_task, (task['id'], action, str(event.get('text', '')),
                                                     event.get('expected_revision')), meta={'kind': action})

    def send_command(self, turn, call, name, arguments, *, plan_parent_id=None, plan_action=None):
        command_id = uuid4().hex[:12]
        wire = {'command_id': command_id, 'turn_id': turn.id, 'name': name, 'arguments': arguments}
        if plan_parent_id is not None:
            wire.update(source='vehicle_plan', parent_call_id=plan_parent_id,
                        action_id=plan_action['id'], intent_id=plan_action['intent_id'],
                        plan_call_id=turn.vehicle_plans[plan_parent_id]['plan'].get('plan_call_id'))
        execution = turn.vehicle_plans.get(plan_parent_id, {})
        self.simulator.register(wire, keep=execution.get('keep_baseline'), place=turn.poi_commands.get(call['id']))
        self.pending_commands[command_id] = {'turn': turn, 'call': call, 'wire': wire,
                                             'plan_parent_id': plan_parent_id, 'plan_action': plan_action}
        turn.commands.append(copy.deepcopy(wire))
        self.trace('vehicle.dispatched', {**wire, 'model_call_id': turn.tool_model_calls.get(call['id'])},
                   turn, call_id=call['id'], status='pending')
        self.emit('vehicle.command', **wire)
        if not turn.dispatch_announced:
            acknowledgement = dispatch_feedback(name, arguments,
                grouped=plan_parent_id is not None or len(turn.group) > 1)
            if acknowledgement:
                turn.dispatch_announced = True
                turn.metrics['dispatch_feedback_ms'] = (time.perf_counter() - turn.started) * 1000
                self.append_text(turn, acknowledgement, source='vehicle_dispatch')
                self.trace('vehicle.dispatch_feedback', {'text': acknowledgement, 'command_id': command_id,
                    'completion_claimed': False}, turn, call_id=call['id'], status='dispatched')
        async def expire():
            await asyncio.sleep(TOOL_TIMEOUT)
            await self.post({'type': '_command_timeout', 'command_id': command_id})
        self.spawn(expire())

    def execute_command(self, command_id, device_receipt=None):
        command = self.pending_commands.pop(command_id, None)
        if not command:
            return  # duplicates and timed out commands cannot execute twice
        wire, turn = command['wire'], command['turn']
        if not self.is_current(turn):
            self.trace('vehicle.receipt', {'command_id': command_id, 'result': device_receipt,
                       'late': True}, turn, call_id=command['call']['id'], status='stale')
            return
        if device_receipt is not None:
            result = copy.deepcopy(device_receipt)
            if result['state_version'] >= self.vehicle_version:
                self.vehicle = copy.deepcopy(result['state'])
                self.vehicle_version = result['state_version']
        elif command.get('plan_parent_id') is not None:
            result = self.execute_planned_command(turn, command['plan_parent_id'], command['plan_action'])
            if not result.get('ok'):
                self.trace('vehicle.plan.commit_rejected', {'agent': 'vehicle_planner', 'source': 'vehicle_plan',
                           'parent_call_id': command['plan_parent_id'], 'action': command['plan_action'],
                           'command_id': command_id, 'result': result}, turn,
                           call_id=command['call']['id'], status='stale' if not self.is_current(turn) else 'rejected')
        else:
            result = execute_tool(wire['name'], wire['arguments'], self.vehicle)
        poi = turn.poi_commands.get(command['call']['id'])
        if result.get('ok') and poi:
            self.vehicle['navigation']['destination_place'] = copy.deepcopy(poi)
            result['state'] = copy.deepcopy(self.vehicle)
            result.update(feedback_kind='result', detail=f'已将模拟导航目的地设为{poi["name"]}，未计算实际路线。',
                          place=copy.deepcopy(poi))
        latency = (time.perf_counter() - turn.started) * 1000
        self.completed_commands[command_id] = result
        attribution = {key: wire[key] for key in ('source', 'parent_call_id', 'plan_call_id', 'action_id', 'intent_id') if key in wire}
        self.trace('vehicle.receipt', {**attribution, 'command_id': command_id, 'result': result, 'latency_ms': latency,
                   'late': not self.is_current(turn)}, turn, call_id=command['call']['id'],
                   status='stale' if not self.is_current(turn) else 'completed' if result['ok'] else 'failed')
        if len(self.completed_commands) > 500:
            self.completed_commands.pop(next(iter(self.completed_commands)))
        turn.commands[-1 if len(turn.commands) == 1 else next(i for i,c in enumerate(turn.commands) if c['command_id'] == command_id)].update({'receipt_ms': latency, 'ok': result['ok']})
        self.emit('vehicle.result', command_id=command_id, turn_id=turn.id, result=result, latency_ms=latency)
        self.emit('vehicle.state', state=self.vehicle)
        if command.get('plan_parent_id') is not None:
            self.planned_action_result(turn, command['plan_parent_id'], command['plan_action'], result)
        else:
            self.tool_result(turn, command['call'], result)

    def command_timeout(self, command_id):
        command = self.pending_commands.pop(command_id, None)
        if not command:
            return
        self.simulator.expire(command_id)
        turn = command['turn']
        if not self.is_current(turn):
            self.trace('vehicle.receipt', {'command_id': command_id, 'timeout': True, 'late': True}, turn,
                       call_id=command['call']['id'], status='stale')
            return
        result = {'ok': False, 'reason_code': 'receipt_timeout', 'resume_condition': None,
                  'reason': '未在时限内收到车端回执，执行状态尚未确认；请检查连接。'}
        self.completed_commands[command_id] = result
        attribution = {key: command['wire'][key] for key in ('source', 'parent_call_id', 'plan_call_id', 'action_id', 'intent_id') if key in command['wire']}
        self.trace('vehicle.receipt', {**attribution, 'command_id': command_id, 'result': result, 'timeout': True,
                   'late': not self.is_current(turn)}, turn, call_id=command['call']['id'],
                   status='stale' if not self.is_current(turn) else 'failed')
        self.emit('vehicle.result', command_id=command_id, turn_id=turn.id, result=result,
                  latency_ms=(time.perf_counter() - turn.started) * 1000)
        if command.get('plan_parent_id') is not None:
            self.planned_action_result(turn, command['plan_parent_id'], command['plan_action'], result)
        else:
            self.tool_result(turn, command['call'], result)

    def tool_result(self, turn, call, result):
        if call['id'] in turn.group_results:
            return
        name = call['function']['name']
        if name == 'delegate_vehicle_control' and result.get('source') == 'vehicle_plan':
            result = copy.deepcopy(result)
            result.setdefault('status', 'completed' if result.get('ok') else 'failed')
            result.setdefault('intents', [])
            result.setdefault('action_receipts', [])
            result.setdefault('read_results', [])
            result.setdefault('keep_paths', [])
            execution = turn.vehicle_plans.setdefault(call['id'], {'call': call,
                'plan': {'status': result['status'], 'intents': result['intents'], 'actions': []},
                'index': 0, 'action_receipts': result['action_receipts'], 'in_flight': set(),
                'interrupted': result['status'] == 'interrupted'})
            execution['ledger'] = copy.deepcopy(result)
            self.trace('vehicle.plan.completed', {'agent': 'vehicle_planner', 'parent_call_id': call['id'],
                       'source': 'vehicle_planner', 'ledger': result, 'status': result['status']}, turn,
                       call_id=call['id'], status='stale' if not self.is_current(turn) else
                       'completed' if result.get('ok') else 'clarification' if result.get('needs_clarification') else 'failed')
        self.trace('tool.completed' if result.get('ok', True) else 'tool.error',
                   {'name': name, 'arguments': call['function'].get('arguments'), 'result': result,
                    'model_call_id': turn.tool_model_calls.get(call['id']), 'late': not self.is_current(turn)},
                   turn, call_id=call['id'], status='stale' if not self.is_current(turn)
                   else 'completed' if result.get('ok', True) else 'failed',
                   task_id=result.get('task', {}).get('id'))
        turn.group_results[call['id']] = result
        turn.tool_results[call['id']] = result
        if len(turn.group_results) != len(turn.group):
            return
        for item in turn.group:
            body = turn.group_results[item['id']]
            turn.messages.append({'role': 'tool', 'tool_call_id': item['id'], 'content': json.dumps(body, ensure_ascii=False, default=str)})
        turn.group = []
        if not self.is_current(turn):
            return
        if turn.needs_model or turn.has_reads:
            self.launch_model(turn)
            return
        # The outer delegated call closes only after its per-intent ledger is
        # terminal; action-count floors never decide a Pro plan's completion.
        text = self.receipt_feedback(turn) or '工具执行结束。'
        if turn.dispatch_announced:
            turn.output = ''
            self.append_text(turn, text, speak=False)
            self.speak_vehicle_failures(turn)
        else:
            self.append_text(turn, text)
        turn.messages.append({'role': 'assistant', 'content': text})
        self.finish(turn, error=self.vehicle_plan_error(turn))

    def speak_vehicle_failures(self, turn):
        failures = []
        for command in turn.commands:
            result = self.completed_commands.get(command['command_id'])
            if result and not result.get('ok'):
                failures.append(result.get('reason') or '车端未能执行该操作。')
        for execution in turn.vehicle_plans.values():
            for receipt in execution['action_receipts']:
                if receipt['status'] not in {'completed'} and not receipt.get('command_id'):
                    failures.append(receipt['result'].get('reason') or '后续操作没有执行。')
        if failures:
            self.feed_audio(turn, '抱歉，' + '；'.join(dict.fromkeys(failures)), 'vehicle_failure')

    def receipt_feedback(self, turn, mutations_only=False):
        feedback = []
        all_calls = [call for message in turn.messages for call in message.get('tool_calls', [])
                     if call['id'] in turn.tool_results and
                     call['function']['name'] != 'load_skill']
        all_calls = [call for call in all_calls if
                     not turn.tool_results[call['id']].get('retry_model') and
                     not (call['function']['name'] in TOOLS and TOOLS[call['function']['name']].state_mapping.get('operation') == 'read')
                     and (not mutations_only or call['function']['name'] in TOOLS
                          or call['function']['name'] in {'delegate_vehicle_control', 'start_poi_navigation'})
                     and call['function']['name'] not in READ_TOOLS]
        for item in all_calls:
            body = turn.tool_results[item['id']]
            if item['function']['name'] == 'delegate_vehicle_control' and body.get('source') == 'vehicle_plan':
                feedback.extend(self.vehicle_plan_feedback(turn, body, mutations_only=mutations_only))
                continue
            if body.get('ok', True):
                if body.get('feedback_kind'):
                    feedback.append(render(turn.persona, body['feedback_kind'], body['detail']))
                elif item['function']['name'] in TOOLS:
                    # Values are from the verified receipt, never generated claims.
                    feedback.append(render(turn.persona, 'success', receipt_detail(item['function']['name'], json.loads(item['function']['arguments']), body)))
            else:
                detail = body.get('spoken_reason') or spoken_reason(body.get('reason', body.get('error', '执行器拒绝')))
                if body.get('candidates'):
                    detail = '你是指哪项任务？可以在对应的任务卡里补充。'
                kind = ('clarify' if body.get('needs_clarification') else
                        'result' if body.get('reason_code') == 'receipt_timeout' else 'refused')
                feedback.append(render(turn.persona, kind, detail))
        successful_vehicle_only = all_calls and all(
            item['function']['name'] in TOOLS and turn.tool_results[item['id']].get('ok')
            for item in all_calls)
        return ('，'.join(part.rstrip('。.!！') for part in feedback) + '。'
                if len(feedback) > 1 and successful_vehicle_only else '\n'.join(feedback))

    def finish(self, turn, error=None):
        if not self.is_current(turn):
            return
        turn.finished, self.generating = True, False
        metrics = {'first_effective_ms': turn.first_effective_ms, 'total_ms': (time.perf_counter() - turn.started) * 1000,
                   'model_calls': turn.model_calls, 'route': turn.route, 'commands': turn.commands, 'error': error}
        turn.metrics.update(metrics)
        self.close_audio_input(turn)
        self.trace('turn.completed', {'output': turn.output, 'metrics': metrics,
                   'output_source': turn.metrics.get('output_source') or ('persona_template' if turn.route not in {'chat', 'lookup'} or turn.proactive or error else 'model_content')},
                   turn, status='failed' if error else 'completed')
        self.emit('stream.done', turn_id=turn.id, text=turn.output, metrics=metrics)
        # Let browser send playback state (and user input take priority) before proactive speech.
        self.schedule_announce()

    def task_event(self, event):
        if event['event_id'] <= self.event_id:
            return
        self.event_id = event['event_id']
        # Events arrive in commit order, so the mirror holds the newest state;
        # an event older than a revision this actor already wrote is skipped.
        self.remember_task(event['payload'].get('task'))
        task = self.tasks.get(event['task_id'])
        if not task or event['revision'] < task['revision']:
            return
        self.emit('task.event', task=task, event_id=event['event_id'], revision=event['revision'])
        key = (task['id'], task['revision'], task['status'])
        if task['status'] in {'completed', 'failed', 'waiting_clarification', 'cancelled'} and key not in self.announced_keys:
            self.announcements[task['id']] = task
            self.schedule_announce()

    def schedule_announce(self):
        async def later():
            await asyncio.sleep(.25)
            await self.post({'type': '_announce'})
        self.spawn(later())

    def announce(self):
        if not self.clients or self.generating or self.speaking or self.asr_capture_id or not self.announcements:
            return
        task_id = next(iter(self.announcements))
        task = self.announcements.pop(task_id)
        latest = self.tasks.get(task_id)
        if not latest or latest['revision'] != task['revision'] or latest['status'] != task['status']:
            return
        self.announced_keys.add((task['id'], task['revision'], task['status']))
        kind = 'clarify' if task['status'] == 'waiting_clarification' else 'error' if task['status'] == 'failed' else 'result'
        result = task.get('result')
        full_report = (result.get('summary') or result.get('answer') or json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result) or task['summary']
        read_details = task.get('context', {}).get('read_details') is True
        selected = full_report if read_details else (result.get('spoken_summary') if isinstance(result, dict) else None) or brief_result(str(full_report))
        detail = task.get('question') or (spoken_reason(task['error']) if task.get('error') else None) or selected
        # Suggestions are validated against the latest state. They are not auto-applied based on old context.
        suggestions = result.get('actions', result.get('suggested_actions', [])) if isinstance(result, dict) else []
        if suggestions:
            checks = []
            for action in suggestions:
                name, args = action.get('name'), action.get('arguments', {})
                probe = execute_tool(name, args, copy.deepcopy(self.vehicle)) if name in TOOLS else {'ok': False, 'reason': '未知函数'}
                checks.append({'name': name, 'arguments': args, 'valid': probe['ok'], 'reason': probe.get('reason')})
            self.emit('ui.card', task={**task, 'validated_actions': checks})
            detail += '这些只是建议，还没有调整车辆。你想先做哪一项？'
        turn = Turn(uuid4().hex[:12], '', self.persona.model_copy(deep=True), self.persona_version)
        turn.proactive, turn.task_id = True, task_id
        self.current = turn
        self.turns.append(turn)
        self.trace('turn.started', {'input': '', 'proactive': True, 'persona_version': turn.persona_version},
                   turn, status='running')
        self.emit('turn.started', turn_id=turn.id, text='', persona_version=turn.persona_version, proactive=True, task_id=task_id)
        self.start_audio(turn)
        # Keep line structure until display syntax has been removed; persona
        # presentation folds whitespace and would otherwise hide report tables.
        text = render(turn.persona, kind, speech_text(str(detail)))
        attribution = {key: task[key] for key in ('id', 'goal', 'revision', 'status')}
        turn.messages = [
            {'role': 'system', 'content': render_prompt('history_attribution',
                attribution_json=json.dumps(attribution, ensure_ascii=False))},
            {'role': 'assistant', 'content': text},
        ]
        self.append_text(turn, text, source='thinker_full_report' if read_details else 'thinker_spoken_summary',
                         original_text=str(full_report))
        self.finish(turn)

    async def close(self):
        await self.simulator.close()
        for turn in self.turns:
            self.cancel_audio(turn, 'session_closed')
        self.runner.cancel()
        self.poller.cancel()
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(self.runner, self.poller, *list(self.background), return_exceptions=True)
        await self.trace_queue.join()
        self.trace_writer.cancel()
        await asyncio.gather(self.trace_writer, return_exceptions=True)
        if self.owns_maps:
            await self.maps.aclose()
