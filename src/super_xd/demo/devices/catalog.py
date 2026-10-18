"""Session-scoped device transport envelopes for both queries and controls."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from super_xd.nlp.contracts.media import CockpitError
from super_xd.nlp.media_diagnostics import provider_diagnostic
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.nlp.textutil import is_snake_code


@dataclass(frozen=True)
class DeviceRequest:
    request_id: str
    session_id: str
    turn_id: str
    domain: str
    action: str
    arguments: dict
    kind: str
    target_device: str
    timeout_ms: int

    def to_wire(self):
        if self.kind not in ('query', 'control') or not self.request_id or self.timeout_ms <= 0:
            raise CockpitError('invalid_action', 'Invalid device request envelope')
        return {'command_id': self.request_id, 'request_id': self.request_id, 'session_id': self.session_id,
                'turn_id': self.turn_id, 'kind': self.kind, 'execution_location': 'device',
                'domain': self.domain, 'action': self.action, 'arguments': deepcopy(self.arguments),
                'target_device': self.target_device, 'timeout_ms': self.timeout_ms,
                'deadline_at': (datetime.now(timezone.utc) + timedelta(milliseconds=self.timeout_ms)).isoformat()}


def validate_device_receipt(request, receipt):
    for field in ('command_id', 'request_id', 'session_id', 'turn_id', 'target_device', 'kind'):
        if receipt.get(field) != request[field]:
            raise CockpitError('stale_receipt', 'Device receipt identity does not match the request')
    deadline = datetime.fromisoformat(request['deadline_at'])
    if datetime.now(timezone.utc) >= deadline:
        raise CockpitError('stale_receipt', 'Device receipt arrived after its deadline')
    if type(receipt.get('ok')) is not bool:
        raise CockpitError('device_failed', 'Device receipt requires a boolean result')
    if request['kind'] == 'query' and receipt['ok'] and not isinstance(receipt.get('result'), dict):
        raise CockpitError('device_failed', 'Device query requires a structured result')
    return receipt


def receipt_failure(request, receipt):
    """Only structured codes affect routing; device prose stays in diagnostics."""
    raw = receipt.get('error')
    raw = raw if isinstance(raw, dict) else {}
    code = raw.get('code')
    if not is_snake_code(code) or len(code) > 80:
        code = 'device_failed'
    retryable = raw.get('retryable')
    if type(retryable) is not bool:
        retryable = code in ('timeout', 'device_timeout', 'rate_limit', 'unavailable')
    if code in ('unsupported_capability', 'not_configured', 'invalid_request', 'invalid_action', 'invalid_response', 'auth', 'stale_command', 'stale_receipt'):
        retryable = False
    return ProviderError(code, 'Device operation failed', provider=request['target_device'], retryable=retryable,
                         details=provider_diagnostic({'request': request, 'receipt': receipt}))
