"""简历文件 -> 纯文本：Markdown / 纯文本 / Word(.docx) / 带文字层的 PDF。

评分、招呼语、定制简历吃的都是"正文文本"，格式只影响怎么把字取出来。
所以这里只做一件事：给一份文件，还一段文本（或者一句人话说明为什么取不出来）。
**从不抛异常** —— 坏文件、加密 PDF、扫描件都要变成用户看得懂的提示，而不是一屏 traceback。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED = (".md", ".markdown", ".txt", ".docx", ".pdf")
FORMAT_NAMES = "Markdown（.md）、纯文本（.txt）、Word（.docx）、PDF"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

# (绝对路径, mtime_ns, 文件大小) -> (文本, 问题)。PDF 解析要几百毫秒，而 /api/resume 每次打开配置页都会调
_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def format_of(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return {".markdown": "md"}.get(ext, ext.lstrip("."))


def extract_text(data: bytes, filename: str) -> tuple[str, str]:
    """返回 (正文, 问题)；能取出文字时问题为空串。"""
    ext = Path(filename or "").suffix.lower()
    if ext == ".doc":
        return "", "暂不支持旧版 .doc，请在 Word 里另存为 .docx（或导出 PDF）后再上传"
    if ext not in SUPPORTED:
        return "", f"不支持 {ext or '无扩展名'} 文件，目前支持：{FORMAT_NAMES}"
    try:
        if ext == ".docx":
            text, problem = _from_docx(data)
        elif ext == ".pdf":
            text, problem = _from_pdf(data)
        else:
            text, problem = _from_plain(data), ""
    except Exception as exc:  # noqa: BLE001 - 解析器可能抛任何东西，统一转成人话
        return "", f"文件读不出来（{type(exc).__name__}），可能已损坏；换一份试试，或把正文直接粘贴进来"
    if problem:
        return "", problem
    text = _tidy(text)
    if not text:
        return "", "简历文件是空的"
    return text, ""


def read_resume_file(path: Path) -> tuple[str, str]:
    """读磁盘上的简历文件（带缓存：文件没变就不重复解析）。"""
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        return "", f"简历读不出来（{type(exc).__name__}）"
    if key in _cache:
        return _cache[key]
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "", f"简历读不出来（{type(exc).__name__}）"
    result = extract_text(data, path.name)
    if len(_cache) > 16:   # 只是怕路径反复改来改去时无限长大；正常只有一两条
        _cache.clear()
    _cache[key] = result
    return result


# ---------------- 各格式 ----------------
def _from_plain(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):          # 记事本存的「Unicode」
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):            # 中文 .txt 很多是 GBK
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _from_docx(data: bytes) -> tuple[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "", "这个 .docx 打不开（不是有效的 Word 文件，可能已损坏，或其实是改了扩展名的 .doc）"
    with archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError:
            return "", "这个 .docx 里没有正文（缺 word/document.xml），可能已损坏"
    body = ET.fromstring(xml).find(_W + "body")
    if body is None:
        return "", "这个 .docx 里没有正文"
    return "\n".join(_block_lines(body)), ""


def _block_lines(container) -> list[str]:
    """按文档顺序把段落、表格、内容控件摊成行。表格一行一行地出，单元格用 | 隔开。"""
    lines: list[str] = []
    for child in container:
        if child.tag == _W + "p":
            lines.append(_para_text(child))
        elif child.tag == _W + "tbl":
            for row in child.findall(_W + "tr"):
                cells = [" ".join(s.strip() for s in _block_lines(cell) if s.strip())
                         for cell in row.findall(_W + "tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
        elif child.tag == _W + "sdt":
            content = child.find(_W + "sdtContent")
            if content is not None:
                lines.extend(_block_lines(content))
    return lines


def _para_text(paragraph) -> str:
    """一个段落的文字。很多中文简历模板把内容放在文本框里（段落 → 绘图 → 文本框 → 段落），
    这里会一路钻进去；Word 会把文本框存两份（新格式 + mc:Fallback 旧格式），跳过 Fallback 免得重复。"""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            tag = child.tag
            if tag == _MC_FALLBACK:
                continue
            if tag == _W + "t":
                parts.append(child.text or "")
            elif tag == _W + "tab":
                parts.append("\t")
            elif tag in (_W + "br", _W + "cr"):
                parts.append("\n")
            else:
                if tag == _W + "p" and parts and not parts[-1].endswith("\n"):
                    parts.append("\n")      # 文本框里的下一段
                walk(child)

    walk(paragraph)
    return "".join(parts)


def _from_pdf(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader  # 懒加载：只有真的上传 PDF 时才付这个导入成本

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception:  # noqa: BLE001 - 解密失败就是打不开
            opened = 0
        if not opened:
            return "", "PDF 已加密，打不开；请导出一份不加密的 PDF，或换成 Word / Markdown"
    text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    if len(text.strip()) < 20:
        return "", "这个 PDF 里没有可提取的文字（可能是扫描件或图片），请换成 Word / Markdown，或把正文直接粘贴进来"
    return text, ""


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
