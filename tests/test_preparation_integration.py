import asyncio
from io import BytesIO
import json
import httpx
from fastapi import UploadFile
from fastapi.testclient import TestClient
from preparation.service import PreparationService, config_path
from preparation.web_settings import create_settings_plugin
from plugin_settings import SettingsContext

def test_plugin_descriptor_matches_existing_ui_contract(tmp_path):
    plugin = create_settings_plugin(SettingsContext(str(tmp_path), str(tmp_path), lambda: False))
    assert plugin.describe()['settings']['type'] == 'file_import'
    assert plugin.describe()['settings']['accept'] == '.json'

def test_config_import_is_atomic_and_active_suite_locked(tmp_path, monkeypatch):
    monkeypatch.setenv('MINIAPP_PILOT_DATA_DIR', str(tmp_path))
    busy = [False]
    plugin = create_settings_plugin(SettingsContext(str(tmp_path), str(tmp_path), lambda: busy[0]))
    endpoint = plugin.router.routes[0].endpoint
    def upload(value):
        return asyncio.run(endpoint(UploadFile(filename='config.json', file=BytesIO(value))))
    assert upload(b'{"actions":{},"checks":{}}')['ok']
    before = config_path().read_bytes()
    assert upload(b'{"unknown": 1}').status_code == 400
    assert config_path().read_bytes() == before
    busy[0] = True
    assert upload(b'{"actions":{}}').status_code == 409
    assert config_path().read_bytes() == before

def test_example_service_round_trip():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'examples/mock_service.py'
    spec = importlib.util.spec_from_file_location('demo_service', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as server:
        def route(request):
            response = server.request(request.method, request.url.path, content=request.content,
                                      headers={'Content-Type': 'application/json'})
            return httpx.Response(response.status_code, content=response.content)
        config = json.loads((path.parent/'preparation.json').read_text())
        service = PreparationService(config, client=httpx.Client(transport=httpx.MockTransport(route)))
        assert service.check_environment('demo_health')['status'] == 'success'
        assert service.prepare_test_data('seed_tasks', {'tasks': [{'title': '阅读示例'}]})['status'] == 'success'
        assert server.get('/tasks').json()['tasks'] == [{'title': '阅读示例', 'completed': False}]

def test_preparation_logs_redact_parameters():
    import tools
    assert 'DO_NOT_LOG' not in json.dumps(tools._digest_args({'parameters': {'tasks': ['DO_NOT_LOG']}}, 'prepare_test_data'))

def test_optional_module_absence_keeps_core_importable(tmp_path):
    import subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = '''
import sys
class BlockPreparation:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "preparation" or fullname.startswith("preparation."):
            raise ModuleNotFoundError("optional module unavailable", name=fullname)
sys.meta_path.insert(0, BlockPreparation())
import tools
import app
assert "mcp__device__execute_ui_actions" in tools.device_server()[1]
assert "mcp__device__prepare_test_data" not in tools.device_server()[1]
assert app.SETTINGS_PLUGINS == []
import tencent_sheets
'''
    import os
    env = dict(os.environ, MINIAPP_PILOT_DATA_DIR=str(tmp_path), PYTHONPATH=os.pathsep.join(str(root/p) for p in ('device-mcp', 'device-mcp/agent', 'device-mcp/agent/web')))
    subprocess.run([sys.executable, '-c', code], cwd=root, env=env, check=True, capture_output=True)
