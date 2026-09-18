import asyncio
import json
from claude_agent_sdk import tool
from .service import load_service

def _result(payload):
    return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}]}

@tool('prepare_test_data', 'Run one user-configured test-data action. Parameters must match its configured types. Missing fields are returned explicitly. Unknown outcomes must not be retried automatically. This does not record a test verdict.', {
    'type': 'object', 'properties': {'action': {'type': 'string'}, 'parameters': {'type': 'object'}},
    'required': ['action', 'parameters'], 'additionalProperties': False,
})
async def prepare_test_data(args):
    try:
        result = await asyncio.to_thread(load_service().prepare_test_data, args['action'], args['parameters'])
    except (ValueError, OSError):
        result = {'status': 'failure', 'request_sent': False, 'code': 'configuration_invalid'}
    return _result(result)

@tool('check_environment', 'Run one configured read-only HTTP JSON check. Unknown means unverified, not healthy. This does not record a test verdict.', {
    'type': 'object', 'properties': {'check': {'type': 'string'}},
    'required': ['check'], 'additionalProperties': False,
})
async def check_environment(args):
    try:
        result = await asyncio.to_thread(load_service().check_environment, args['check'])
    except (ValueError, OSError):
        result = {'status': 'failure', 'request_sent': False, 'code': 'configuration_invalid'}
    return _result(result)

PREPARATION_TOOLS = [prepare_test_data, check_environment]

