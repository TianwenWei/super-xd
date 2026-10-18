"""Mutable turn state, written only by the session actor event handlers."""
import asyncio
import time
from dataclasses import dataclass, field
from super_xd.nlp.contracts.catalog import PersonaConfig


@dataclass
class Turn:
    id: str
    text: str
    persona: PersonaConfig
    persona_version: int
    started: float = field(default_factory=time.perf_counter)
    messages: list = field(default_factory=list)
    output: str = ''
    first_effective_ms: float | None = None
    model_calls: int = 0
    commands: list = field(default_factory=list)
    pending_results: dict = field(default_factory=dict)
    group: list = field(default_factory=list)
    tool_results: dict = field(default_factory=dict)
    call_metadata: dict = field(default_factory=dict)
    traced_tool_results: set = field(default_factory=set)
    finished: bool = False
    model_task: object = None
    route: str = 'chat'
    audio_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    metrics: dict = field(default_factory=dict)
    proactive: bool = False
    task_id: str | None = None
    source_task: dict | None = None
    interrupted: bool = False
    client_request_id: str | None = None
    speaker_seat: str | None = None
    playback_id: str | None = None
    audio_queue: object = None
    audio_task: object = None
    audio_chunks: int = 0
    audio_bytes: int = 0
    audio_input_closed: bool = False
    model_text_pending: str = ''
    spoken_parts: list = field(default_factory=list)
    sentences: list = field(default_factory=list)
    played_ms: float = 0
    played_text: str = ''
    audio_finished: bool = False
    preface: str = ''  # What the model said it is doing (progressive/future tense), already spoken
    first_clause_fed: bool = False
    sentence_buffer: str = ''
    vehicle_plans: dict = field(default_factory=dict)
    lookup_tasks: dict = field(default_factory=dict)
    lookup_results: list = field(default_factory=list)
    start_request: object = None
    gateway: object = None
    checkpoint: object = None
