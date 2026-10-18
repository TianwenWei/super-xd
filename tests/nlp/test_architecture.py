"""Keep layer direction enforceable as new modules and imports are added."""
import subprocess
import sys

import pytest

from scripts.check_import_boundaries import check_static_boundaries


@pytest.mark.parametrize('layer,target', [
    ('contracts', 'super_xd.nlp.prompts.templates'),
    ('catalog', 'super_xd.nlp.orchestration.gateway'),
    ('engine', 'super_xd.nlp.adapters.models.service'),
    ('policy', 'super_xd.nlp.adapters.reads.services'),
    ('orchestration', 'super_xd.demo.tasks.database'),
    ('policy', 'httpx'),
])
def test_guard_rejects_dependencies_even_in_dormant_branches(tmp_path, layer, target):
    folder = tmp_path / 'nlp' / layer
    folder.mkdir(parents=True)
    (folder / 'future_module.py').write_text(f'if False:\n    import {target}\n')
    with pytest.raises(AssertionError):
        check_static_boundaries(tmp_path)


def test_current_production_layers_obey_the_same_guard():
    check_static_boundaries()


def test_gateway_contracts_are_importable_without_prompts_or_runtime():
    code = '''
import sys
class DataOnly:
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('super_xd.demo', 'super_xd.nlp.prompts', 'super_xd.nlp.engine',
                   'super_xd.nlp.orchestration', 'super_xd.nlp.adapters')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Contract import reached ' + fullname)
sys.meta_path.insert(0, DataOnly())
from super_xd.nlp.contracts.gateway_v1 import StartRequest
from super_xd.nlp.contracts.tasks import TaskExecutionRequest
assert StartRequest.model_json_schema()['properties']
assert TaskExecutionRequest.model_json_schema()['properties']
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
