"""Deployment schemas include the actual delivery and continuation contracts."""
import json
from pathlib import Path
import subprocess
import sys

from scripts.export_gateway_contract import MODELS


def test_export_is_provider_free_and_covers_delivery_notices_and_continuation(tmp_path, monkeypatch):
    monkeypatch.setenv('SERVICES_CONFIG_PATH', str(tmp_path / 'missing.toml'))
    monkeypatch.setenv('SERVICE_REGION', 'intentionally-invalid-no-services-loaded')
    result = subprocess.run([sys.executable, '-m', 'scripts.export_gateway_contract', '--out', str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, text=True)
    assert 'Exported 14' in result.stdout
    documents = {path.name: json.loads(path.read_text()) for path in tmp_path.glob('*.schema.json')}
    assert set(documents) == {model.__name__ + '.schema.json' for model in MODELS}
    start = documents['StartRequest.schema.json']
    assert 'provider_metadata' in start['$defs']['AssistantRecord']['properties']
    plan = documents['ResumeRequest.schema.json']['$defs']['VehiclePlan']['properties']
    assert {'model_calls', 'loaded_skills', 'provenance', 'validation', 'action_ledger'} <= set(plan)
    assert not {'model_observations', 'load_evidence', 'trace_events'} & set(plan)
    snapshot = documents['TaskSnapshot.schema.json']
    assert snapshot['properties']['delivery_mode']['enum'] == ['foreground', 'background']
    delivery = documents['TaskDeliveryRequest.schema.json']
    assert set(delivery['required']) == {'conversation_id', 'task_id', 'expected_revision', 'idempotency_key'}
    assert documents['TaskNotice.schema.json']['properties']['expires_at']['format'] == 'date-time'
    assert documents['PresentationDecision.schema.json']['properties']['action']['enum'] == ['speak', 'defer', 'screen_only', 'drop']
