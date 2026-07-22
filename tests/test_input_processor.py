import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import py7zr

from app.main import app, processor
from app.services.input_processor import decode_zip_member_name


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    processor.storage_root = tmp_path / "uploads"
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
