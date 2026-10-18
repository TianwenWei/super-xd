"""Redact executable media URLs without importing media clients."""
from super_xd.nlp.contracts.providers import redact
from super_xd.nlp.textutil import find_urls


def provider_diagnostic(value, secrets=()):
    """Playback URLs are capabilities, including URLs echoed inside raw errors."""
    safe = redact(value, secrets)
    if isinstance(safe, dict):
        return {key: ('[redacted playback capability]' if str(key).lower() in ('source_url', 'url', 'playback_url', 'stream_url', 'download_url')
                      else provider_diagnostic(item, secrets)) for key, item in safe.items()}
    if isinstance(safe, list):
        return [provider_diagnostic(item, secrets) for item in safe]
    if isinstance(safe, str):
        for url in find_urls(safe):
            safe = safe.replace(url, '[redacted playback capability]')
    return safe
