"""Single-writer session actor. Network/model callbacks only enqueue events."""
import asyncio
import copy
import logging
import math
import time
import unicodedata
from uuid import uuid4
from jsonschema import validate
from super_xd.nlp.catalog.vehicle import TOOLS
from super_xd.demo.devices.simulator import initial_vehicle_state
from super_xd.demo.session.nlp import NLPBridge
from super_xd.demo.session.state import Turn
from super_xd.demo.session.notifications import NotificationQueue
from super_xd.demo.session.audio import synthesize_audio
from super_xd.nlp.contracts.events import SpeakingContext
from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner
from super_xd.nlp.diagnostics import safe_diagnostic
from super_xd.nlp.prompts.persona import PERSONAS, render
from super_xd.nlp.contracts.catalog import PersonaConfig
from super_xd.demo.contracts import SessionSnapshot
from super_xd.nlp.defaults import TOOL_TIMEOUT
from super_xd.nlp.adapters.common import utcnow, sanitized_error
from super_xd.nlp.policy.product import resolve_task, resolve_place_reference
from super_xd.nlp.prompts.speech_text import speech_text
from super_xd.demo.resources.manual_search import ManualSearchClient
from super_xd.nlp.catalog.manual import MANUAL_BY_NAME
from super_xd.demo.devices.vehicle import VehicleSimulator
from super_xd.nlp.prompts.speech import feedback_text
from super_xd.nlp.textutil import is_identifier
from super_xd.demo.tasks.database import TERMINAL as TERMINAL_TASK, task_card
from super_xd.nlp.policy.speaker import DEFAULT_SPEAKER_SEAT, speaker_context
from super_xd.nlp.contracts.media import CockpitError
from super_xd.demo.devices.cockpit import initial_cockpit_state
from super_xd.nlp.media_diagnostics import provider_diagnostic
from super_xd.nlp.catalog.cockpit import COCKPIT_BY_NAME
from super_xd.nlp.prompts.catalog import cockpit_feedback
from super_xd.demo.devices.preparation import prepare_media
from super_xd.nlp.policy.cockpit import prepare_navigation, validated_receipt, scoped_place, finite, observed_media_state
from super_xd.demo.devices.catalog import DeviceRequest, validate_device_receipt, receipt_failure
from super_xd.demo.audio.providers import PCM24K_TTS
from super_xd.nlp.contracts.providers import ProviderError, redact
from super_xd.nlp.adapters.factory import create_maps, create_search


FIRST_CLAUSE_MIN_CHARS = 8  # Shorter openers ("好，") wait for the rest of the sentence.


def effective_text(text):
    """Metric only: at least four letters/digits/CJK characters have streamed."""
    return sum(1 for c in text if unicodedata.category(c)[0] in 'LN') >= 4


