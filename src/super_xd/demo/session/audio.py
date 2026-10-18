"""Audio IO emits actor events without modifying session or turn state."""
import asyncio
from contextlib import aclosing

from super_xd.nlp.adapters.common import sanitized_error
from super_xd.nlp.contracts.providers import ProviderError, redact


async def synthesize_audio(provider, queue, voice, turn, post):
    async def texts():
        while True:
            text = await queue.get()
            if text is None:
                return
            yield text

    try:
        async with aclosing(provider.stream_synthesize(texts(), voice=voice)) as stream:
            async for item in stream:
                await post({'type': '_audio_item', 'turn': turn, 'item': item})
        await post({'type': '_audio_finished', 'turn': turn})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        await post({'type': '_audio_error', 'turn': turn, 'error': sanitized_error(error),
                    'provider_error': redact(error.public()) if isinstance(error, ProviderError) else None,
                    'diagnostics': redact(error.details) if isinstance(error, ProviderError) else None})
