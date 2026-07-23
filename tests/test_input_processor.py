import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import py7zr
from docx import Document
from openpyxl import Workbook

import app.services.chunk_processor as chunk_processor_module
from app.main import app, chunk_processor, processor
from app.services.chunk_processor import (
    ChunkProcessingError,
    TextLine,
    TocChapter,
    extract_doc_lines_with_aspose,
    extract_wps_lines,
    split_by_chapter_pages,
    split_document_sections,
)
from app.services.input_processor import decode_zip_member_name


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    processor.storage_root = tmp_path / "uploads"
    chunk_processor.storage_root = tmp_path / "chunks"
    return TestClient(app)


def test_upload_single_pdf_returns_bid_and_file_list(client: TestClient) -> None:
    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("招标文件.pdf", b"fake pdf", "application/pdf")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["bid_id"]
    assert payload["status"] == "completed"

    files_response = client.get(payload["files_url"])
    assert files_response.status_code == 200
    files = files_response.json()["files"]
    assert len(files) == 1
    assert files[0]["name"] == "招标文件.pdf"
    assert files[0]["extension"] == ".pdf"


def test_upload_zip_preserves_tree(client: TestClient) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("docs/招标文件.docx", b"docx")
        zip_file.writestr("报价.xlsx", b"xlsx")
    archive.seek(0)

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("bid.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    tree = client.get(response.json()["tree_url"]).json()["root"]
    assert tree["name"] == "extracted"
    assert [child["name"] for child in tree["children"]] == ["docs", "报价.xlsx"]


def test_upload_zip_keeps_wps_file(client: TestClient) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("附件/招标公告附件三.wps", b"wps")
    archive.seek(0)

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("bid.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    files = client.get(response.json()["files_url"]).json()["files"]
    assert [file["relative_path"] for file in files] == ["附件/招标公告附件三.wps"]
    assert files[0]["extension"] == ".wps"

    tree = client.get(response.json()["tree_url"]).json()["root"]
    attachment_dir = next(child for child in tree["children"] if child["name"] == "附件")
    assert [child["name"] for child in attachment_dir["children"]] == ["招标公告附件三.wps"]


def test_decodes_gbk_zip_member_name() -> None:
    info = zipfile.ZipInfo("招标文件.pdf".encode("gbk").decode("cp437"))
    info.flag_bits = 0

    assert decode_zip_member_name(info) == "招标文件.pdf"


def test_keeps_existing_unicode_zip_member_name() -> None:
    info = zipfile.ZipInfo("资料/招标文件.pdf")
    info.flag_bits = 0

    assert decode_zip_member_name(info) == "资料/招标文件.pdf"


def test_upload_zip_skips_macos_metadata(client: TestClient) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("__MACOSX/._招标文件.pdf", b"metadata")
        zip_file.writestr(".DS_Store", b"metadata")
        zip_file.writestr("招标文件.pdf", b"pdf")
    archive.seek(0)

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("bid.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    files = client.get(response.json()["files_url"]).json()["files"]
    assert [file["relative_path"] for file in files] == ["招标文件.pdf"]


def test_upload_7z_skips_office_temp_files(client: TestClient, tmp_path: Path) -> None:
    archive_path = tmp_path / "bid.7z"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "202403181810341242286402020.doc").write_bytes(b"doc")
    (source_dir / "~$2403181810341242286402020.doc").write_bytes(b"lock")
    (source_dir / "广西通信信息安全监控指挥中心维修及配套机房改造.pdf").write_bytes(b"pdf")

    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writeall(source_dir, arcname="20240408-项目")

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("bid.7z", archive_path.read_bytes(), "application/x-7z-compressed")},
    )

    assert response.status_code == 200
    files = client.get(response.json()["files_url"]).json()["files"]
    relative_paths = {file["relative_path"] for file in files}
    assert "20240408-项目/202403181810341242286402020.doc" in relative_paths
    assert "20240408-项目/广西通信信息安全监控指挥中心维修及配套机房改造.pdf" in relative_paths
    assert all("~$" not in path for path in relative_paths)