class SessionActor(NLPBridge):
    def __init__(self, session_id, db, ark, tts=None, vehicle_planner=None, maps=None, web=None, manual=None, cockpit=None):
        self.id, self.db, self.ark = session_id, db, ark
        self.tts = tts
        self.vehicle_planner = vehicle_planner or VehicleControlPlanner(ark)
        self.maps = maps if maps is not None else create_maps()
        self.owns_maps = maps is None
        self.web = web if web is not None else create_search()
        self.owns_web = web is None
        self.manual = manual or ManualSearchClient()
        self.owns_manual = manual is None
        self.cockpit = cockpit
        self.cockpit_state = initial_cockpit_state()
        self.media_choices = {}
        self.pending_cockpit = {}
        self.cockpit_latest = {}
        self.cockpit_generation = 0
        self.cockpit_active = {}
        self.cockpit_deferred = {'media': [], 'navigation': []}
        self.lookup_timeout = 25
        self.places = {}  # Session-local identities from successful, adopted search results.
        self.place_turns = {}  # place_id -> turn whose search surfaced it
        self.recent_places = []
        self.audio_clients = set()
        self.audio_enabled = False
        self.queue = asyncio.Queue()
        self.clients = set()
        self.persona = PERSONAS['warm'].model_copy(deep=True)
        self.persona_version = 1
        self.speaker_seat = DEFAULT_SPEAKER_SEAT
        self.vehicle = initial_vehicle_state()
        self.simulator = VehicleSimulator(self.vehicle)
        self.vehicle_version = 0
        self.turns = []
        self.current = None
        self.generating = False
        self.speaking = False
        self.asr_capture_id = None
        self.pending_commands = {}
        self.completed_commands = {}
        self.event_id = 0
        self.notifications = NotificationQueue()
        self.speaking_context = SpeakingContext()
        self.last_activity = time.monotonic()
        # Read-side mirror of this session's tasks, fed by the event poller and
        # by this actor's own writes, so handle() never blocks on SQLite.
        self.tasks = {}
        self.background = set()
        self.trace_queue = asyncio.Queue()
        self.trace_error = None
        self.trace_writer = asyncio.create_task(self.write_traces())
        self.runner = asyncio.create_task(self.run())
        self.poller = asyncio.create_task(self.poll_events())
        self.configure_nlp()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def trace(self, kind, data, turn=None, *, call_id=None, status=None, task_id=None):
        # Trace writes have a separate ordered queue; SQLite never blocks the
        # actor's model deltas, command dispatch or receipt handling.
        self.trace_queue.put_nowait((kind, safe_diagnostic({'agent': 'talker', 'observed_at': utcnow(), **data},
                                                        getattr(self, 'nlp_secrets', ())),
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
                client.put_nowait({'type': 'audio.error', 'message': feedback_text('errors.audio_receiver_slow')})
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

    def audio_format(self):
        return self.tts.output_format if self.tts else PCM24K_TTS

    def start_audio(self, turn):
        if self.asr_capture_id or not (self.audio_enabled and self.audio_clients and self.tts):
            return
        turn.playback_id, turn.audio_queue = uuid4().hex, asyncio.Queue()
        self.speaking = True  # Includes synthesis and queued PCM, until browser drain.
        identity = {'session_id': self.id, 'turn_id': turn.id, 'playback_id': turn.playback_id,
                    'client_request_id': turn.client_request_id}
        self.emit_audio({'type': 'audio.started', **identity, **self.audio_format().to_dict()})
        self.trace('audio.started', identity, turn, status='running')

        turn.audio_task = self.spawn(synthesize_audio(self.tts, turn.audio_queue, turn.persona.voice, turn, self.post))

    def feed_audio(self, turn, text, source='persona_template', original_text=None):
        clean = speech_text(text)
        if clean and self.is_current(turn):
            # When the first words were ready for TTS, with or without an audio client.
            turn.metrics.setdefault('first_speech_ms', (time.perf_counter() - turn.started) * 1000)
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
            # The first chunk may end at a comma so audio starts before the whole
            # sentence has streamed; later chunks keep full-sentence prosody.
            early = (not turn.first_clause_fed and character in '，,；;：:'
                     and len(turn.sentence_buffer[end:index].strip()) >= FIRST_CLAUSE_MIN_CHARS)
            if character in '。！？!?\n' or early:
                self.feed_audio(turn, turn.sentence_buffer[end:index + 1], 'model_stream')
                turn.first_clause_fed = True
                end = index + 1
        turn.sentence_buffer = turn.sentence_buffer[end:]
        if final and turn.sentence_buffer:
            self.feed_audio(turn, turn.sentence_buffer, 'model_stream')
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
        result['speaker'] = speaker_context(self.speaker_seat)
        result['cockpit'] = copy.deepcopy(self.cockpit_state)
        result['cockpit_capabilities'] = self.cockpit.info() if self.cockpit else {'configured': False}
        result['tool_capabilities'] = self.tool_capabilities()
        result['device'] = {'device_id': 'browser:' + self.id, 'online': bool(self.clients)}
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
        if not kind.startswith('_') or kind in {'_attach', '_detach', '_audio_attach', '_audio_detach'}:
            self.last_activity = time.monotonic()
        if kind == '_attach':
            self.clients.add(e['outgoing'])
            e['outgoing'].put_nowait({'type': 'session.snapshot', **self.snapshot()})
            for command in self.pending_commands.values():
                e['outgoing'].put_nowait({'type': 'vehicle.command', **command['wire']})
            for command in self.pending_cockpit.values():
                if (command['wire']['kind'] == 'query' or command['generation'] == self.cockpit_latest.get(command['wire']['domain'])) and (command['turn'] is None or self.is_current(command['turn'])):
                    e['outgoing'].put_nowait({'type': 'cockpit.command', **command['wire']})
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
        elif kind == '_cockpit_prepared':
            self.accept_cockpit_prepared(e)
        elif kind == 'cockpit.receipt':
            self.accept_cockpit_receipt(e)
        elif kind == '_cockpit_timeout':
            self.cockpit_timeout(e['command_id'])
        elif kind == 'cockpit.control':
            self.cockpit_ui_control(e)
        elif kind == 'cockpit.media_state':
            self.accept_media_state(e)
        elif kind == 'attention.set':
            # Demo fixture for Gateway-provided call/driving facts; no semantic inference.
            update = {key: e[key] for key in ('in_call', 'demanding_driving') if key in e}
            if any(type(value) is not bool for value in update.values()):
                raise ValueError('Attention facts must be booleans')
            self.speaking_context = self.speaking_context.model_copy(update=update)
            self.schedule_announce()
        elif kind == 'input':
            text = str(e.get('text', '')).strip()
            if not text or len(text) > 4000:
                raise ValueError('输入需为1–4000字')
            seat = e.get('speaker_seat', self.speaker_seat)
            speaker_context(seat)
            self.asr_capture_id = None
            self.start_turn(text, str(e.get('client_request_id', ''))[:80] or None, speaker_seat=seat)
        elif kind == 'speaker.set':
            speaker = speaker_context(e.get('seat'))
            self.speaker_seat = speaker['seat']
            self.emit('speaker.changed', speaker=speaker)
        elif kind == 'asr.capture':
            capture_id = e.get('capture_id')
            if not is_identifier(capture_id, max_length=80):
                raise ValueError('无效语音录音标识')
            if e.get('active') is True:
                if self.asr_capture_id == capture_id:
                    return
                self.asr_capture_id = capture_id
                # Voice activity only stops playback. VAD cannot tell speech from a
                # cough, road noise or our own speaker echo, so cancelling the turn
                # here would also void already-dispatched vehicle commands. A real
                # utterance arrives as input and start_turn interrupts the old turn.
                if self.current:
                    self.cancel_audio(self.current, 'voice_activity' if e.get('interrupt') is True else 'asr_capture')
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
                target.played_ms = max(target.played_ms, min(elapsed, self.audio_format().duration_ms(target.audio_bytes)))
                target.played_text = ''.join(item['text'] for item in target.sentences
                                            if item['end_ms'] <= target.played_ms)
                if target.audio_finished and target.played_ms >= self.audio_format().duration_ms(target.audio_bytes) - 1:
                    target.played_text = ''.join(target.spoken_parts)
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
        elif kind == '_nlp_diagnostics':
            self.nlp_diagnostics(e)
        elif kind == '_nlp_event':
            self.accept_nlp_event(e['turn'], e['event'])
        elif kind == '_gateway_execute':
            self.execute_gateway_request(e)
        elif kind == '_nlp_error':
            if self.is_current(e['turn']):
                self.trace('run.error', {'error': e['error']}, e['turn'], status='failed')
                self.append_text(e['turn'], render(e['turn'].persona, 'error', feedback_text('errors.model_connection')))
                self.finish(e['turn'], error=e['error'])
        elif kind == '_nlp_closed':
            turn = e['turn']
            if self.is_current(turn):
                self.append_text(turn, feedback_text('errors.receipt_timeout'))
                self.finish(turn, error='execution_unknown')
        elif kind == '_audio_item':
            turn, item = e['turn'], e['item']
            if not self.audio_valid(turn): return
            audio = item.get('audio')
            if isinstance(audio, bytes) and audio:
                if len(audio) % (self.audio_format().channels * self.audio_format().sample_width):
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
                if item.get('type') == 'alignment' and item.get('precision') == 'provider':
                    start, end = item.get('start_ms'), item.get('end_ms')
                    if isinstance(item.get('text'), str) and finite(start) and finite(end) and 0 <= start <= end:
                        turn.sentences.append({'text': item['text'], 'start_ms': start, 'end_ms': end})
                diagnostics = {k: v for k, v in item.items() if k != 'audio'}
                self.emit_audio({'type': 'audio.metrics', 'turn_id': turn.id,
                                 'playback_id': turn.playback_id, 'client_request_id': turn.client_request_id,
                                 'metrics': diagnostics})
        elif kind == '_audio_finished':
            turn = e['turn']
            if not self.audio_valid(turn): return
            turn.audio_finished = True
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
            self.trace('audio.error', {'playback_id': turn.playback_id, 'error': e['error'],
                       'provider_error': e.get('provider_error'), 'diagnostics': e.get('diagnostics')}, turn, status='failed')
            self.emit_audio({'type': 'audio.error', 'turn_id': turn.id, 'playback_id': turn.playback_id,
                             'message': e['error'], 'provider_code': (e.get('provider_error') or {}).get('code')})
            turn.audio_cancelled.set()
            if turn.audio_task and not turn.audio_task.done(): turn.audio_task.cancel()
            self.speaking = False
            self.schedule_announce()
        elif kind == '_lookup_result':
            self.accept_lookup(e['turn'], e['call'], e['result'])
        elif kind == '_manual_images':
            self.manual_images_done(e['turn'], e['call_id'], e['result'])
        elif kind == 'vehicle.configure':
            self.vehicle = self.simulator.configure(e.get('settings', {}))
            self.vehicle_version = self.simulator.version
            self.emit('vehicle.state', state=self.vehicle)
            self.trace('vehicle.configured', {'settings': e.get('settings', {})})
        elif kind == 'vehicle.receipt':
            receipt = self.simulator.receipt(e.get('command_id'), e.get('receipt_id'))
            self.execute_command(e.get('command_id'), receipt)
        elif kind == '_command_timeout':
            self.command_timeout(e['command_id'])
        elif kind == '_task_event':
            self.task_event(e['event'])
        elif kind == '_tasks_loaded':
            for task in e['tasks']:
                self.remember_task(task)
        elif kind == '_task_write':
            self.task_write_done(e)
        elif kind == '_task_presented':
            self.accept_task_presentation(e)
        elif kind.startswith('task.'):
            self.explicit_task(kind[5:], e)
        else:
            raise ValueError(f'未知消息类型：{kind}')

    def is_current(self, turn):
        return self.current is turn and not turn.finished

    def interrupt_current(self, reason):
        if self.current:
            old = self.current
            self.cancel_audio(old, reason)
            if old.gateway:
                old.gateway.cancel()
            if old.model_task and not old.model_task.done():
                old.model_task.cancel()
            for job in old.lookup_tasks.values():
                if not job.done():
                    job.cancel()
            self.simulator.invalidate(old.id)
            if not old.finished:
                old.finished, old.interrupted = True, True
                self.trace('turn.interrupted', {'output': old.output, 'reason': reason}, old, status='interrupted')
                self.emit('stream.interrupted', turn_id=old.id)
        self.speaking = False
        self.generating = False


    def start_turn(self, text, client_request_id=None, *, speaker_seat=None):
        self.interrupt_current('new_input')
        turn = Turn(uuid4().hex[:12], text, self.persona.model_copy(deep=True), self.persona_version)
        turn.client_request_id = client_request_id
        turn.speaker_seat = speaker_seat or self.speaker_seat
        turn.messages.append({'role': 'user', 'content': text})
        self.turns.append(turn)
        self.current, self.generating = turn, True
        self.trace('turn.started', {'input': text, 'persona_version': turn.persona_version,
                   'speaker': speaker_context(turn.speaker_seat)}, turn, status='running')
        self.emit('turn.started', turn_id=turn.id, text=text, persona_version=turn.persona_version,
                  client_request_id=client_request_id)
        self.start_audio(turn)
        self.context_revision += 1
        self.launch_nlp(turn)


    def append_text(self, turn, text, speak=True, source='persona_template', original_text=None):
        selected_text = text
        if original_text is None: original_text = text
        if speak: text = speech_text(text)
        turn.output += text
        if turn.first_effective_ms is None and effective_text(turn.output):
            turn.first_effective_ms = (time.perf_counter() - turn.started) * 1000
        self.emit('stream.delta', turn_id=turn.id, text=text)
        if speak: self.feed_audio(turn, selected_text, source, original_text)


    def accept_lookup(self, turn, call, result):
        embedding_trace = result.pop('_embedding_trace', None)
        if embedding_trace is not None:
            self.trace('embedding.completed', embedding_trace, turn, call_id=call['id'],
                       status='ok' if self.is_current(turn) else 'stale')
        if self.is_current(turn):
            if call['function']['name'] == 'search_places' and result.get('ok'):
                self.recent_places = [scoped_place(place, result.get('provider')) for place in result.get('places', [])]
                result['places'] = copy.deepcopy(self.recent_places)
                self.places.update({place['place_ref']: copy.deepcopy(place) for place in self.recent_places})
                self.place_turns.update({place['place_ref']: turn.id for place in self.recent_places})
                while len(self.places) > 50:
                    self.place_turns.pop(self.places.pop(next(iter(self.places)))['place_ref'], None)
            if call['function']['name'] == 'search_media' and result.get('ok'):
                from super_xd.nlp.contracts.media import media_item
                items = [media_item(item) | {'source_turn_id': turn.id} for item in result.get('items', [])]
                self.media_choices = {item['content_id']: item for item in items}
                result['items'] = copy.deepcopy(items)
                self.emit('cockpit.catalog', items=items, provider=result.get('provider'))
            record = {'call_id': call['id'], 'tool': call['function']['name'], 'result': copy.deepcopy(result)}
            turn.lookup_results.append(record)
            self.emit('lookup.result', turn_id=turn.id, **record)
            if call['function']['name'] in MANUAL_BY_NAME and result.get('ok') and hasattr(self.manual, 'with_page_images'):
                self.launch_manual_images(turn, call, copy.deepcopy(result))
        self.tool_result(turn, call, result)

    def launch_manual_images(self, turn, call, result):
        async def render():
            try:
                updated = await self.manual.with_page_images(result)
            except Exception as exc:  # The card keeps its text evidence and PDF link.
                self.trace('manual.images.error', {'error': sanitized_error(exc)}, turn, call_id=call['id'], status='failed')
                return
            await self.post({'type': '_manual_images', 'turn': turn, 'call_id': call['id'], 'result': updated})
        self.spawn(render())

    def manual_images_done(self, turn, call_id, result):
        # Display-only card data; an interrupted turn's card is not updated.
        if turn.interrupted:
            return
        for record in turn.lookup_results:
            if record['call_id'] == call_id:
                record['result'] = copy.deepcopy(result)
                self.emit('lookup.result', turn_id=turn.id, **record)

    def resolve_place_reference(self, reference):
        return resolve_place_reference(reference, self.places)


    def resolve_task(self, task_id=None):
        return resolve_task(self.task_list(), task_id)


    def task_write_done(self, e):
        task, meta = e.get('task'), e['meta']
        if task:
            self.remember_task(task)
            self.notifications.discard(task['id'])
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


    def send_command(self, turn, call, name, arguments, *, keep_baseline=None):
        pending = turn.pending_results[call['id']][1]
        command_id = pending.invocation_id
        kind = 'query' if TOOLS[name].state_mapping.get('operation') == 'read' else 'control'
        wire = DeviceRequest(command_id, self.id, turn.id, 'vehicle', name, arguments, kind,
                             'browser:' + self.id, int(TOOL_TIMEOUT * 1000)).to_wire()
        wire.update(name=name, idempotency_key=pending.idempotency_key)
        keep = copy.deepcopy(keep_baseline or {})
        groups = turn.checkpoint.orchestration.groups if turn.checkpoint else []
        if groups and groups[-1].plan is not None:
            plan = groups[-1].plan.model_dump()
            action = next(row for row in plan['actions'] if row['id'] == call['id'])
            wire.update(source='vehicle_plan', parent_call_id=groups[-1].requests[groups[-1].cursor].request.tool_call_id,
                        action_id=action['id'], intent_id=action['intent_id'], plan_call_id=plan.get('plan_call_id'))
            self.emit('vehicle.function_call', turn_id=turn.id, call=copy.deepcopy(call), source='vehicle_plan',
                      parent_call_id=wire['parent_call_id'], action_id=wire['action_id'], intent_id=wire['intent_id'])
        self.simulator.register(wire, keep=keep)
        self.pending_commands[command_id] = {'turn': turn, 'call': call, 'wire': wire}
        turn.commands.append(copy.deepcopy(wire))
        self.trace('vehicle.dispatched', wire, turn, call_id=call['id'], status='pending')
        self.emit('vehicle.command', **wire)
        async def expire():
            await asyncio.sleep(TOOL_TIMEOUT)
            await self.post({'type': '_command_timeout', 'command_id': command_id})
        self.spawn(expire())


    def execute_command(self, command_id, device_receipt=None):
        command = self.pending_commands.pop(command_id, None)
        if not command:
            return
        turn = command['turn']
        if device_receipt is None:
            raise ValueError('Device execution requires an authoritative receipt')
        result = copy.deepcopy(device_receipt)
        late = not self.is_current(turn)
        self.trace('vehicle.receipt', {'command_id': command_id, 'result': result, 'late': late}, turn,
                   call_id=command['call']['id'], status='stale' if late else 'completed')
        if result.get('state_version', -1) >= self.vehicle_version and isinstance(result.get('state'), dict):
            self.vehicle, self.vehicle_version = copy.deepcopy(result['state']), result['state_version']
        if late:
            return
        latency = (time.perf_counter() - turn.started) * 1000
        self.completed_commands[command_id] = result
        if len(self.completed_commands) > 500:
            self.completed_commands.pop(next(iter(self.completed_commands)))
        next(item for item in turn.commands if item['command_id'] == command_id).update(receipt_ms=latency, ok=result.get('ok'))
        self.emit('vehicle.result', command_id=command_id, turn_id=turn.id, result=result, latency_ms=latency)
        self.emit('vehicle.state', state=self.vehicle)
        self.tool_result(turn, command['call'], result)


    def command_timeout(self, command_id):
        command = self.pending_commands.pop(command_id, None)
        if command is None:
            return
        turn = command['turn']
        result = {'ok': False, 'executed': False, 'outcome_unknown': True, 'code': 'receipt_timeout',
                  'reason_code': 'receipt_timeout', 'reason': feedback_text('errors.receipt_timeout')}
        late = not self.is_current(turn)
        self.trace('vehicle.receipt', {'command_id': command_id, 'result': result, 'timeout': True, 'late': late},
                   turn, call_id=command['call']['id'], status='stale' if late else 'unknown')
        if self.is_current(turn):
            self.emit('vehicle.result', command_id=command_id, turn_id=turn.id, result=result)
            self.tool_result(turn, command['call'], result)


    def finish(self, turn, error=None):
        if not self.is_current(turn):
            return
        turn.finished, self.generating = True, False
        metrics = {'first_effective_ms': turn.first_effective_ms, 'total_ms': (time.perf_counter() - turn.started) * 1000,
                   'model_calls': turn.model_calls + turn.metrics.get('planner_model_calls', 0),
                   'model_calls_by_role': {'talker': turn.model_calls, 'planner': turn.metrics.get('planner_model_calls', 0)},
                   'first_useful_ms': turn.metrics.get('first_useful_ms'),
                   'route': turn.route, 'commands': turn.commands, 'error': error,
                   'preface': turn.preface or None,
                   'first_model_text_ms': turn.metrics.get('server_first_model_text_ms'),
                   'first_speech_ms': turn.metrics.get('first_speech_ms')}
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
        if event['kind'] == 'progress_notice' and task['status'] in {'queued', 'running'}:
            self.notifications.offer(task, event_id=event['event_id'], progress=True)
        else:
            self.notifications.offer(task, event_id=event['event_id'])
        self.schedule_announce()

    def schedule_announce(self, delay=.25):
        async def later():
            await asyncio.sleep(delay)
            await self.post({'type': '_announce'})
        self.spawn(later())

    def announce(self):
        context = self.speaking_context.model_copy(update={'connected': bool(self.clients),
            'generating': self.generating, 'assistant_speaking': self.speaking,
            'user_speaking': bool(self.asr_capture_id)})
        notice = self.notifications.select(self.tasks, context)
        if notice is not None:
            self.present_task(self.tasks[notice.task_id], notice=notice)


    def observed_media_state(self, result):
        return observed_media_state(self.cockpit_state['media'], {**result, 'state_version': result.get('state_version')})

    def media_provider_info(self, kind=None, content_id=None):
        if not self.cockpit:
            return {'configured': False, 'execution_location': 'cloud'}
        catalog = self.cockpit.media
        if content_id and hasattr(catalog, 'content_provider_info'):
            return catalog.content_provider_info(content_id)
        if hasattr(catalog, 'provider_info'):
            return catalog.provider_info(kind or 'music')
        return catalog.info()

    def tool_capabilities(self):
        return [capability.model_dump() for capability in self.nlp_capabilities]

    def send_device_query(self, turn, call, domain, action, arguments, *, continuation=None):
        pending = turn.pending_results.get(call['id']) if turn and call and not continuation else None
        identifier = pending[1].invocation_id if pending else uuid4().hex
        wire = DeviceRequest(identifier, self.id, turn.id if turn else 'ui-' + uuid4().hex[:12],
                             domain, action, arguments, 'query', 'browser:' + self.id, int(TOOL_TIMEOUT * 1000)).to_wire()
        if pending:
            wire['idempotency_key'] = pending[1].idempotency_key
        self.pending_cockpit[identifier] = {'wire': wire, 'turn': turn, 'call': call, 'continuation': continuation}
        self.trace('device.query.dispatched', wire, turn, status='pending')
        self.emit('cockpit.command', **wire)
        async def expire():
            await asyncio.sleep(TOOL_TIMEOUT)
            await self.post({'type': '_cockpit_timeout', 'command_id': identifier})
        self.spawn(expire())

    def device_failure_result(self, wire, receipt):
        failure = receipt_failure(wire, receipt)
        result = self.cockpit_error(failure.code, domain=wire['domain'], action=wire['action'])
        result.update(retryable=failure.retryable, execution_location='device',
                      provider_error={**failure.public(), 'message': result['reason']})
        return result

    def trace_device_query(self, command, receipt, result, *, status=None):
        wire, turn = command['wire'], command['turn']
        failure = receipt_failure(wire, receipt) if receipt.get('ok') is False else None
        self.trace('device.query.completed', {'request_id': wire['request_id'], 'domain': wire['domain'], 'action': wire['action'],
                   'receipt': provider_diagnostic(receipt), 'result': provider_diagnostic(result),
                   'provider_error': failure.public() if failure else None,
                   'diagnostics': failure.details if failure else None}, turn,
                   status=status or ('completed' if result.get('ok') else 'failed'))

    def accept_device_query(self, command, receipt):
        wire, turn, call = command['wire'], command['turn'], command['call']
        if turn and not self.is_current(turn):
            self.trace_device_query(command, receipt, {'ok': False, 'late': True}, status='stale')
            return
        result = receipt.get('result') if receipt['ok'] else self.device_failure_result(wire, receipt)
        result = copy.deepcopy(result)
        try:
            if receipt['ok'] and result.get('ok', True) is not True:
                raise CockpitError('device_failed', 'Device query result is not successful')
            result['ok'] = receipt['ok']
            result['execution_location'] = 'device'
            if receipt['ok'] and wire['domain'] == 'media':
                from super_xd.nlp.contracts.media import media_item
                if wire['action'] in ('search','resolve') and hasattr(self.cockpit.media,'scope_device_result'):
                    result = self.cockpit.media.scope_device_result(wire['arguments']['kind'],result)
                if wire['action'] == 'get_status':
                    state = self.observed_media_state(result)
                    continuation = command.get('continuation')
                    if continuation and continuation['generation'] != self.cockpit_latest.get('media'):
                        self.trace_device_query(command, receipt, {'ok': False, 'late': True}, status='stale')
                        return
                    self.cockpit_state['media'] = state
                    self.emit('cockpit.state', domain='media', state=copy.deepcopy(state))
                    if continuation:
                        self.trace_device_query(command, receipt, {'ok': True, 'kind': 'media_status', 'state': state})
                        self.launch_cockpit(turn, call, 'control_media', continuation['arguments'],
                            trusted_selection=True, reserved_generation=continuation['generation'])
                        self.cockpit_active['media']['retry_count'] = 1
                        return
                    result = {'ok': True, 'kind': 'media_status', 'state': copy.deepcopy(state), 'execution_location': 'device'}
                elif wire['action'] == 'resolve':
                    item = media_item(result.get('item'))
                    logical_id = command['continuation']['arguments']['content_id']
                    if item['content_id'] != logical_id:
                        raise CockpitError('unknown_content', 'Device resolved a different content')
                    continuation = command['continuation']
                    if continuation['generation'] != self.cockpit_latest.get('media'):
                        self.trace_device_query(command, receipt, {'ok': False, 'late': True}, status='stale')
                        return
                    self.media_choices[item['content_id']] = item
                    self.trace_device_query(command, receipt, {'ok': True, 'kind': 'media_resolved', 'item': item})
                    self.launch_cockpit(turn, call, continuation['name'], continuation['arguments'],
                        trusted_selection=continuation['trusted_selection'], reserved_generation=continuation['generation'])
                    return
                if wire['action'] == 'search' and (not isinstance(result.get('items'), list) or len(result['items']) > wire['arguments'].get('max_results', 5)):
                    raise CockpitError('invalid_action', 'Device catalog did not return items')
                if wire['action'] == 'search':
                    items = [media_item(item) for item in result['items']]
                    if any(item['kind'] != wire['arguments']['kind'] for item in items):
                        raise CockpitError('invalid_action', 'Device catalog returned a different kind')
                    result.update(kind='media', items=items)
                    self.media_choices = {item['content_id']: item for item in items}
                    self.emit('cockpit.catalog', items=items, provider=result.get('provider'), execution_location='device')
            if wire['domain'] == 'places' and turn:
                if receipt['ok'] and not isinstance(result.get('places'), list):
                    raise CockpitError('unknown_place', 'Device places query did not return places')
                if receipt['ok']:
                    result['places'] = [scoped_place(place, result.get('provider')) for place in result['places']]
                result.setdefault('kind', 'places')
                self.trace_device_query(command, receipt, result)
                self.accept_lookup(turn, call, result)
                return
        except (CockpitError, TypeError, KeyError):
            result = self.cockpit_error('device_failed')
        self.trace_device_query(command, receipt, result)
        if turn:
            result['cockpit_read'] = True
            self.tool_result(turn, call, result)
        else:
            self.emit('cockpit.result', domain=wire['domain'], result=result)
        if command.get('continuation'): self.advance_cockpit('media', command['continuation'])

    def cockpit_error(self, code, *, domain=None, action=None):
        known = {'not_configured', 'unknown_content', 'unknown_place', 'place_not_confirmed', 'no_content',
                 'queue_boundary', 'invalid_action', 'device_failed', 'device_timeout', 'stale_receipt', 'video_driving', 'route_not_configured', 'origin_required', 'unsupported_coordinate_system', 'unsupported_capability'}
        key = code if code in known else 'device_failed'
        if domain == 'navigation' and key == 'device_failed':
            key = 'route_failed' if action == 'route' else 'navigation_failed'
        return {'ok': False, 'code': code, 'executed': False, 'reason': cockpit_feedback(key),
                'spoken_reason': cockpit_feedback(key)}

    def cockpit_ui_control(self, event):
        domain, action, arguments = event.get('domain'), event.get('action'), event.get('arguments', {})
        if domain not in ('media', 'navigation') or not isinstance(arguments, dict):
            self.emit('error', code='invalid_cockpit_control', message=cockpit_feedback('invalid_action'))
            return
        name = 'control_media' if domain == 'media' else 'manage_navigation'
        if domain == 'media' and action == 'search':
            validate(arguments, COCKPIT_BY_NAME['search_media']['function']['parameters'])
            self.launch_cockpit(None, None, 'search_media', arguments, trusted_selection=True)
            return
        arguments = {**arguments, 'action': action}
        validate(arguments, COCKPIT_BY_NAME[name]['function']['parameters'])
        if domain == 'navigation' and action in ('start', 'add_waypoint', 'set_origin'):
            reference = self.resolve_place_reference(arguments.get('place_ref'))
            if not reference:
                self.emit('error', code='unknown_place', message=cockpit_feedback('unknown_place'))
                return
            arguments['place_ref'] = reference
        if arguments.get('origin_ref'):
            reference = self.resolve_place_reference(arguments['origin_ref'])
            if not reference:
                self.emit('error', code='unknown_place', message=cockpit_feedback('unknown_place'))
                return
            arguments['origin_ref'] = reference
        self.launch_cockpit(None, None, name, arguments, trusted_selection=True)

    def launch_cockpit(self, turn, call, name, arguments, *, trusted_selection=False, reserved_generation=None):
        if not self.cockpit:
            result = self.cockpit_error('not_configured')
            if turn: self.tool_result(turn, call, result)
            else: self.emit('error', code=result['code'], message=result['reason'])
            return
        domain = 'navigation' if name == 'manage_navigation' else 'media'
        action = arguments.get('action')
        mutation = name != 'search_media' and action not in ('get_status', 'route')
        active = self.cockpit_active.get(domain)
        if mutation and reserved_generation is None and turn and active and active['turn'] is turn:
            self.cockpit_deferred[domain].append((turn, call, name, arguments, trusted_selection))
            return
        snapshot = copy.deepcopy(self.cockpit_state[domain])
        choices, places = copy.deepcopy(self.media_choices), copy.deepcopy(self.places)
        if reserved_generation is None:
            self.cockpit_generation += 1
        generation = self.cockpit_generation if reserved_generation is None else reserved_generation
        # Read-only queries do not invalidate a device command already in flight.
        if mutation:
            self.cockpit_latest[domain] = generation
            self.cockpit_active[domain] = {'turn': turn, 'call': call, 'generation': generation}
            if not turn: self.cockpit_deferred[domain].clear()
        if domain == 'media' and action == 'get_status':
            self.send_device_query(turn, call, 'media', 'get_status', {})
            return
        try:
            provider_info = self.media_provider_info(arguments.get('kind') or snapshot.get('kind'),
                arguments.get('content_id') if action == 'play' else None)
        except CockpitError as error:
            result = self.cockpit_error(error.code)
            if turn: self.tool_result(turn,call,result)
            else: self.emit('cockpit.result',domain=domain,result=result)
            self.advance_cockpit(domain,{'generation':generation})
            return
        if provider_info.get('execution_location') == 'device':
            if name == 'search_media':
                self.send_device_query(turn, call, 'media', 'search', {**arguments, 'provider_binding':provider_info.get('binding')})
                return
            if domain == 'media' and action == 'play' and arguments.get('content_id') not in choices and trusted_selection:
                raw_id = self.cockpit.media.device_content_id(arguments['content_id']) if hasattr(self.cockpit.media,'device_content_id') else arguments['content_id']
                self.send_device_query(turn, call, 'media', 'resolve', {'content_id': raw_id, 'kind':provider_info.get('kind') or snapshot.get('kind'), 'provider_binding':provider_info.get('binding')},
                    continuation={'name': name, 'arguments': arguments, 'trusted_selection': trusted_selection, 'generation': generation})
                return
        vehicle = copy.deepcopy(self.vehicle)
        speaker_seat = turn.speaker_seat if turn else self.speaker_seat
        if turn and (name == 'search_media' or action in ('get_status', 'route')):
            turn.route = 'lookup'
        async def prepare():
            try:
                async with asyncio.timeout(self.lookup_timeout):
                    if name == 'search_media':
                        result = await self.cockpit.media.search(**arguments)
                        from super_xd.nlp.contracts.media import media_item
                        if not isinstance(result, dict) or result.get('ok') is not True or not isinstance(result.get('items'), list) or len(result['items']) > arguments.get('max_results', 5):
                            raise CockpitError('invalid_content', 'Media provider returned an invalid catalog response')
                        result['items'] = [media_item(item) for item in result['items']]
                        if any(item['kind'] != arguments['kind'] for item in result['items']):
                            raise CockpitError('invalid_content', 'Media provider returned a different kind')
                        outcome = {'result': result, 'read': True, 'search': True}
                    elif action == 'get_status':
                        outcome = {'result': {'ok': True, 'kind': domain + '_status', 'state': snapshot}, 'read': True}
                    elif domain == 'navigation' and action == 'route':
                        if not snapshot.get('destination'):
                            raise CockpitError('unknown_place', 'No navigation destination')
                        origin = (places.get(arguments.get('origin_ref')) or snapshot.get('origin')
                                  or vehicle.get('_meta', {}).get('coordinates'))
                        result = await self.cockpit.route(origin, snapshot['destination'], snapshot['waypoints'])
                        outcome = {'result': result, 'read': True}
                    elif domain == 'media':
                        command = await prepare_media(self.cockpit, action, arguments, snapshot, choices, trusted_selection=trusted_selection)
                        if command['expected'].get('kind') == 'video' and vehicle['_meta'].get('speed_kph', 0) > 0 and speaker_seat == 'driver':
                            raise CockpitError('video_driving', 'Driver video is unavailable while moving')
                        outcome = {'command': command}
                    else:
                        outcome = {'command': prepare_navigation(action, arguments, snapshot, places)}
            except asyncio.CancelledError:
                return
            except Exception as error:
                code = getattr(error, 'code', 'device_failed')
                if domain == 'navigation' and action == 'route' and code == 'not_configured': code = 'route_not_configured'
                outcome = {'result': self.cockpit_error(code, domain=domain, action=action), 'read': False,
                           'error': {'code': code,
                                     'provider_error': redact(error.public()) if isinstance(error, ProviderError) else None,
                                     'diagnostics': redact(error.details) if isinstance(error, ProviderError) else None}}
            await self.post({'type': '_cockpit_prepared', 'turn': turn, 'call': call, 'name': name,
                             'domain': domain, 'action': action, 'arguments': arguments, 'generation': generation,
                             'version': snapshot['version'], 'outcome': outcome})
        job = self.spawn(prepare())
        if turn: turn.lookup_tasks[call['id']] = job

    def accept_cockpit_prepared(self, event):
        turn, call, domain = event['turn'], event['call'], event['domain']
        outcome = event['outcome']
        if turn and not self.is_current(turn):
            self.trace('cockpit.stale', {'name': event['name'], 'stage': 'prepared'}, turn, status='stale')
            return
        if 'command' not in outcome:
            result = outcome['result']
            provider_trace = result.pop('_provider_trace', None)
            self.trace('cockpit.prepared', {'name': event['name'], 'domain': domain, 'action': event.get('action'),
                       'arguments': redact(event.get('arguments', {})), 'result': redact(result),
                       'error': outcome.get('error'), 'provider_trace': provider_trace}, turn, status='completed' if result.get('ok') else 'failed')
            if outcome.get('search') and result.get('ok'):
                from super_xd.nlp.contracts.media import media_item
                items = [media_item(item) for item in result.get('items', [])]
                self.media_choices = {item['content_id']: item for item in items}
                result['items'] = items
                self.emit('cockpit.catalog', items=items, provider=result.get('provider'))
            if (event['name'] == 'manage_navigation' and result.get('ok') and outcome.get('read')
                    and event['version'] == self.cockpit_state['navigation']['version']
                    and result.get('provider') and result.get('configured') is True):
                self.cockpit_state['navigation']['route'] = copy.deepcopy(result)
                self.emit('cockpit.state', domain='navigation', state=copy.deepcopy(self.cockpit_state['navigation']))
            if turn:
                result['cockpit_read'] = outcome.get('read', False)
                self.tool_result(turn, call, result)
            else:
                self.emit('cockpit.result', domain=domain, result=result)
            self.advance_cockpit(domain, event)
            return
        if event['generation'] != self.cockpit_latest.get(domain) or event['version'] != self.cockpit_state[domain]['version']:
            if turn: self.tool_result(turn, call, self.cockpit_error('stale_receipt'))
            self.advance_cockpit(domain, event)
            return
        command = outcome['command']
        if command.get('provider_trace'):
            self.trace('cockpit.provider', {'name': event['name'], 'action': command['action'],
                       'provider_trace': command['provider_trace']}, turn, status='completed')
        pending = turn.pending_results.get(call['id']) if turn and call else None
        command_id = pending[1].invocation_id if pending else uuid4().hex
        wire = DeviceRequest(command_id, self.id, turn.id if turn else 'ui-' + uuid4().hex[:12],
                             domain, command['action'], command['arguments'], 'control', 'browser:' + self.id,
                             int(TOOL_TIMEOUT * 1000)).to_wire()
        wire['expected_state_version'] = event['version']
        if pending:
            wire['idempotency_key'] = pending[1].idempotency_key
        self.pending_cockpit[command_id] = {**command, 'wire': wire, 'turn': turn, 'call': call, 'generation': event['generation']}
        self.trace('cockpit.dispatched', {**wire, 'arguments': {key: ('[redacted playback capability]' if key == 'source_url' else value)
                    for key,value in wire['arguments'].items()}}, turn, status='pending')
        self.emit('cockpit.command', **wire)
        async def expire():
            await asyncio.sleep(TOOL_TIMEOUT)
            await self.post({'type': '_cockpit_timeout', 'command_id': command_id})
        self.spawn(expire())

    def accept_cockpit_receipt(self, event):
        command = self.pending_cockpit.get(event.get('command_id'))
        if not command:
            return
        wire, turn = command['wire'], command['turn']
        try:
            validate_device_receipt(wire, event)
        except CockpitError:
            self.trace('cockpit.receipt.rejected', {'command_id': wire['command_id']}, turn, status='stale')
            return
        self.trace('device.receipt', {'request_id': wire['request_id'], 'domain': wire['domain'],
                   'receipt': provider_diagnostic(event)}, turn, status='received')
        self.pending_cockpit.pop(event['command_id'])
        if wire['kind'] == 'query':
            self.accept_device_query(command, event)
            return
        domain = wire['domain']
        if (turn and not self.is_current(turn) or command['generation'] != self.cockpit_latest.get(domain)
                or wire['expected_state_version'] != self.cockpit_state[domain]['version']
                or event.get('turn_id', wire['turn_id']) != wire['turn_id']):
            self.trace('cockpit.receipt', {'command_id': wire['command_id'], 'late': True}, turn, status='stale')
            self.advance_cockpit(domain, command)
            return
        if (event.get('ok') is False and domain == 'media' and isinstance(event.get('error'), dict) and event['error'].get('code') == 'state_conflict'
                and not self.cockpit_active.get(domain, {}).get('retry_count')):
            active = self.cockpit_active.get(domain)
            if active: active['retry_count'] = 1
            arguments = {**wire['arguments'], 'action': wire['action']}
            arguments = {key: value for key, value in arguments.items() if key in ('action', 'content_id', 'position_seconds', 'volume')}
            self.send_device_query(turn, command['call'], 'media', 'get_status', {},
                continuation={'generation': command['generation'], 'arguments': arguments})
            return
        if event.get('ok') is True:
            try:
                state = validated_receipt(command, event)
                self.cockpit_state[domain] = state
                result = {'ok': True, 'executed': True, 'domain': domain, 'state': copy.deepcopy(state),
                          'feedback_kind': 'success', 'detail': cockpit_feedback('media_done' if domain == 'media' else 'navigation_done')}
                self.emit('cockpit.state', domain=domain, state=copy.deepcopy(state))
            except CockpitError as error:
                result = self.cockpit_error(error.code, domain=domain, action=wire['action'])
        else:
            result = self.device_failure_result(wire, event)
        self.trace('cockpit.receipt', {'command_id': wire['command_id'], 'result': result}, turn,
                   status='completed' if result['ok'] else 'failed')
        if turn:
            self.tool_result(turn, command['call'], result)
        else:
            self.emit('cockpit.result', domain=domain, command_id=wire['command_id'], result=result)
        self.advance_cockpit(domain, command)

    def advance_cockpit(self, domain, operation):
        active = self.cockpit_active.get(domain)
        if not active or active['generation'] != operation.get('generation'):
            return
        self.cockpit_active.pop(domain)
        pending = self.cockpit_deferred[domain]
        while pending:
            turn, call, name, arguments, trusted = pending.pop(0)
            if self.is_current(turn):
                self.launch_cockpit(turn, call, name, arguments, trusted_selection=trusted)
                break

    def cockpit_timeout(self, command_id):
        command = self.pending_cockpit.pop(command_id, None)
        if command is None:
            return
        result = self.cockpit_error('device_timeout')
        if command['wire']['kind'] == 'control':
            result.update(outcome_unknown=True, reason=feedback_text('errors.receipt_timeout'))
        turn = command['turn']
        self.trace('cockpit.receipt', {'command_id': command_id, 'result': result}, turn, status='timeout')
        if turn and self.is_current(turn): self.tool_result(turn, command['call'], result)
        elif not turn: self.emit('cockpit.result', domain=command['wire']['domain'], result=result)
        if command['wire']['kind'] == 'control': self.advance_cockpit(command['wire']['domain'], command)
        elif command.get('continuation'):
            self.advance_cockpit('media', command['continuation'])

    def accept_media_state(self, event):
        current, incoming = self.cockpit_state['media'], event.get('state')
        if not isinstance(incoming, dict) or event.get('state_version') != current['version'] or incoming.get('content_id') != current['content_id']:
            return
        if incoming.get('status') not in ('playing', 'paused', 'stopped', 'closed'):
            return
        updated = copy.deepcopy(current)
        for key in ('position_seconds', 'duration_seconds', 'volume'):
            value = incoming.get(key)
            if value is not None:
                if not finite(value) or value < 0 or key == 'volume' and value > 100: return
                updated[key] = value
        updated['status'] = incoming['status']
        if event.get('event') == 'ended' and incoming['status'] == incoming.get('desired_status') == 'stopped':
            updated['desired_status'] = 'stopped'
        updated['focus_reason'] = incoming.get('focus_reason') if incoming.get('focus_reason') == 'speech' else None
        self.cockpit_state['media'] = updated
        self.emit('cockpit.state', domain='media', state=copy.deepcopy(updated))

    async def close(self):
        await self.simulator.close()
        for turn in self.turns:
            self.cancel_audio(turn, 'session_closed')
        self.poller.cancel()
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(self.poller, *list(self.background), return_exceptions=True)
        self.runner.cancel()
        await asyncio.gather(self.runner, return_exceptions=True)
        await self.trace_queue.join()
        self.trace_writer.cancel()
        await asyncio.gather(self.trace_writer, return_exceptions=True)
        if self.owns_maps:
            await self.maps.aclose()
        if self.owns_web:
            await self.web.aclose()
        if self.owns_manual:
            await self.manual.aclose()
