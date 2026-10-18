"""Versioned evidence ledger values and structural constraints."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Literal, Self
from pydantic import Field, field_validator, model_validator
from super_xd.nlp.contracts.run import Contract


RETRIEVAL_TOOLS = frozenset({'search_manual', 'web_search', 'get_weather', 'search_places',
                             'search_media', 'search_web', 'extract_web', 'lookup_web',
                             'get_weather_forecast'})


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class EvidenceScope(Contract):
    kind: Literal['manual_excerpt', 'search_snippet', 'page_content', 'weather', 'catalog', 'unknown'] = 'unknown'
    coverage: Literal['partial', 'complete', 'unknown'] = 'unknown'
    granularity: Literal['current', 'hourly', 'daily', 'unknown'] = 'unknown'
    subject: str | None = None
    date: str | None = None
    timezone: str | None = None
    interval: dict[str, str] = Field(default_factory=dict)
    qualification: str | None = None


class EvidenceReference(Contract):
    source_id: str = Field(min_length=1, max_length=4000)
    version: str = Field(min_length=1, max_length=500)
    content_sha256: str
    scope: EvidenceScope

    @field_validator('content_sha256')
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
            raise ValueError('Evidence content fingerprint must be SHA-256')
        return value

    @property
    def reference_id(self) -> str:
        return 'ev-' + _hash([self.source_id, self.version, self.content_sha256])


class RetrievalRecord(Contract):
    invocation_id: str = Field(min_length=1, max_length=300)
    tool_name: str
    receipt_sha256: str
    references: list[EvidenceReference] = Field(default_factory=list, max_length=128)

    @field_validator('tool_name')
    @classmethod
    def valid_tool(cls, value: str) -> str:
        if value not in RETRIEVAL_TOOLS:
            raise ValueError('Not a retrieval tool')
        return value

    @field_validator('receipt_sha256')
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        return EvidenceReference.valid_hash(value)


class RetrievalProgress(Contract):
    version: Literal[1] = 1
    records: list[RetrievalRecord] = Field(default_factory=list, max_length=128)

    @model_validator(mode='after')
    def unique_invocations(self) -> Self:
        ids = [record.invocation_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError('Retrieval invocation IDs must be unique')
        return self


class RetrievalLimits(Contract):
    max_calls_per_tool: int = Field(default=4, ge=1, le=128)
    max_no_new_evidence: int = Field(default=1, ge=1, le=128)
    max_total_calls: int = Field(default=8, ge=1, le=128)


class EvidenceReport(Contract):
    tool_name: str
    calls: int = Field(ge=0)
    total_calls: int = Field(ge=0)
    new_evidence_count: int = Field(ge=0)
    consecutive_no_new_evidence: int = Field(ge=0)
    evidence_ids: list[str]
    support_assessed: Literal[False] = False
