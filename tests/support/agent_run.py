"""Shared explicit fixtures for agent run tests."""

import json


from super_xd.nlp.contracts.run import PreparedRun, RunIdentity, ToolObservation


def prepared(**changes):
    values = dict(
        identity=RunIdentity(
            run_id="run", request_id="request", turn_id="turn", context_revision=3
        ),
        messages=[{"role": "user", "content": "explicit offline input"}],
        release_id="release-a",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
    )
    values.update(changes)
    return PreparedRun(**values)


def call(city="Paris", identifier="call"):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": "lookup", "arguments": json.dumps({"city": city})},
    }


class ScriptedModel:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
        self.requests.append({"messages": messages, "tools": tools, "options": options})
        response = self.responses.pop(0)
        if response.get("content"):
            yield {"type": "delta", "text": response["content"]}
        yield {
            "type": "message",
            "message": {"role": "assistant", **response},
            "meta": {"fixture": True},
        }


async def collect(run, observations=()):
    return [event async for event in run.advance(observations)]


def observation(run, request, status="completed", **result):
    return ToolObservation(
        identity=run.prepared.identity,
        invocation_id=request.invocation_id,
        status=status,
        result=result or {"ok": True, "value": "verified fixture"},
    )
