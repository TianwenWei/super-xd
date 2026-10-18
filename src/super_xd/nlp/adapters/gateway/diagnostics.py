"""Gateway diagnostic transport; the engine only owns redaction."""
import hashlib
import json
from super_xd.nlp.diagnostics import safe_diagnostic
from super_xd.nlp.adapters.gateway.tasks import Acknowledgement

class GatewayDiagnostics:
    """Gateway atomically acknowledges ordered batches; no local durable state."""
    def __init__(self, transport):
        self.transport = transport

    async def emit(self, record):
        await self.emit_many([record])

    async def emit_many(self, records):
        if not records:
            return
        values = [safe_diagnostic(record.model_dump(), self.transport.secrets) for record in records]
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        key = 'diagnostics-' + hashlib.sha256(encoded.encode()).hexdigest()
        await self.transport.request('/internal/nlp/diagnostics',
                                     {'records': values, 'idempotency_key': key},
                                     Acknowledgement)
