import json
import os
import tempfile
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse
from plugin_settings import SettingsPlugin
from .service import PreparationService, config_path

def create_settings_plugin(context):
    router = APIRouter()

    @router.post('/import')
    async def import_config(file: UploadFile = File(...)):
        if context.is_busy():
            return JSONResponse({'error': 'End the active Suite before replacing configuration'}, status_code=409)
        raw = await file.read(1024 * 1024 + 1)
        try:
            if len(raw) > 1024 * 1024 or not (file.filename or '').endswith('.json'):
                raise ValueError()
            config = json.loads(raw)
            PreparationService(config)
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Invalid preparation JSON; existing configuration preserved'}, status_code=400)
        if context.is_busy():
            return JSONResponse({'error': 'Configuration is locked by an active Suite'}, status_code=409)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.preparation-', suffix='.json')
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(config, handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {'ok': True, 'summary': 'Preparation configuration imported'}

    return SettingsPlugin('preparation', router, lambda: {
        'id': 'preparation', 'name': '测试数据与环境检查',
        'description': '导入显式 HTTP 动作与 JSON 断言配置。',
        'summary': '已配置' if config_path().exists() else '未配置；可导入示例 JSON',
        'settings': {'type': 'file_import', 'accept': '.json',
                     'title': '测试数据与环境检查', 'input_label': '选择 JSON 配置',
                     'action_label': '导入配置',
                     'help': '配置中只使用凭证环境变量名，不填写真实密钥。'},
    })
