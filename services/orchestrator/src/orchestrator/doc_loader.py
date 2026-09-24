"""需求文档加载：docx / md / xlsx 统一文本化（F-REQ-01~03）。"""

from __future__ import annotations

from pathlib import Path

from common.errors import X_ORCH_01, PipelineError

_SUPPORTED_SUFFIXES = (".md", ".txt", ".docx", ".xlsx")


def load_document_text(path: Path) -> str:
    """按扩展名加载文档并文本化（保持行序，供 LLM 解析与溯源）。

    Raises:
        PipelineError: X-ORCH-01——文件不存在或格式不支持。
    """
    if not path.is_file():
        raise PipelineError(
            code=X_ORCH_01,
            message=f"需求文档不存在：{path}",
        )
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8")
        # 某些 Windows/脚本上传会把换行序列作为字面量 ``\\n`` 写入文件。
        # 仅在文件完全没有真实换行时恢复它，避免改写正常正文中的反斜杠。
        if "\\n" in text and "\n" not in text:
            text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        return text
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".xlsx":
        return _load_xlsx(path)
    raise PipelineError(
        code=X_ORCH_01,
        message=f"不支持的需求文档格式：{suffix}（支持 {'/'.join(_SUPPORTED_SUFFIXES)}）",
    )


def _load_docx(path: Path) -> str:
    """docx 段落文本（表格按行拼接为制表符分隔文本，保持阅读顺序）。"""
    from docx import Document

    document = Document(str(path))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        parts.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append("\t".join(cells))
    return "\n".join(parts)


def _load_xlsx(path: Path) -> str:
    """xlsx 全部工作表文本化（sheet 名作节标题，行按制表符拼接）。"""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"## {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell) for cell in row]
            if any(cell.strip() for cell in cells):
                parts.append("\t".join(cells))
    workbook.close()
    return "\n".join(parts)
