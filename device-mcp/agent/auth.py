"""Explicit provider configuration for isolated SDK subprocesses."""
import os
from pathlib import Path
from dotenv import dotenv_values
from runtime_paths import data_root

ENV_FILE = Path(__file__).resolve().parents[1] / '.env'
DEFAULT_KIMI_MODEL = 'k3[1m]'
_MODEL_KEYS = ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_FABLE_MODEL',
               'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
               'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL')

def claude_sdk_env(api_key=None, base_url=None, model=None):
    # Do not modify os.environ, search parent folders, or interpolate ambient secrets.
    local = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.is_file() else {}
    def value(key, default=''):
        name = 'MINIAPP_PILOT_' + key
        return os.environ.get(name, local.get(name) or default)
    selected = str(model or value('MODEL', DEFAULT_KIMI_MODEL)).strip()
    # The SDK merges this map into the parent environment. Explicit empty values
    # prevent ambient provider headers, authentication helpers and cloud switches
    # from being inherited by its subprocess.
    env = {key: '' for key in os.environ if key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_'))}
    env.update({key: selected for key in _MODEL_KEYS})
    env.update({
        'ANTHROPIC_API_KEY': value('API_KEY') if api_key is None else api_key,
        'ANTHROPIC_AUTH_TOKEN': '',
        'CLAUDE_CODE_OAUTH_TOKEN': '',
        'CLAUDECODE': '',
        'ANTHROPIC_BASE_URL': str(base_url or value('BASE_URL', 'https://api.kimi.com/coding/')).strip(),
        'CLAUDE_CONFIG_DIR': str(data_root() / 'sdk'),
        'CLAUDE_CODE_USE_BEDROCK': '', 'CLAUDE_CODE_USE_VERTEX': '',
        'CLAUDE_CODE_USE_FOUNDRY': '',
        'CLAUDE_CODE_AUTO_COMPACT_WINDOW': value('CONTEXT_TOKENS', '1048576'),
        'CLAUDE_CODE_MAX_CONTEXT_TOKENS': value('CONTEXT_TOKENS', '1048576'),
    })
    return env
