"""Device-side authority, separate from the Talker's vehicle snapshot."""
from backend.speech_config import feedback_text
import asyncio
from copy import deepcopy
import math
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.catalog import execute_tool
from backend.harness.vehicle_planner import validate_vehicle_action_keep, _value, _canonical


class VehicleSimulator:
    def __init__(self, state, *, execution_ms=300, network_rtt_ms=400):
        self.state = deepcopy(state)
        self.execution_ms, self.network_rtt_ms = execution_ms, network_rtt_ms
        self.version = 0
        self.commands, self.receipts, self.jobs = {}, {}, {}
        self.invalid_turns = set()
        self.expired_commands = set()

    def register(self, command, *, keep=None, place=None):
        self.commands[command['command_id']] = {'wire': deepcopy(command),
            'keep': deepcopy(keep or {}), 'place': deepcopy(place)}

    def invalidate(self, turn_id):
        self.invalid_turns.add(turn_id)

    def expire(self, command_id):
        self.expired_commands.add(command_id)

    def configure(self, settings):
        allowed = {'speed_kph', 'gear', 'call_active', 'navigation_active', 'timezone', 'location', 'location_country'}
        if not isinstance(settings, dict) or not set(settings) <= allowed:
            raise ValueError('未知模拟器设置')
        state = deepcopy(self.state)
        for key, value in settings.items():
            if key == 'speed_kph' and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 200):
                raise ValueError('车速需为0–200 km/h')
            if key == 'gear' and value not in ('P', 'R', 'N', 'D'):
                raise ValueError('无效档位')
            if key in ('call_active', 'navigation_active') and type(value) is not bool:
                raise ValueError('状态需为boolean')
            if key == 'timezone':
                if not isinstance(value, str):
                    raise ValueError('无效时区')
                ZoneInfo(value)
            if key in ('location', 'location_country'):
                if not isinstance(value, str) or len(value) > 200:
                    raise ValueError('地点设置需为不超过200字的文本。')
                value = value.strip()
            if key == 'navigation_active':
                state['navigation']['active'] = value
            else:
                state['_meta'][key] = value
        if state['_meta']['speed_kph'] > 0 and state['_meta']['gear'] == 'P':
            raise ValueError('P档车速必须为0，请先切换档位')
        self.state, self.version = state, self.version + 1
        return deepcopy(state)

    async def execute(self, command_id):
        if command_id not in self.commands:
            raise ValueError('未知车辆命令')
        if command_id not in self.jobs:
            self.jobs[command_id] = asyncio.create_task(self._execute(command_id))
        # A client disconnect must not repeat or cancel an already accepted action.
        return deepcopy(await asyncio.shield(self.jobs[command_id]))

    async def _execute(self, command_id):
        started = time.perf_counter()
        await asyncio.sleep(self.network_rtt_ms / 2000)
        arrived = time.perf_counter()
        await asyncio.sleep(self.execution_ms / 1000)
        entry = self.commands[command_id]
        wire, keep = entry['wire'], entry['keep']
        result = {'ok': False, 'executed': False, 'name': wire['name'],
                  'arguments': deepcopy(wire['arguments']), 'resume_condition': None}
        if wire['turn_id'] in self.invalid_turns or command_id in self.expired_commands:
            result.update(reason_code='stale_command', reason=feedback_text('simulator.stale_command'))
        else:
            try:
                if any(_canonical(_value(self.state, path)) != _canonical(value) for path, value in keep.items()):
                    raise ValueError(feedback_text('simulator.keep_changed'))
                validate_vehicle_action_keep(wire['name'], wire['arguments'], self.state, list(keep))
                candidate = deepcopy(self.state)
                outcome = execute_tool(wire['name'], wire['arguments'], candidate)
                if outcome['ok'] and any(_canonical(_value(candidate, path)) != _canonical(value) for path, value in keep.items()):
                    raise ValueError(feedback_text('simulator.keep_mutation'))
                result.update(outcome, executed=outcome['ok'])
                if outcome['ok']:
                    if entry['place']:
                        candidate['navigation']['destination_place'] = entry['place']
                    self.state = candidate
                    self.version += 1
            except (ValueError, KeyError, TypeError) as exc:
                result.update(reason_code='keep_conflict', reason=str(exc))
        result.update(command_id=command_id, turn_id=wire['turn_id'], receipt_id=uuid4().hex,
                      state=deepcopy(self.state), state_version=self.version)
        result.setdefault('reason_code', 'ok' if result['ok'] else 'vehicle_rejected')
        result.setdefault('resume_condition', None)
        executed = time.perf_counter()
        await asyncio.sleep(self.network_rtt_ms / 2000)
        result['timings'] = {'outbound_ms': (arrived-started)*1000, 'execution_ms': (executed-arrived)*1000,
                             'inbound_ms': (time.perf_counter()-executed)*1000,
                             'total_ms': (time.perf_counter()-started)*1000}
        self.receipts[command_id] = deepcopy(result)
        return result

    def receipt(self, command_id, receipt_id):
        receipt = self.receipts.get(command_id)
        if not receipt or receipt['receipt_id'] != receipt_id:
            raise ValueError('回执未通过车端绑定校验')
        return deepcopy(receipt)

    async def close(self):
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
