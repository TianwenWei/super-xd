"""Session/UI bridge to the same NLPService used by production and evaluation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from super_xd.nlp.catalog.vehicle import TOOLS
from super_xd.demo.tasks.database import task_card
from super_xd.demo.session.state import Turn
from super_xd.nlp.orchestration.tasks.service import TaskService, TaskMutationRequest, TaskPresentationRequest
from super_xd.nlp.contracts.run import RunIdentity, ToolObservation, ToolRequest
from super_xd.nlp.orchestration.gateway import NLPService
from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner
from super_xd.nlp.contracts.gateway_v1 import HistoryTurn, StartRequest
from super_xd.nlp.policy.product import ProductTalkerPolicy
from super_xd.nlp.orchestration.talker import talker_tools
from super_xd.nlp.orchestration.tasks.runner import TaskRunner
from super_xd.nlp.adapters.common import utcnow, sanitized_error
from super_xd.nlp.contracts.providers import ProviderError, redact
from super_xd.nlp.prompts.speech_text import speech_text
from super_xd.demo.gateway import LocalGateway
from super_xd.demo.resources.runtime import LocalResources, freeze_local_model, local_model_policy_id, local_task_budget
from super_xd.demo.tasks.ledger import SQLiteGatewayTaskStore


class ActorDiagnostics:
    def __init__(self, actor):
        self.actor = actor

    async def emit(self, record):
        await self.emit_many([record])

    async def emit_many(self, records):
        acknowledgement = asyncio.get_running_loop().create_future()
        await self.actor.post({'type': '_nlp_diagnostics', 'records': records, 'future': acknowledgement})
        await acknowledgement
        await self.actor.trace_queue.join()
        if self.actor.trace_error:
            raise RuntimeError('Local Gateway diagnostic commit failed')


class NLPBridge:
    def configure_nlp(self):
        self.resources = LocalResources(maps=self.maps, web=self.web, manual=self.manual, cockpit=self.cockpit)
        self.nlp_capabilities, bindings = self.resources.declarations()
        self.context_revision = getattr(self, 'context_revision', 0)
        model = freeze_local_model(self.ark)
        self.nlp_secrets = tuple(secret for provider in getattr(model, 'providers', {}).values()
                                 for secret in getattr(provider, 'secrets', ()) if secret)
        if isinstance(self.vehicle_planner, VehicleControlPlanner):
            self.vehicle_planner = VehicleControlPlanner(model)
        from super_xd.nlp.settings import NLPSettings
        foreground_wait_seconds = NLPSettings.from_environment().foreground_wait_seconds
        budget = local_task_budget()
        policy_id = local_model_policy_id(model, budget, foreground_wait_seconds=foreground_wait_seconds)
        task_service = TaskService(SQLiteGatewayTaskStore(self.db),
            lambda store: TaskRunner(store, model, self.web, trace_sink=store, weather=self.maps.weather,
                                    close_clients=False, **asdict(budget)),
            model_policy_id=policy_id, foreground_wait_seconds=foreground_wait_seconds)
        self.nlp = NLPService(model, model_policy_id=policy_id,
            semantic_policy=ProductTalkerPolicy(bindings,
                enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management']),
            diagnostics=ActorDiagnostics(self), planner=self.vehicle_planner, task_service=task_service)

    def nlp_start_request(self, turn):
        now = utcnow()
        state = deepcopy(self.vehicle)
        meta = state['_meta']
        history = []
        for previous in self.turns[-5:]:
            if previous is turn or not previous.messages:
                continue
            records = []
            pending = set()
            for record in previous.messages:
                if record.get('role') == 'system':
                    continue
                if record['role'] == 'assistant':
                    calls = [{"id": call['id'], "name": call['function']['name'],
                              "arguments": json.loads(call['function']['arguments']),
                              **({'extra_content': deepcopy(call['extra_content'])} if 'extra_content' in call else {})}
                             for call in record.get('tool_calls', [])]
                    pending.update(call['id'] for call in calls)
                    records.append({'role': 'assistant', 'content': record.get('content'), 'tool_calls': calls,
                        **({'provider_metadata': {'extra_content': deepcopy(record['extra_content'])}}
                           if 'extra_content' in record else {})})
                elif record['role'] == 'tool':
                    if record['tool_call_id'] in pending:
                        pending.remove(record['tool_call_id'])
                        records.append({'role': 'tool', 'tool_call_id': record['tool_call_id'],
                            'result': json.loads(record['content'])})
                else:
                    records.append({'role': 'user', 'content': record['content']})
            if not records:
                continue
            task = previous.source_task if previous.proactive else None
            if previous.proactive and not task:
                continue
            duration = self.audio_format().duration_ms(previous.audio_bytes)
            playback = ('not_started' if not previous.playback_id else
                        'completed' if previous.audio_finished and duration > 0 and previous.played_ms >= duration - 1 else
                        'partial' if previous.played_text else 'unknown')
            fields = {'turn_id': previous.id, 'speaker_seat': previous.speaker_seat or self.speaker_seat,
                'records': records, 'pending_tool_group': bool(pending), 'proactive': previous.proactive,
                'source_task': {key: task[key] for key in ('id', 'revision', 'status', 'goal')} if task else None,
                'interrupted_playback': bool(previous.playback_id and previous.audio_cancelled.is_set()),
                'heard_text': previous.played_text,
                'playback_status': playback}
            history.append(HistoryTurn.model_validate(fields))
        tasks = [{key: task[key] for key in ('id', 'revision', 'status', 'goal', 'summary', 'context', 'result', 'question') if key in task}
                 for task in self.task_list()]
        context = {'revision': self.context_revision, 'observed_at': now,
            'vehicle': {'revision': self.vehicle_version, 'observed_at': now, 'state': state},
            'capabilities': {'revision': 1, 'observed_at': now,
                'tools': [item.model_dump() for item in self.nlp_capabilities],
                'features': {name: True for name in TOOLS},
                'cockpit': self.cockpit.info() if self.cockpit else {'configured': False}},
            'environment': {'timezone': meta['timezone'], 'location':
                {'name': meta['location'], 'country': meta.get('location_country', ''), 'source': 'user_setting'}
                if meta.get('location') else None},
            'cockpit': deepcopy(self.cockpit_state) if self.cockpit else {}, 'tasks': tasks,
            'places': [{'place_ref': key, 'name': value['name'], 'provider': value['provider'],
                'source_turn_id': self.place_turns.get(key, 'previous'), 'presented_to_user': True,
                'details': deepcopy(value)} for key, value in self.places.items()],
            'media': [{key: item[key] for key in ('content_id', 'kind', 'title', 'provider')} |
                      {'source_turn_id': item.get('source_turn_id', 'previous'), 'presented_to_user': True}
                      for item in self.media_choices.values()]}
        return StartRequest(identity=RunIdentity(run_id=uuid4().hex, request_id=turn.client_request_id or uuid4().hex,
            turn_id=turn.id, context_revision=self.context_revision), conversation_id=self.id,
            received_at=now, user_text=turn.text, speaker_seat=turn.speaker_seat or self.speaker_seat,
            persona_id=turn.persona.id, history=history, context=context)

    def messages_for(self, turn):
        request = turn.start_request or self.nlp_start_request(turn)
        return self.nlp.compile_start(request).messages

    def tools_for(self, turn):
        return talker_tools()

    def launch_nlp(self, turn):
        turn.start_request = self.nlp_start_request(turn)

        async def execute(item, start):
            result = asyncio.get_running_loop().create_future()
            await self.post({'type': '_gateway_execute', 'turn': turn, 'item': item, 'future': result})
            return await result

        turn.gateway = LocalGateway(self.nlp, turn.start_request, execute)

        async def drive():
            try:
                async for event in turn.gateway.events():
                    await self.post({'type': '_nlp_event', 'turn': turn, 'event': event})
                await self.post({'type': '_nlp_closed', 'turn': turn})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.post({'type': '_nlp_error', 'turn': turn, 'error': sanitized_error(error)})
        turn.model_task = self.spawn(drive())

    def nlp_diagnostics(self, event):
        for record in event['records']:
            turn = next((item for item in reversed(self.turns) if item.id == record.identity.turn_id), None)
            if turn is None:
                continue
            if record.kind == 'observations.received':
                self.trace('observations.received',
                    {'observations': [item.model_dump() for item in record.observations]}, turn,
                    status='stale' if turn.interrupted else 'received')
                continue
            if record.kind == 'orchestration.event':
                row = record.data
                if row['kind'] == 'model.started':
                    turn.metrics['planner_model_calls'] = turn.metrics.get('planner_model_calls', 0) + 1
                self.trace(row['kind'], row['data'], turn, call_id=row.get('call_id'), status=row.get('status'))
                if row['kind'] == 'vehicle.plan.validated':
                    self.emit('vehicle.plan', turn_id=turn.id, plan=row['data'])
                continue
            if record.kind != 'run.event':
                continue
            item = record.event
            data = deepcopy(item.data)
            if item.type == 'model.started':
                turn.model_calls += 1
                turn.metrics['current_model_call_id'] = f'{record.identity.run_id}:model:{turn.model_calls}'
                turn.metrics['current_model_started_ms'] = (time.perf_counter() - turn.started) * 1000
                if turn.output and not turn.output.endswith('\n'):
                    self.append_text(turn, '\n', speak=False)
                data['model_call_index'] = data.get('model_call')
            elif item.type == 'model.completed':
                message = data['message']
                self.feed_sentences(turn, final=True)
                turn.messages.append(deepcopy(message))
                calls = message.get('tool_calls', [])
                if calls:
                    turn.group = deepcopy(calls)
                    turn.preface += speech_text(turn.model_text_pending)
                    names = {call['function']['name'] for call in calls}
                    turn.route = 'delegate' if 'delegate_task' in names else 'task' if 'manage_task' in names else 'tool'
                    for call in calls:
                        turn.call_metadata[call['id']] = {'name': call['function']['name'],
                            'arguments': json.loads(call['function']['arguments']),
                            'model_call_id': turn.metrics['current_model_call_id']}
                        self.trace('tool.started', {'name': call['function']['name'],
                            'arguments': call['function']['arguments'], 'model_call_index': turn.model_calls,
                            'model_call_id': turn.metrics['current_model_call_id']},
                            turn, call_id=call['id'], status='proposed')
                        if call['function']['name'] in TOOLS:
                            self.emit('vehicle.function_call', turn_id=turn.id, call=deepcopy(call))
                else:
                    turn.group = []
                    turn.metrics.setdefault('first_useful_ms', turn.metrics.get('current_model_first_text_ms'))
                turn.model_text_pending = ''
                turn.metrics.pop('current_model_first_text_ms', None)
                data['meta'] = data.pop('metadata', {})
                data['model_call_index'] = turn.model_calls
            elif item.type == 'policy.rejected':
                for identifier in data['tool_call_ids']:
                    turn.tool_results[identifier] = deepcopy(data['result'])
                    turn.messages.append({'role': 'tool', 'tool_call_id': identifier,
                        'content': json.dumps(data['result'], ensure_ascii=False)})
                turn.group = []
            model_id = turn.metrics.get('current_model_call_id')
            model_record = item.type.startswith('model.') or item.type == 'run.cancelled'
            if model_record:
                data['model_call_id'] = model_id
            self.trace(item.type, data, turn,
                call_id=model_id if model_record else None,
                status='stale' if turn.interrupted else 'cancelled' if item.type == 'run.cancelled' else 'completed')
        if not event['future'].done():
            event['future'].set_result(True)

    def accept_nlp_event(self, turn, event):
        if not self.is_current(turn):
            return
        if event.type == 'text.delta':
            elapsed = (time.perf_counter() - turn.started) * 1000
            turn.metrics.setdefault('server_first_model_text_ms', elapsed)
            turn.metrics.setdefault('current_model_first_text_ms', elapsed)
            turn.model_text_pending += event.text
            self.append_text(turn, event.text, speak=False)
            turn.sentence_buffer += event.text
            self.feed_sentences(turn)
        elif event.checkpoint is not None:
            turn.checkpoint = event.checkpoint
            turn.messages = self.nlp.turn_records(event.checkpoint)
            for group in event.checkpoint.orchestration.groups:
                for item in group.requests:
                    if item.request.name != 'delegate_vehicle_control':
                        continue
                    plan = group.plan or (group.completed_plans[-1].plan if group.completed_plans else None)
                    if plan is not None:
                        receipts = group.plan_receipts if group.plan is not None else group.completed_plans[-1].receipts
                        turn.vehicle_plans[item.request.tool_call_id] = {'plan': plan.model_dump(),
                            'action_receipts': [row.model_dump() for row in receipts]}
            for record in turn.messages:
                if record['role'] != 'tool':
                    continue
                result = json.loads(record['content'])
                turn.tool_results[record['tool_call_id']] = deepcopy(result)
                if record['tool_call_id'] not in turn.traced_tool_results:
                    meta = turn.call_metadata.get(record['tool_call_id'], {})
                    self.trace('tool.completed', {**meta, 'result': deepcopy(result)}, turn,
                        call_id=record['tool_call_id'], task_id=(result.get('task') or {}).get('id'),
                        status='completed' if result.get('ok') else 'failed')
                    turn.traced_tool_results.add(record['tool_call_id'])
                for task in result.get('tasks', []) or ([result['task']] if isinstance(result.get('task'), dict) else []):
                    previous = self.tasks.get(task['id'])
                    self.remember_task(task)
                    if previous != self.tasks.get(task['id']):
                        self.emit('ui.card', task=task_card(task), origin_turn_id=turn.id)
                    if (event.type == 'run.completed' and event.presentation is not None
                            and result.get('foreground_waited') is True
                            and task['status'] in {'completed', 'failed', 'cancelled', 'waiting_clarification'}):
                        self.notifications.delivered_in_turn(task)
            if event.presentation is not None:
                if event.type == 'run.completed' and event.presentation.source == 'verified_receipts':
                    turn.metrics['first_useful_ms'] = (time.perf_counter() - turn.started) * 1000
                turn.metrics['output_source'] = event.presentation.source
                self.append_text(turn, event.presentation.speech, source=event.presentation.source)
            # Failure speech and an unfinished model stream are audible, but
            # only an actual completed control can remain useful in a failed turn.
            if event.type == 'run.failed' or (event.type == 'run.completed'
                    and turn.metrics.get('first_useful_ms') is None):
                turn.metrics['first_useful_ms'] = turn.metrics.get('last_execution_receipt_ms')
            if event.type == 'run.suspended':
                self.emit('run.waiting', turn_id=turn.id, status=event.checkpoint.run.status)
            elif event.type == 'run.completed':
                self.finish(turn)
            elif event.type == 'run.failed':
                self.finish(turn, error=event.error_code)
            elif event.type == 'run.cancelled':
                self.finish(turn, error='cancelled')

    def execute_gateway_request(self, event):
        turn, item, future = event['turn'], event['item'], event['future']
        if not self.is_current(turn):
            future.cancel()
            return
        request, dispatch = item.request, item.dispatch
        call = {'id': request.tool_call_id, 'function': {'name': request.name,
                'arguments': json.dumps(request.arguments, ensure_ascii=False)}, 'type': 'function'}
        turn.pending_results[call['id']] = (future, request, dispatch.operation)
        arguments = deepcopy(dispatch.execution_arguments if dispatch.execution_arguments is not None else request.arguments)
        if request.name in TOOLS and dispatch.prepared_command is None:
            self.send_command(turn, call, request.name, arguments,
                keep_baseline=deepcopy(dispatch.execution_constraints.get('keep_baseline', {})))
        elif dispatch.execution_location == 'device' and dispatch.prepared_command is None:
            self.send_device_query(turn, call, dispatch.domain, arguments.get('action', request.name), arguments)
        elif dispatch.prepared_command is not None:
            domain = dispatch.prepared_command['domain']
            self.cockpit_generation += 1
            generation = self.cockpit_generation
            self.cockpit_latest[domain] = generation
            self.cockpit_active[domain] = {'turn': turn, 'call': call, 'generation': generation}
            async def prepare():
                try:
                    command = await self.resources.resolve_command(item)
                    outcome = {'command': command}
                except Exception as error:
                    outcome = {'result': {'ok': False, 'executed': False,
                        'code': getattr(error, 'code', 'provider_failed'), 'reason': sanitized_error(error)}}
                await self.post({'type': '_cockpit_prepared', 'turn': turn, 'call': call, 'name': request.name,
                    'domain': domain, 'action': dispatch.prepared_command['action'], 'arguments': arguments,
                    'generation': generation, 'version': dispatch.prepared_command['expected_state_version'], 'outcome': outcome})
            self.spawn(prepare())
        elif request.name == 'manage_navigation' and arguments.get('action') == 'get_status':
            state = deepcopy(self.cockpit_state['navigation'])
            self.tool_result(turn, call, {'ok': True, 'state': state, 'state_version': state['version']})
        else:
            turn.route = 'lookup'
            async def query():
                try:
                    async with asyncio.timeout(self.lookup_timeout):
                        result = await self.resources.query(item)
                except Exception as error:
                    result = {'ok': False, 'code': 'timeout' if isinstance(error, TimeoutError) else getattr(error, 'code', 'lookup_error'),
                              'reason': sanitized_error(error)}
                    if isinstance(error, ProviderError):
                        result.update(provider_error=redact(error.public()), diagnostics=redact(error.details))
                await self.post({'type': '_lookup_result', 'turn': turn, 'call': call, 'result': result})
            turn.lookup_tasks[call['id']] = self.spawn(query())

    def tool_result(self, turn, call, result):
        pending = turn.pending_results.pop(call['id'], None)
        if not self.is_current(turn):
            self.trace('tool.completed', {'name': call['function']['name'],
                'arguments': json.loads(call['function']['arguments']), 'result': deepcopy(result)},
                turn, call_id=call['id'], status='stale')
            if pending is not None and not pending[0].done():
                pending[0].cancel()
            return
        if pending is None:
            return
        future, request, operation = pending
        if request.name in TOOLS and isinstance(result.get('state'), dict) and '_meta' in result['state']:
            result = {**result, 'vehicle_state': deepcopy(result['state'])}
        self.trace('tool.completed', {'name': request.name, 'arguments': request.arguments, 'result': result},
                   turn, call_id=call['id'], status='completed' if result.get('ok') else 'failed')
        turn.traced_tool_results.add(call['id'])
        if (operation == 'control' and result.get('ok') is True and result.get('executed') is True
                and result.get('outcome_unknown') is not True):
            turn.metrics['last_execution_receipt_ms'] = (time.perf_counter() - turn.started) * 1000
        if isinstance(result.get('task'), dict):
            self.remember_task(result['task'])
            self.emit('ui.card', task=task_card(result['task']))
        if not future.done():
            status = 'unknown' if result.get('outcome_unknown') else 'completed'
            future.set_result(ToolObservation(identity=turn.start_request.identity,
                invocation_id=request.invocation_id, status=status, result=deepcopy(result)))

    def explicit_task(self, action, event):
        if action == 'get':
            task, error = self.resolve_task(event.get('task_id'))
            self.emit('ui.card', task=task) if task else self.emit('error', code='task_error', message=error['reason'])
            return
        task, error = self.resolve_task(event.get('task_id')) if action != 'create' else (None, None)
        if error:
            self.emit('error', code='task_error', message=error['reason'])
            return
        text = str(event.get('goal' if action == 'create' else 'text', '')).strip()
        if action == 'create' and not 1 <= len(text) <= 4000:
            raise ValueError('任务目标需为1–4000字')
        identity = uuid4().hex
        if action == 'create':
            turn = Turn(identity[:12], text, self.persona.model_copy(deep=True), self.persona_version)
            turn.speaker_seat = self.speaker_seat
            start = self.nlp_start_request(turn)
            request = ToolRequest(invocation_id=identity, tool_call_id=identity, name='delegate_task',
                                  arguments={'goal': text}, idempotency_key=identity)
        async def mutate():
            try:
                if action == 'create':
                    result = await self.nlp.task_service.handle(start, request, manual_registration=True)
                    changed = result['task']
                else:
                    changed = await self.nlp.mutate_task(TaskMutationRequest(conversation_id=self.id,
                        task_id=task['id'], expected_revision=event.get('expected_revision', task['revision']),
                        action=action, text=text, idempotency_key=identity))
                outcome = {'task': changed}
            except Exception as failure:
                outcome = {'error': sanitized_error(failure)}
            await self.post({'type': '_task_write', 'turn': None, 'call': None, 'meta': {'kind': action}, **outcome})
        self.spawn(mutate())

    def present_task(self, task, *, notice):
        request = TaskPresentationRequest(conversation_id=self.id, task_id=task['id'],
            revision=task['revision'], expected_status=task['status'], persona_id=self.persona.id)
        async def present():
            try:
                result = await self.nlp.present_task(request)
                outcome = {'presentation': result}
            except Exception as error:
                outcome = {'error': sanitized_error(error)}
            await self.post({'type': '_task_presented', 'task': task, 'notice': notice, **outcome})
        self.spawn(present())

    def accept_task_presentation(self, event):
        task, result = event['task'], event.get('presentation')
        notice = event['notice']
        if not self.notifications.is_pending(notice):
            # Foreground delivery or a newer event may have consumed this
            # notice while its presentation request was still in flight.
            return
        if notice.expires_at <= datetime.now(timezone.utc):
            self.notifications.acknowledge(notice)
            return
        current = self.tasks.get(task['id'])
        if (not current or current['revision'] != task['revision'] or
                current['status'] != task['status']):
            self.notifications.acknowledge(notice)
            return
        if result is None:
            self.trace('task.presentation.failed', {'error': event.get('error')}, task_id=task['id'], status='failed')
            delay = self.notifications.retry(notice, failed=True)
            if delay is not None:
                self.schedule_announce(delay=delay)
            return
        if result.stale:
            self.notifications.acknowledge(notice)
            return
        if (self.generating or self.speaking or self.asr_capture_id or not self.clients
                or self.speaking_context.in_call or self.speaking_context.demanding_driving):
            self.notifications.retry(notice)
            self.schedule_announce()
            return
        self.notifications.acknowledge(notice)
        turn = Turn(uuid4().hex[:12], '', self.persona.model_copy(deep=True), self.persona_version)
        turn.proactive, turn.task_id, turn.speaker_seat = True, task['id'], self.speaker_seat
        turn.source_task = {key: deepcopy(task[key]) for key in ('id', 'revision', 'status', 'goal')}
        turn.messages = [{'role': 'assistant', 'content': result.speech}]
        self.current = turn
        self.turns.append(turn)
        self.trace('turn.started', {'input': '', 'proactive': True, 'persona_version': turn.persona_version}, turn, status='running')
        self.emit('turn.started', turn_id=turn.id, text='', persona_version=turn.persona_version, proactive=True, task_id=task['id'])
        self.emit('ui.card', task={**task_card(current), **(result.card or {})})
        self.start_audio(turn)
        self.append_text(turn, result.speech, source='task_presentation')
        self.finish(turn)