def test_upload_nested_zip_extracts_nested_contents(client: TestClient) -> None:
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as zip_file:
        zip_file.writestr("inner.pdf", b"pdf")

    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zip_file:
        zip_file.writestr("packs/nested.zip", nested.getvalue())
    outer.seek(0)

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("outer.zip", outer.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    files = client.get(response.json()["files_url"]).json()["files"]
    relative_paths = {file["relative_path"] for file in files}
    assert "packs/nested.zip" not in relative_paths
    assert "packs/nested/inner.pdf" in relative_paths

    tree = client.get(response.json()["tree_url"]).json()["root"]
    packs = next(child for child in tree["children"] if child["name"] == "packs")
    assert [child["name"] for child in packs["children"]] == ["nested"]


def test_rejects_unsupported_extension(client: TestClient) -> None:
    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("run.exe", b"binary", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "不支持" in response.json()["detail"]


def test_rejects_zip_path_traversal(client: TestClient) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("../escape.pdf", b"bad")
    archive.seek(0)

    response = client.post(
        "/api/interpretation/v1/upload",
        files={"file": ("bad.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "不安全路径" in response.json()["detail"]


def test_chunk_docx_by_level_1_headings(client: TestClient, tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("第一章 招标公告")
    document.add_paragraph("公告正文")
    document.add_paragraph("第二章 投标人须知")
    document.add_paragraph("须知正文")
    file_path = tmp_path / "招标文件.docx"
    document.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "招标文件.docx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chunk_strategy"] == "chapter_heading"
    assert payload["chunk_job_id"]
    assert Path(payload["chunks_file"]).exists()
    assert Path(payload["chunks_dir"]).is_dir()
    assert "未识别到目录章结构" in payload["warnings"][0]
    assert [chunk["title"] for chunk in payload["chunks"]] == ["第一章 招标公告", "第二章 投标人须知"]
    assert "公告正文" in payload["chunks"][0]["text"]
    assert "content_blocks" not in payload["chunks"][0]
    chunk_files = sorted(Path(payload["chunks_dir"]).glob("*.md"))
    assert len(chunk_files) == 2
    assert "第一章 招标公告" in Path(payload["chunks_file"]).read_text(encoding="utf-8")
    assert "公告正文" in chunk_files[0].read_text(encoding="utf-8")
    assert "<table" not in chunk_files[0].read_text(encoding="utf-8")


def test_chunk_prefers_chapter_headings_over_numbered_sections(client: TestClient, tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("2025年四川联通成都枢纽北楼核心局房老旧低压配电系统隐患整治工程项目")
    document.add_paragraph("招 标 文 件")
    document.add_paragraph("招标编号：03-08-04A-2025-D-F-C23077")
    document.add_paragraph("招标人：中国联合网络通信有限公司成都市分公司（盖单位公章）")
    document.add_paragraph("2025年09月")
    document.add_paragraph("第一章  招标公告")
    document.add_paragraph("本招标项目为2025年四川联通成都枢纽北楼核心局房老旧低压配电系统隐患整治工程项目")
    document.add_paragraph("1．项目概况与招标内容")
    document.add_paragraph("1.1 项目概况")
    document.add_paragraph("第二章  投标人须知")
    document.add_paragraph("投标人须知前附表")
    document.add_paragraph("1．总则")
    document.add_paragraph("1.1 项目概况")
    document.add_paragraph("2.1.1 招标文件一般由以下部分组成：")
    document.add_paragraph("第一章  招标公告/投标邀请书")
    document.add_paragraph("第二章  投标人须知")
    document.add_paragraph("第三章  评标办法")
    document.add_paragraph("第四章  合同条款")
    document.add_paragraph("第五章  技术标准和要求")
    file_path = tmp_path / "四川联通.docx"
    document.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "四川联通.docx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chunk_strategy"] == "chapter_heading"
    assert [chunk["title"] for chunk in payload["chunks"]] == ["封面", "第一章 招标公告", "第二章 投标人须知"]
    assert "招 标 文 件" in payload["chunks"][0]["text"]
    assert "1．项目概况与招标内容" in payload["chunks"][1]["text"]
    assert "1．总则" in payload["chunks"][2]["text"]
    assert "第三章  评标办法" in payload["chunks"][2]["text"]


def test_chunk_docx_by_toc_chapters(client: TestClient, tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("项目招标文件")
    document.add_paragraph("采购人：测试单位")
    document.add_paragraph("目录")
    document.add_paragraph("第一章 投标邀请")
    document.add_paragraph("第二章 供应商须知")
    document.add_paragraph("投标注意事项")
    document.add_paragraph("注意事项正文")
    document.add_paragraph("第一章 投标邀请(招标公告)")
    document.add_paragraph("一、项目基本情况")
    document.add_paragraph("1.项目名称：测试项目")
    document.add_paragraph("第二章 供应商须知")
    document.add_paragraph("1.说明：这里不应该成为一级切片")
    file_path = tmp_path / "目录招标文件.docx"
    document.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "目录招标文件.docx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chunk_strategy"] == "toc_chapter"
    assert payload["warnings"] == []
    assert [chunk["title"] for chunk in payload["chunks"]] == [
        "封面",
        "投标注意事项",
        "第一章 投标邀请",
        "第二章 供应商须知",
    ]
    assert "项目招标文件" in payload["chunks"][0]["text"]
    assert "注意事项正文" in payload["chunks"][1]["text"]
    assert "一、项目基本情况" in payload["chunks"][2]["text"]
    assert "1.项目名称：测试项目" in payload["chunks"][2]["text"]
    assert "1.说明：这里不应该成为一级切片" in payload["chunks"][3]["text"]
    assert len(list(Path(payload["chunks_dir"]).glob("*.md"))) == 4


def test_toc_chapters_handle_starred_title_and_skip_embedded_chapter_list() -> None:
    lines = [
        TextLine("项目招标文件"),
        TextLine("目    录"),
        TextLine("第一章 招标公告\t1"),
        TextLine("第二章 投标人须知\t8"),
        TextLine("第三章 评标办法（综合评估法）\t31"),
        TextLine("第四章 合同条款\t41"),
        TextLine("★第五章 货物需求一览表及技术规格\t85"),
        TextLine("第六章 投标文件格式\t109"),
        TextLine("第一章 招标公告"),
        TextLine("第一章正文"),
        TextLine("第二章 投标人须知"),
        TextLine("招标文件组成"),
        TextLine("第一章 招标公告"),
        TextLine("第二章 投标人须知"),
        TextLine("第三章 评标办法"),
        TextLine("第四章 合同条款"),
        TextLine("第五章 技术标准和要求"),
        TextLine("第六章 投标文件格式"),
        TextLine("第二章正文"),
        TextLine("第二章正文补充1"),
        TextLine("第二章正文补充2"),
        TextLine("第二章正文补充3"),
        TextLine("第三章 评标办法（综合评估法）"),
        TextLine("第三章正文"),
        TextLine("第三章正文补充1"),
        TextLine("第三章正文补充2"),
        TextLine("第三章正文补充3"),
        TextLine("第四章 合同条款"),
        TextLine("第四章正文"),
        TextLine("第四章正文补充1"),
        TextLine("第四章正文补充2"),
        TextLine("第四章正文补充3"),
        TextLine("★第五章 货物需求一览表及技术规格"),
        TextLine("第五章正文"),
        TextLine("第五章正文补充1"),
        TextLine("第五章正文补充2"),
        TextLine("第五章正文补充3"),
        TextLine("第六章 投标文件格式"),
        TextLine("第六章正文"),
    ]

    sections, warnings, strategy = split_document_sections(lines, "四川联通.doc")

    assert strategy == "toc_chapter"
    assert warnings == []
    assert [section.title for section in sections] == [
        "封面",
        "第一章 招标公告",
        "第二章 投标人须知",
        "第三章 评标办法（综合评估法）",
        "第四章 合同条款",
        "第五章 货物需求一览表及技术规格",
        "第六章 投标文件格式",
    ]
    assert "第三章 评标办法\n第四章 合同条款" in "\n".join(line.text for line in sections[2].lines)
    assert sections[3].lines[0].text == "第三章 评标办法（综合评估法）"
    assert "第三章正文" in "\n".join(line.text for line in sections[3].lines)
    assert sections[5].lines[0].text == "★第五章 货物需求一览表及技术规格"
    assert "第五章正文" in "\n".join(line.text for line in sections[5].lines)


def test_chunk_docx_preserves_table_order(client: TestClient, tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("第一章 招标公告")
    document.add_paragraph("表格前正文")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "条款号"
    table.cell(0, 1).text = "条款名称"
    table.cell(1, 0).text = "1.1.2"
    table.cell(1, 1).text = "招标人"
    document.add_paragraph("表格后正文")
    document.add_paragraph("第二章 投标人须知")
    document.add_paragraph("第二章正文")
    file_path = tmp_path / "表格顺序.docx"
    document.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "表格顺序.docx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    first_text = payload["chunks"][0]["text"]
    assert first_text.index("表格前正文") < first_text.index("条款号\t条款名称")
    assert first_text.index("1.1.2\t招标人") < first_text.index("表格后正文")
    assert "条款号\t条款名称" not in payload["chunks"][1]["text"]


def test_chunk_doc_falls_back_to_aspose_when_soffice_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_convert(file_path: Path, temp_dir: Path) -> tuple[Path | None, str | None]:
        return None, "模拟 LibreOffice 转换失败"

    def fake_aspose(file_path: Path, temp_dir: Path) -> tuple[list[TextLine], list[str]]:
        return (
            [
                TextLine("第一章 招标公告"),
                TextLine("公告正文"),
                TextLine("第二章 投标人须知"),
                TextLine("须知正文"),
            ],
            [],
        )

    monkeypatch.setattr(chunk_processor_module, "try_convert_doc_to_docx_with_soffice", fake_convert)
    monkeypatch.setattr(chunk_processor_module, "extract_doc_lines_with_aspose", fake_aspose)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={"file": ("老格式.doc", b"legacy doc", "application/msword")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "LibreOffice 转换失败，已使用 DOC 兜底解析" in payload["warnings"]
    assert [chunk["title"] for chunk in payload["chunks"]] == ["第一章 招标公告", "第二章 投标人须知"]
    assert "公告正文" in payload["chunks"][0]["text"]


def test_chunk_wps_converts_to_docx(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = Document()
    document.add_paragraph("第一章 招标公告")
    document.add_paragraph("公告正文")
    converted_path = tmp_path / "source.docx"
    document.save(converted_path)

    def fake_convert(file_path: Path, temp_dir: Path, source_extension: str) -> tuple[Path | None, str | None]:
        assert source_extension == ".wps"
        return converted_path, None

    monkeypatch.setattr(chunk_processor_module, "try_convert_to_docx_with_soffice", fake_convert)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={"file": ("招标公告附件三.wps", b"wps", "application/octet-stream")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["extension"] == ".wps"
    assert payload["chunk_strategy"] == "chapter_heading"
    assert [chunk["title"] for chunk in payload["chunks"]] == ["第一章 招标公告"]
    assert "公告正文" in payload["chunks"][0]["text"]


def test_chunk_wps_returns_clear_error_when_conversion_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_convert(file_path: Path, temp_dir: Path, source_extension: str) -> tuple[Path | None, str | None]:
        assert source_extension == ".wps"
        return None, "WPS 文件转换失败：模拟错误"

    monkeypatch.setattr(chunk_processor_module, "try_convert_to_docx_with_soffice", fake_convert)

    with pytest.raises(ChunkProcessingError, match="WPS 文件转换失败"):
        extract_wps_lines(tmp_path / "bad.wps", tmp_path)


def test_aspose_truncated_output_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeDocument:
        def __init__(self, path: str) -> None:
            self.path = path

        def save(self, output_path: str, save_format: object) -> None:
            Path(output_path).write_text(
                "第一章 招标公告\n"
                "This document was truncated here because it was created in the Evaluation Mode.",
                encoding="utf-8",
            )

    class FakeSaveFormat:
        TEXT = object()

    class FakeAsposeWords:
        Document = FakeDocument
        SaveFormat = FakeSaveFormat

    monkeypatch.setitem(__import__("sys").modules, "aspose", type("FakeAspose", (), {"words": FakeAsposeWords})())
    monkeypatch.setitem(__import__("sys").modules, "aspose.words", FakeAsposeWords)

    with pytest.raises(ChunkProcessingError, match="评估版输出被截断"):
        extract_doc_lines_with_aspose(tmp_path / "old.doc", tmp_path)


def test_split_by_chapter_pages_uses_outline_boundaries() -> None:
    lines = [
        TextLine("封面标题", page=1),
        TextLine("目录", page=2),
        TextLine("第一章 标题", page=2),
        TextLine("第一章 正文", page=3),
        TextLine("第六章 正文标题没有被抽取出来", page=35),
        TextLine("第六章 正文内容", page=36),
        TextLine("第七章 政府采购合同主要条款", page=47),
    ]
    chapters = [
        TocChapter("第一章 投标邀请", 1, 3),
        TocChapter("第六章 评审方法和标准", 6, 35),
        TocChapter("第七章 政府采购合同主要条款", 7, 47),
    ]

    sections = split_by_chapter_pages(lines, chapters)

    assert [section.title for section in sections] == [
        "封面",
        "第一章 投标邀请",
        "第六章 评审方法和标准",
        "第七章 政府采购合同主要条款",
    ]
    assert [line.text for line in sections[0].lines] == ["封面标题"]
    assert [line.page for line in sections[2].lines] == [35, 36]


def test_chunk_docx_without_heading_returns_single_chunk(client: TestClient, tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("这是一个没有一级标题的文件")
    file_path = tmp_path / "普通文件.docx"
    document.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "普通文件.docx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["chunks"]) == 1
    assert payload["chunks"][0]["title"] == "普通文件"
    assert "未识别到目录章结构" in payload["warnings"][0]
    assert "未识别到一级标题" in payload["warnings"][1]


def test_chunk_xlsx_by_sheet(client: TestClient, tmp_path: Path) -> None:
    workbook = Workbook()
    active = workbook.active
    active.title = "报价清单"
    active.append(["项目", "价格"])
    active.append(["施工", 100])
    second = workbook.create_sheet("技术要求")
    second.append(["要求", "说明"])
    second.append(["工期", "30天"])
    file_path = tmp_path / "报价表.xlsx"
    workbook.save(file_path)

    response = client.post(
        "/api/interpretation/v1/chunk",
        files={
            "file": (
                "报价表.xlsx",
                file_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chunk_strategy"] == "sheet"
    assert [chunk["title"] for chunk in payload["chunks"]] == ["报价清单", "技术要求"]
    assert payload["chunks"][0]["source"]["sheet_name"] == "报价清单"
    assert "施工\t100" in payload["chunks"][0]["text"]


def test_chunk_rejects_unsupported_extension(client: TestClient) -> None:
    response = client.post(
        "/api/interpretation/v1/chunk",
        files={"file": ("slides.pptx", b"pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
    )

    assert response.status_code == 400
    assert "不支持" in response.json()["detail"]
