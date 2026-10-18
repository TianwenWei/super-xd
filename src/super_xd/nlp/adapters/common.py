"""Transport-independent timestamps and safe external-service diagnostics."""
import os
from datetime import datetime, timezone
import httpx
from super_xd.nlp.textutil import redact as redact_text, remove_urls


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def sanitized_error(exc, secrets=()):
    if isinstance(exc, httpx.HTTPStatusError):
        code = ''
        try:
            error = exc.response.json().get('error', {})
            code = error.get('code', '') if isinstance(error, dict) else ''
        except Exception:
            pass
        value = f'HTTP {exc.response.status_code}' + (f' ({code})' if code else '')
    elif isinstance(exc, httpx.TimeoutException):
        return 'network timeout'
    else:
        value = str(exc)
    for key in ('ARK_API_KEY', 'ARK_EMBEDDING_API_KEY', 'TAVILY_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_MAPS_API_KEY',
                'VOLCENGINE_SPEECH_API_KEY', 'VOLCENGINE_SPEECH_ACCESS_TOKEN'):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, '[redacted]')
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[redacted]')
    value = remove_urls(redact_text(value, secrets))[0]
    return value[:240] or type(exc).__name__
