import asyncio
import io
import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from tencent_sheets import TencentDocsError
import tencent_sheets


def _workbook_bytes():
    wb = openpyxl.Workbook()
    wb.active.title = "用例场景"
    wb.active.append(["序号", "操作", "观测点"])
    wb.active.append([1, "打开页面", "页面显示"])
    wb.create_sheet("步骤脚本")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_tencent_sheet_becomes_regular_pending_upload(monkeypatch, tmp_path):
    async def fake_fetch(url, token):
        assert token == "temporary-secret"
        return _workbook_bytes(), {
            "file_id": "DEMO00000001",
            "url": "https://docs.qq.com/sheet/DEMO00000001?tab=000002",
        }

    monkeypatch.setattr(A, "UPLOADS", str(tmp_path))
    monkeypatch.setattr(tencent_sheets, "fetch_workbook_bytes", fake_fetch)

    result = asyncio.run(A.upload_tencent_sheet({
        "url": "https://docs.qq.com/sheet/DEMO00000001?tab=000002",
        "token": "temporary-secret",
    }))

    assert result["filename"] == "腾讯用例_DEMO00000001.xlsx"
    assert result["sheets"] == ["用例场景", "步骤脚本"]
    assert result["source"]["type"] == "tencent_docs"
    assert "token" not in result["source"]
    assert (tmp_path / result["filename"]).is_file()
    assert b"temporary-secret" not in (tmp_path / result["filename"]).read_bytes()


def test_tencent_sheet_failure_does_not_create_upload(monkeypatch, tmp_path):
    async def fake_fetch(url, token):
        raise TencentDocsError("读取失败")

    monkeypatch.setattr(A, "UPLOADS", str(tmp_path))
    monkeypatch.setattr(tencent_sheets, "fetch_workbook_bytes", fake_fetch)

    response = asyncio.run(A.upload_tencent_sheet({"url": "x", "token": "secret"}))

    assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []
