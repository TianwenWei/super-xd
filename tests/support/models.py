"""Typed model fixtures shared by production-engine and Demo tests."""
from copy import deepcopy
from super_xd.nlp.contracts.providers import ModelResult


def scripted_result(message):
    if isinstance(message, ModelResult):
        return deepcopy(message)
    if not isinstance(message, dict):
        return message
    message = deepcopy(message)
    metadata = message.pop('_meta', {})
    return ModelResult(message, metadata)
