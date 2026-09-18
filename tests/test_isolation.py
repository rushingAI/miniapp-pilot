from pathlib import Path

def test_auth_ignores_ambient_provider_credentials(monkeypatch):
    from auth import claude_sdk_env
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'UNRELATED_SECRET')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://unrelated.example.org')
    monkeypatch.setenv('ANTHROPIC_CUSTOM_HEADERS', 'X-Test: UNRELATED_SECRET')
    monkeypatch.delenv('MINIAPP_PILOT_API_KEY', raising=False)
    env = claude_sdk_env()
    assert env.get('ANTHROPIC_API_KEY', '') == ''
    assert env['ANTHROPIC_BASE_URL'] != 'https://unrelated.example.org'
    assert env['ANTHROPIC_CUSTOM_HEADERS'] == ''
    assert 'UNRELATED_SECRET' not in str(env)

def test_provider_override_is_process_local(monkeypatch):
    from auth import claude_sdk_env
    import os
    before = dict(os.environ)
    env = claude_sdk_env(api_key='EXPLICIT_TEST_KEY', base_url='https://example.org', model='demo-model')
    assert env['ANTHROPIC_API_KEY'] == 'EXPLICIT_TEST_KEY'
    assert env['ANTHROPIC_MODEL'] == 'demo-model'
    assert dict(os.environ) == before

def test_runtime_root_uses_only_explicit_namespace(monkeypatch, tmp_path):
    from runtime_paths import data_root
    monkeypatch.setenv('MINIAPP_PILOT_DATA_DIR', str(tmp_path))
    assert data_root() == tmp_path

def test_update_routes_and_modules_are_absent():
    import app
    assert all(not getattr(r, 'path', '').startswith('/api/update') for r in app.app.routes)
    assert not (Path(app.__file__).parent / 'update_service.py').exists()

def test_telemetry_ignores_ambient_endpoint(monkeypatch):
    from telemetry import environment_fingerprint
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://unrelated.example.org')
    assert 'unrelated.example.org' not in str(environment_fingerprint({}))

def test_key_change_is_locked_while_suite_is_active(monkeypatch):
    import asyncio
    import app
    class Manager:
        busy = False
        mutation_locked = True
        active = False
    monkeypatch.setattr(app, 'INTERACTIVE_MANAGER', Manager())
    response = asyncio.run(app.set_provider_key({'api_key': 'SYNTHETIC_TEST_KEY'}))
    assert response.status_code == 409
