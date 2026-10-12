"""简历文件解析：Markdown / 纯文本 / Word / PDF（不需要网络、不需要真实简历）。"""

from __future__ import annotations

import io
import zipfile

from jobpilot.resume_io import extract_text, read_resume_file

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def make_docx(body: str) -> bytes:
    """最小可用的 .docx：只有 word/document.xml（解析器只读这一个部件）。"""
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W} {MC}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


def para(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>" for text in runs) + "</w:p>"


def make_pdf(lines: list[str]) -> bytes:
    """手写一个带文字层的单页 PDF（Helvetica，只放 ASCII，避免依赖中文字体）。"""
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


# ---------------- 纯文本 ----------------
def test_markdown_utf8_with_bom():
    text, problem = extract_text("\ufeff# 张三\r\n\r\n6 年数据分析".encode(), "cv.md")
    assert problem == "" and text == "# 张三\n\n6 年数据分析"


def test_txt_in_gbk_is_decoded():
    text, problem = extract_text("数据分析师，熟悉 SQL".encode("gbk"), "cv.txt")
    assert problem == "" and text == "数据分析师，熟悉 SQL"


def test_empty_file_is_reported():
    assert extract_text(b"  \n ", "cv.md") == ("", "简历文件是空的")


# ---------------- Word ----------------
def test_docx_paragraphs_tables_and_textboxes():
    body = (
        para("张三", " · 数据分析师")
        + "<w:tbl><w:tr><w:tc>" + para("技能") + "</w:tc><w:tc>" + para("SQL") + para("Python") + "</w:tc></w:tr></w:tbl>"
        # 文本框：Word 存两份（Choice 新格式 + Fallback 旧格式），只能取一份
        + "<w:p><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><w:txbxContent>"
        + para("2019-2024 某公司") + "</w:txbxContent></w:drawing></mc:Choice>"
        + "<mc:Fallback><w:pict><w:txbxContent>" + para("2019-2024 某公司") + "</w:txbxContent></w:pict></mc:Fallback>"
        + "</mc:AlternateContent></w:r></w:p>"
    )
    text, problem = extract_text(make_docx(body), "简历.DOCX")
    assert problem == ""
    assert text.splitlines() == ["张三 · 数据分析师", "技能 | SQL Python", "2019-2024 某公司"]


def test_docx_that_is_not_a_zip_is_reported():
    text, problem = extract_text(b"definitely not a zip", "cv.docx")
    assert text == "" and "打不开" in problem


def test_old_doc_format_gets_a_clear_hint():
    text, problem = extract_text(b"\xd0\xcf\x11\xe0", "cv.doc")
    assert text == "" and ".docx" in problem


def test_unknown_extension_lists_supported_formats():
    _, problem = extract_text(b"x", "cv.pages")
    assert "Word" in problem and "PDF" in problem


# ---------------- PDF ----------------
def test_pdf_with_text_layer():
    text, problem = extract_text(make_pdf(["Zhang San - Data Analyst", "SQL, Python, A/B testing"]), "cv.pdf")
    assert problem == ""
    assert "Data Analyst" in text and "A/B testing" in text


def test_pdf_without_text_is_treated_as_scan():
    text, problem = extract_text(make_pdf([]), "scan.pdf")
    assert text == "" and "扫描件" in problem


def test_garbage_pdf_never_raises():
    text, problem = extract_text(b"%PDF-1.4 broken", "cv.pdf")
    assert text == "" and problem


# ---------------- 磁盘读取 + 服务层统一入口 ----------------
def test_read_resume_file_caches_until_file_changes(tmp_path):
    path = tmp_path / "cv.docx"
    path.write_bytes(make_docx(para("第一版简历内容")))
    assert read_resume_file(path)[0] == "第一版简历内容"
    path.write_bytes(make_docx(para("第二版简历内容，多了一些字")))   # 大小变了 -> 缓存失效
    assert read_resume_file(path)[0] == "第二版简历内容，多了一些字"


def test_service_context_reads_docx_resume(tmp_path):
    from jobpilot.bootstrap import build_context
    from jobpilot.config import AppConfig
    from jobpilot.db import Repo, connect

    path = tmp_path / "cv.docx"
    path.write_bytes(make_docx(para("6 年数据分析经验，做过推荐系统")))
    cfg = AppConfig()
    cfg.profile.resume_path = str(path)
    ctx = build_context(cfg, repo=Repo(connect(tmp_path / "t.db")))
    assert ctx.resume_text() == ("6 年数据分析经验，做过推荐系统", "")
