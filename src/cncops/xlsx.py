"""极简 XLSX 写入器（零第三方依赖）：生成 Excel 能直接打开的 .xlsx 文件。

为什么自己写：项目运行环境要求离线可跑、依赖最小化（requirements.txt 只声明 PyYAML），
而"导出 Excel 给工厂老板"是硬需求。xlsx 本质是 zip + XML，用标准库 zipfile 即可生成，
字符串用 inlineStr 内联（免去 sharedStrings 表），样式只做"表头加粗 + 冻结首行"。

支持：
- 多工作表（每个 sheet 一个名称 + 二维数据 + 可选列宽）；
- 数字按数值写入（Excel 里可直接求和/排序），字符串按 inlineStr 写入；
- None 写为空单元格；首行加粗并冻结。

不支持（也不需要）：公式、合并单元格、图表、条件格式——导出的是数据不是报表模板。
"""

from __future__ import annotations

import zipfile
from typing import Any, Iterable, List, Optional, Sequence, Tuple

Cell = Tuple[int, int, Any]        # (row, col, value) 0-based
Sheet = Tuple[str, Sequence[Sequence[Any]], Optional[Sequence[float]]]


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_name(idx: int) -> str:
    """0-based 列号 → Excel 列名（A, B, ..., Z, AA, ...）。"""
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell_xml(row: int, col: int, value: Any, style: str = "") -> str:
    ref = f"{_col_name(col)}{row + 1}"
    s_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t>{1 if value else 0}</t></is></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s_attr}><v>{value}</v></c>'
    text = _esc(value)
    # 保留前导空格/换行需要 xml:space 属性，否则 Excel 会裁剪
    space = ' xml:space="preserve"' if str(value) != str(value).strip() or "\n" in str(value) else ""
    return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t{space}>{text}</t></is></c>'


def _sheet_xml(rows: Sequence[Sequence[Any]], widths: Optional[Sequence[float]],
               header_style: str = "1") -> str:
    body: List[str] = []
    max_col = max((len(r) for r in rows), default=0)
    for r_idx, row in enumerate(rows):
        style = header_style if r_idx == 0 else ""
        cells = "".join(_cell_xml(r_idx, c_idx, row[c_idx] if c_idx < len(row) else None, style)
                        for c_idx in range(max_col))
        body.append(f'<row r="{r_idx + 1}">{cells}</row>')
    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)) + "</cols>"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="14.4"/>'
        f'{cols}'
        '<sheetData>' + "".join(body) + '</sheetData>'
        '</worksheet>')


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '{sheet_overrides}'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>')

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
    '</Relationships>')

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def write_xlsx(sheets: Iterable[Sheet], title: str = "运维数据导出") -> bytes:
    """把若干工作表打包成 xlsx 字节流。

    sheets: [(名称, 二维数据, 可选列宽列表), ...]；第一行按表头处理（加粗 + 冻结）。
    """
    sheets = list(sheets)
    if not sheets:
        sheets = [("Sheet1", [["(无数据)"]], None)]
    safe_names: List[str] = []
    for idx, (name, _rows, _w) in enumerate(sheets, 1):
        clean = "".join(c for c in str(name) if c not in '[]:*?/\\')[:31] or f"Sheet{idx}"
        safe_names.append(clean)

    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(safe_names) + 1))

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + "".join(
            f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, n in enumerate(safe_names, 1)) + '</sheets>'
        '</workbook>')

    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(safe_names) + 1))
        + f'<Relationship Id="rId{len(safe_names) + 1}" '
          f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
          f'Target="styles.xml"/>'
        '</Relationships>')

    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{_esc(title)}</dc:title>'
        '<dc:creator>CNC 设备运维智能体</dc:creator>'
        '</cp:coreProperties>')

    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>CNC 设备运维智能体</Application>'
        '</Properties>')

    import io
    buf = io.BytesIO()
    # 固定 zip 条目时间戳：writestr(字符串, ...) 默认取当前系统时间，会导致
    # "同一份数据两次导出字节数不同"，既破坏自测的逐次运行一致性，
    # 也让工厂侧无法用校验和比对两份导出。这里显式指定 zip 纪元时间。
    fixed_dt = (1980, 1, 1, 0, 0, 0)

    def _entry(name: str, data: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=fixed_dt)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16          # 固定权限位，避免受 umask 影响
        return info

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_entry("[Content_Types].xml", _CONTENT_TYPES.format(sheet_overrides=sheet_overrides)),
                    _CONTENT_TYPES.format(sheet_overrides=sheet_overrides))
        zf.writestr(_entry("_rels/.rels", _ROOT_RELS), _ROOT_RELS)
        zf.writestr(_entry("docProps/core.xml", core), core)
        zf.writestr(_entry("docProps/app.xml", app), app)
        zf.writestr(_entry("xl/workbook.xml", workbook), workbook)
        zf.writestr(_entry("xl/_rels/workbook.xml.rels", wb_rels), wb_rels)
        zf.writestr(_entry("xl/styles.xml", _STYLES), _STYLES)
        for i, (_name, rows, widths) in enumerate(sheets, 1):
            xml = _sheet_xml(rows, widths)
            zf.writestr(_entry(f"xl/worksheets/sheet{i}.xml", xml), xml)
    return buf.getvalue()
