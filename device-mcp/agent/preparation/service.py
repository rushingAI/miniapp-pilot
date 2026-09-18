"""Validate once; execute one configured HTTP request; return a safe receipt."""
from copy import deepcopy
import json
import math
import os
import re
from urllib.parse import urlsplit
import httpx
from runtime_paths import data_root

_NAME = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
_TYPES = {'string': str, 'boolean': bool, 'object': dict, 'array': list,
          'integer': int, 'number': (int, float)}

def _valid_value(value, kind):
    if kind not in _TYPES or not isinstance(value, _TYPES[kind]):
        return False
    if kind in {'integer', 'number'} and isinstance(value, bool):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        return False
    return True

class PreparationService:
    def __init__(self, config, *, client=None):
        self.config = deepcopy(config)
        self.client = client
        if not isinstance(config, dict) or set(config) - {'actions', 'checks'}:
            raise ValueError('Expected actions and checks maps')
        for group in ('actions', 'checks'):
            entries = config.get(group, {})
            if not isinstance(entries, dict):
                raise ValueError('Expected a configuration map')
            for name, entry in entries.items():
                if not _NAME.fullmatch(name) or not isinstance(entry, dict):
                    raise ValueError('Invalid action/check definition')
                if set(entry) - {'method', 'url', 'parameters', 'success', 'timeout_s', 'authorization_env'}:
                    raise ValueError('Unknown configuration field')
                parsed = urlsplit(entry.get('url', ''))
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError('Expected an HTTP(S) URL without credentials or fragment')
                if any(ch.isspace() for ch in entry['url']) or (parsed.port is not None and not 1 <= parsed.port <= 65535):
                    raise ValueError('Invalid URL or port')
                if entry.get('method') not in ({'GET'} if group == 'checks' else {'POST', 'PUT', 'PATCH', 'DELETE'}):
                    raise ValueError('Checks must use GET; actions require an explicit write method')
                timeout = entry.get('timeout_s', 15)
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
                    raise ValueError('timeout_s must be between 0 and 60 seconds')
                success = entry.get('success')
                if not isinstance(success, dict) or set(success) != {'path', 'equals'} or not isinstance(success['path'], list) or not success['path']:
                    raise ValueError('An explicit JSON success path and expected value are required')
                if not all(type(x) in (str, int) and (not isinstance(x, int) or x >= 0) for x in success['path']):
                    raise ValueError('Invalid JSON path')
                json.dumps(success['equals'], allow_nan=False)
                params = entry.get('parameters', {})
                if not isinstance(params, dict) or (group == 'checks' and params):
                    raise ValueError('Checks accept no runtime parameters')
                for key, rule in params.items():
                    if not _NAME.fullmatch(key) or not isinstance(rule, dict) or set(rule)-{'type', 'required', 'default'} or rule.get('type') not in _TYPES:
                        raise ValueError('Invalid parameter schema')
                    if 'required' in rule and not isinstance(rule['required'], bool):
                        raise ValueError('required must be boolean')
                    if 'default' in rule and not _valid_value(rule['default'], rule['type']):
                        raise ValueError('Invalid parameter default')
                credential = entry.get('authorization_env', '')
                if credential and not re.fullmatch(r'MINIAPP_PILOT_PREP_[A-Z0-9_]+', credential):
                    raise ValueError('Use a MINIAPP_PILOT_PREP_ credential reference')

    def prepare_test_data(self, action, parameters):
        return self._execute('actions', action, parameters)

    def check_environment(self, check):
        return self._execute('checks', check, {})

    def _execute(self, group, name, parameters):
        receipt = {'status': 'failure', 'request_sent': False, 'code': 'unknown_operation'}
        entry = self.config.get(group, {}).get(name)
        if entry is None:
            return {**receipt, 'available': list(self.config.get(group, {}))}
        rules = entry.get('parameters', {})
        if not isinstance(parameters, dict) or set(parameters) - set(rules):
            return {**receipt, 'code': 'invalid_parameters'}
        body = {}
        missing = []
        for key, rule in rules.items():
            if key not in parameters and 'default' not in rule:
                if rule.get('required'):
                    missing.append(key)
                continue
            value = parameters[key] if key in parameters else rule['default']
            if not _valid_value(value, rule['type']):
                return {**receipt, 'code': 'invalid_parameters', 'field': key}
            body[key] = value
        if missing:
            return {**receipt, 'code': 'missing_parameters', 'fields': missing,
                    'types': {key: rules[key]['type'] for key in missing}}
        headers = {}
        if entry.get('authorization_env'):
            token = os.environ.get(entry['authorization_env'], '')
            if not token:
                return {**receipt, 'code': 'credential_required'}
            headers['Authorization'] = 'Bearer ' + token
        client = self.client or httpx.Client(trust_env=False, follow_redirects=False)
        try:
            response = client.request(entry['method'], entry['url'],
                                      json=body if group == 'actions' else None,
                                      headers=headers, timeout=entry.get('timeout_s', 15),
                                      follow_redirects=False)
            receipt.update(status='unknown', request_sent=True, http_status=response.status_code)
            if not 200 <= response.status_code < 300:
                return {**receipt, 'code': 'http_unconfirmed'}
            value = response.json()
            for key in entry['success']['path']:
                if isinstance(value, dict) and isinstance(key, str):
                    value = value[key]
                elif isinstance(value, list) and type(key) is int:
                    value = value[key]
                else:
                    raise KeyError(key)
            expected = entry['success']['equals']
            match = json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)
            return {**receipt, 'status': 'success' if match else 'failure', 'code': 'assertion_matched' if match else 'assertion_mismatch'}
        except httpx.HTTPError:
            return {**receipt, 'status': 'unknown', 'request_sent': True, 'code': 'transport_unconfirmed'}
        except (ValueError, KeyError, IndexError, TypeError):
            return {**receipt, 'status': 'unknown', 'code': 'response_unconfirmed'}
        finally:
            if self.client is None:
                client.close()

def config_path():
    return data_root() / 'preparation.json'

def load_service():
    path = config_path()
    return PreparationService(json.loads(path.read_text()) if path.is_file() else {})
