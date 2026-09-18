"""通用设置插件注册表。核心只发现并挂载插件，不理解插件业务配置。"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import re
from typing import Callable

from fastapi import APIRouter


LOG = logging.getLogger(__name__)
DEFAULT_SETTINGS_MODULES = ("preparation.web_settings",)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class SettingsContext:
    data_root: str
    uploads_dir: str
    is_busy: Callable[[], bool]


@dataclass(frozen=True)
class SettingsPlugin:
    plugin_id: str
    router: APIRouter
    describe: Callable[[], dict]


def discover_settings_plugins(
        context: SettingsContext,
        module_names: tuple[str, ...] = DEFAULT_SETTINGS_MODULES,
) -> list[SettingsPlugin]:
    """发现可选设置插件；缺失或损坏的插件不能阻止通用 Agent 启动。"""
    found = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
            plugin = module.create_settings_plugin(context)
            if not _SAFE_ID.fullmatch(plugin.plugin_id):
                raise ValueError(f"非法插件 ID: {plugin.plugin_id!r}")
            found.append(plugin)
        except Exception as exc:
            LOG.warning("设置插件不可用 %s: %s", module_name, exc)
    return found


def plugin_descriptors(plugins: list[SettingsPlugin]) -> list[dict]:
    return [plugin.describe() for plugin in plugins]
