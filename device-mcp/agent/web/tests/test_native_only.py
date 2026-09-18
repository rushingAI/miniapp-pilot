from pathlib import Path
import sys

_WEB = Path(__file__).resolve().parent.parent
_AGENT = _WEB.parent
for _path in (str(_AGENT), str(_WEB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import app
import tools


def test_web_exposes_native_harness_without_architecture_switcher():
    route_paths = {route.path for route in app.app.routes if hasattr(route, "path")}
    html = (Path(app.__file__).resolve().parent / "index.html").read_text(encoding="utf-8")

    assert "/api/architecture" not in route_paths
    assert 'id="architectureSel"' not in html
    assert "逐行 Agent" not in html
    assert "Native Harness（实验）" not in html


def test_device_tool_interface_has_no_legacy_suite_tools():
    _, allowed = tools.device_server()

    assert "mcp__device__get_screen" in allowed
    assert "mcp__device__start_test_suite" not in allowed
    assert "mcp__device__continue_test_suite" not in allowed
    assert "mcp__device__report_result" not in allowed
    assert "mcp__device__ask_human" not in allowed
