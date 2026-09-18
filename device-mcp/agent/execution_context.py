"""当前单设备执行的轻量上下文，供可插拔工具落本次 run 的证据。

Runner 与工具插件都依赖这个中立模块，避免插件反向 import tools 形成循环依赖。
项目按单设备串行执行；每条 case 开始时由 tools.set_trace_dir 更新。
"""

TRACE_DIR: str | None = None


def set_trace_dir(path: str | None) -> None:
    global TRACE_DIR
    TRACE_DIR = path


def get_trace_dir() -> str | None:
    return TRACE_DIR
