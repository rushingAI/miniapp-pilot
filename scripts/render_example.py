"""Print a read-only Markdown view of the canonical example workbook."""
from html import escape
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parents[1]


def cell(value):
    if value is None:
        return '（空白）'
    return escape(str(value), quote=False).replace('|', '&#124;').replace('\n', '<br>')


def render():
    workbook = openpyxl.load_workbook(ROOT/'examples/basic-interactions.xlsx', read_only=True)
    lines = [
        '# 基础交互用例', '',
        '[下载 Excel](basic-interactions.xlsx) · [测试记录](../docs/validation.md)', '',
        '以下内容由 Excel 生成。“（空白）”表示空单元格，待验证标记为执行前状态。修改方法见 [示例指南](README.md#维护用例)。', '',
    ]
    try:
        for sheet in workbook:
            lines += [f'## {sheet.title}', '']
            rows = list(sheet.values)
            if sheet.title == '基础交互':
                headers = rows[0]
                for row in rows[1:]:
                    lines += [f'### {cell(row[0])}', '', '| 字段 | 内容 |', '| --- | --- |']
                    lines += [f'| {cell(key)} | {cell(value)} |' for key, value in zip(headers, row)]
                    lines += ['']
            else:
                lines += ['| ' + ' | '.join(cell(v) for v in rows[0]) + ' |',
                          '| ' + ' | '.join('---' for _ in rows[0]) + ' |']
                lines += ['| ' + ' | '.join(cell(v) for v in row) + ' |' for row in rows[1:]]
                lines += ['']
    finally:
        workbook.close()
    return '\n'.join(lines)


if __name__ == '__main__':
    print(render(), end='')
