"""Redacted diagnostics independent of conversation storage or model clients."""
from __future__ import annotations

import json
import hashlib
from backend.services.contracts import redact
from backend.textutil import redact_bearer, redact_known_secrets
from backend.cockpit.media import provider_diagnostic


def safe_diagnostic(value, secrets=(), *, media=False):
    """Preserve research provenance while removing executable media URLs."""
    if isinstance(value, dict):
        name, kind = value.get('name'), value.get('kind')
        media = media or (isinstance(name, str) and name in {'search_media', 'control_media'}) or (
            isinstance(kind, str) and kind in {'media', 'music', 'video', 'radio'}) or 'content_id' in value
        sensitive = {'authorization', 'proxyauthorization', 'apikey', 'accesskey', 'accesstoken',
                     'appsecret', 'clientsecret', 'xapikey', 'xapiaccesskey', 'xgoogapikey',
                     'googlemapsapikey', 'leasetoken', 'token', 'secret', 'password'}
        result = {}
        for key, item in value.items():
            normalized = ''.join(char for char in str(key) if char not in '-_' and not char.isspace()).lower()
            if normalized in sensitive:
                result[key] = '[redacted]'
            elif str(key).lower() in {'playback_url', 'stream_url', 'download_url'} or (
                    media and str(key).lower() in {'source_url', 'url'}):
                result[key] = '[redacted playback capability]'
            else:
                result[key] = safe_diagnostic(item, secrets, media=media)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_diagnostic(item, secrets, media=media) for item in value]
    if isinstance(value, str):
        value = redact_bearer(redact_known_secrets(redact(value, secrets)))
        if value.lstrip().startswith(('{', '[')):
            try:
                return json.dumps(safe_diagnostic(json.loads(value), secrets, media=media), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return provider_diagnostic(value, secrets) if media else value
    return value


class GatewayDiagnostics:
    """Gateway atomically acknowledges ordered batches; no local durable state."""
    def __init__(self, transport):
        self.transport = transport

    async def emit(self, record):
        await self.emit_many([record])

    async def emit_many(self, records):
        from .task_transport import Acknowledgement
        if not records:
            return
        values = [safe_diagnostic(record.model_dump(), self.transport.secrets) for record in records]
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        key = 'diagnostics-' + hashlib.sha256(encoded.encode()).hexdigest()
        await self.transport.request('/internal/nlp/diagnostics',
                                     {'records': values, 'idempotency_key': key},
                                     Acknowledgement)
