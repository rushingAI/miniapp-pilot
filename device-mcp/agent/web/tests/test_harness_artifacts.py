import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_session import deliverable_artifacts  # noqa: E402


def test_deliverable_artifacts_only_returns_non_hidden_root_files(tmp_path):
    output = tmp_path / "output"
    nested = output / "v1"
    nested.mkdir(parents=True)
    (output / "result.xlsx").write_bytes(b"xlsx")
    (output / "preview.png").write_bytes(b"png")
    (output / ".dir").write_text("", encoding="utf-8")
    (nested / "case-1.png").write_bytes(b"evidence")

    assert [path.name for path in deliverable_artifacts(output)] == [
        "preview.png", "result.xlsx",
    ]
