"""Shared media values and validation; no provider or device implementation."""
from copy import deepcopy
import math
from urllib.parse import urlparse

KINDS = ('music', 'video', 'radio')


class CockpitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code



def media_item(value: object, *, playable: bool = False) -> dict[str, object]:
    if not isinstance(value, dict) or value.get('kind') not in KINDS:
        raise CockpitError('invalid_content', 'Media item kind is invalid')
    identifier, title = value.get('content_id'), value.get('title')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 500:
        raise CockpitError('invalid_content', 'Media content_id is invalid')
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise CockpitError('invalid_content', 'Media title is invalid')
    for field in ('artist', 'album', 'provider', 'mime_type'):
        if field in value and (not isinstance(value[field], str) or len(value[field]) > 500):
            raise CockpitError('invalid_content', 'Media descriptive metadata is invalid')
    duration = value.get('duration_seconds')
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
        raise CockpitError('invalid_content', 'Media duration is invalid')
    result = {key: deepcopy(value[key]) for key in ('content_id', 'kind', 'title', 'artist', 'album', 'provider', 'mime_type', 'duration_seconds') if key in value}
    if playable:
        url = value.get('source_url')
        parsed = urlparse(url) if isinstance(url, str) else None
        local = (isinstance(url, str) and parsed is not None and url.startswith('/api/media/') and not url.startswith('//')
                 and not any(part in ('.', '..') for part in parsed.path.split('/')) and '\\' not in url)
        remote = parsed and parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
        if not (local or remote):
            raise CockpitError('invalid_source', 'Media source must be a configured HTTP URL or local media endpoint')
        result['source_url'] = url
    return result
