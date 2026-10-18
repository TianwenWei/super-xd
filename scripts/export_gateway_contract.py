"""Export deployable JSON Schemas without constructing providers or loading .env."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from jsonschema import Draft202012Validator

from super_xd.nlp.contracts.tasks import TaskDeliveryRequest, TaskExecutionRequest, TaskExecutionResult, TaskMutationRequest, TaskPresentationRequest, TaskPresentationResult, TaskSnapshot
from super_xd.nlp.contracts.gateway_v1 import GatewayOutput, ResumeRequest, StartRequest
from super_xd.nlp.contracts.gateway_v1 import CancelRequest
from super_xd.nlp.contracts.events import PresentationDecision, SpeakingContext, TaskNotice

MODELS = (StartRequest, ResumeRequest, GatewayOutput, CancelRequest, TaskExecutionRequest,
          TaskExecutionResult, TaskMutationRequest, TaskPresentationRequest, TaskPresentationResult,
          TaskDeliveryRequest, TaskSnapshot, TaskNotice, SpeakingContext, PresentationDecision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='Directory for standalone JSON Schema documents')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        document = {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()}
        Draft202012Validator.check_schema(document)
        (args.out / f'{model.__name__}.schema.json').write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Exported {len(MODELS)} Gateway V1 schemas to {args.out}')


if __name__ == '__main__':
    main()
