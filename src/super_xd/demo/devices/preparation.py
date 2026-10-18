"""Resolve Demo media resources before preparing a device command."""
from copy import deepcopy
from super_xd.nlp.contracts.media import CockpitError, media_item
from super_xd.nlp.policy.cockpit import _media_selection, prepare_media_action


async def prepare_media(services, action, arguments, state, choices, *, trusted_selection=False):
    """Resolve a sourced catalog reference, then use the shared pure state rules."""
    resolved, provider_trace, device_content_id = None, None, None
    if action in ('play', 'next', 'previous'):
        known = {item['content_id']: item for item in state.get('queue', [])}
        known.update(choices)
        identifier = _media_selection(action, arguments, state, known, trusted_selection=trusted_selection)
        info = services.media.content_provider_info(identifier) if hasattr(services.media, 'content_provider_info') else services.media.info()
        if info.get('execution_location') == 'device':
            if identifier not in known:
                raise CockpitError('unknown_content', 'Device content must first be resolved by this session')
            resolved = media_item(known[identifier])
            if hasattr(services.media, 'device_content_id'):
                device_content_id = services.media.device_content_id(identifier)
        else:
            value = await services.media.resolve(identifier)
            provider_trace = deepcopy(value.get('_provider_trace'))
            resolved = media_item(value, playable=True)
            if resolved['content_id'] != identifier:
                raise CockpitError('invalid_content', 'Resolved content ID does not match its catalog reference')
    command = prepare_media_action(action, arguments, state, choices, resolved_content=resolved, require_provider=False)
    command.pop('requires_content_resolution')
    if resolved is not None and 'source_url' in resolved:
        command['arguments']['source_url'] = resolved['source_url']
    if device_content_id is not None:
        command['arguments']['device_content_id'] = device_content_id
    command['provider_trace'] = provider_trace
    return command
