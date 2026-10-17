"""Per-turn HTTP audio cancellation/late-result guard with injected synthesis."""
import asyncio
import httpx
import pytest
from backend.app import app, actors
from backend.db import Database
from backend.session import SessionActor


class Ark:
    async def stream(self, messages, tools, **options):
        yield {'type':'delta','text':'当前轮次的一句话。'}
        yield {'type':'message','message':{'role':'assistant','content':'当前轮次的一句话。'}}


class FakeTTS:
    def __init__(self, blocked=False):
        self.blocked=blocked;self.started=asyncio.Event();self.cancelled=False
    async def synthesize_with_metrics(self, text):
        self.started.set()
        try:
            if self.blocked: await asyncio.Event().wait()
            return {'audio':b'ID3testMP3','first_packet_ms':12.5,'total_ms':25.5}
        except asyncio.CancelledError:
            self.cancelled=True
            raise


@pytest.fixture
async def context(tmp_path):
    actor=SessionActor('audio-session',Database(tmp_path/'tasks.sqlite3'),Ark())
    actors[actor.id]=actor
    q=await actor.attach();await q.get()
    await actor.post({'type':'input','text':'说一句话'})
    while (await q.get())['type']!='stream.done': pass
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        yield actor,client
    actors.pop(actor.id,None)
    await actor.close()


async def test_audio_response_has_turn_id_and_separate_metrics(context):
    actor,client=context;app.state.tts=FakeTTS()
    response=await client.post('/api/sessions/audio-session/tts',json={'turn_id':actor.current.id,'text':actor.current.output})
    assert response.status_code==200 and response.content.startswith(b'ID3')
    assert response.headers['x-turn-id']==actor.current.id
    assert response.headers['x-tts-first-packet-ms']=='12.5'
    assert response.headers['cache-control']=='no-store'


async def test_new_input_cancels_synthesis_and_no_late_audio(context):
    actor,client=context;app.state.tts=FakeTTS(blocked=True)
    old_id=actor.current.id
    request=asyncio.create_task(client.post('/api/sessions/audio-session/tts',json={'turn_id':old_id,'text':actor.current.output}))
    await app.state.tts.started.wait()
    await actor.post({'type':'input','text':'新消息'})
    response=await request
    assert response.status_code==409 and app.state.tts.cancelled
    late=await client.post('/api/sessions/audio-session/tts',json={'turn_id':old_id,'text':'当前轮次的一句话。'})
    assert late.status_code==409


async def test_cannot_synthesize_arbitrary_or_unfinished_text(context):
    actor,client=context;app.state.tts=FakeTTS()
    response=await client.post('/api/sessions/audio-session/tts',json={'turn_id':actor.current.id,'text':'伪造文本'})
    assert response.status_code==400 and not app.state.tts.started.is_set()


async def test_missing_speech_credentials_is_explicit_without_fallback(context, monkeypatch):
    from backend.tts import VolcanoTTS
    actor,client=context
    monkeypatch.delenv('VOLCENGINE_SPEECH_CONFIG_FILE', raising=False)
    tts=VolcanoTTS(api_key='',app_id='',access_token='')
    app.state.tts=tts
    try:
        response=await client.post('/api/sessions/audio-session/tts',json={'turn_id':actor.current.id,'text':actor.current.output})
        assert response.status_code==503
        assert 'VOLCENGINE_SPEECH_API_KEY' in response.json()['detail']
    finally:await tts.aclose()


async def test_explicit_stop_cancels_server_synthesis(context):
    actor,client=context;app.state.tts=FakeTTS(blocked=True)
    turn_id=actor.current.id
    request=asyncio.create_task(client.post('/api/sessions/audio-session/tts',json={'turn_id':turn_id,'text':actor.current.output}))
    await app.state.tts.started.wait()
    await actor.post({'type':'audio.cancel','turn_id':turn_id})
    response=await request
    assert response.status_code==409 and app.state.tts.cancelled
