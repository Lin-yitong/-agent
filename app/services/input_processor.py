import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import UploadFile


ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
DOCUMENT_EXTENSIONS = {".pdf", ".xls", ".xlsx", ".doc", ".docx", ".wps"}
ALLOWED_EXTENSIONS = ARCHIVE_EXTENSIONS | DOCUMENT_EXTENSIONS
ZIP_UTF8_FLAG = 0x800


class InputProcessingError(Exception):
    pass


class UnsafeArchivePathError(InputProcessingError):
    pass


class BidNotFoundError(InputProcessingError):
    pass


@dataclass
class ProcessingContext:
    max_depth: int
    max_files: int
    warnings: list[str] = field(default_factory=list)
    seen_files: int = 0

    def count_file(self) -> None:
        self.seen_files += 1
        if self.seen_files > self.max_files:
            raise InputProcessingError(f"文件数量超过限制：最多允许 {self.max_files} 个文件")


class InputProcessor:
    def __init__(self, storage_root: Path, max_depth: int = 5, max_files: int = 3000) -> None:
        self.storage_root = storage_root
        self.max_depth = max_depth
        self.max_files = max_files

    async def process_upload(self, upload: UploadFile) -> dict[str, Any]:
        original_name = Path(upload.filename or "").name
        if not original_name:
            raise InputProcessingError("上传文件名不能为空")

        extension = original_name.lower()
        extension = Path(extension).suffix
        if extension not in ALLOWED_EXTENSIONS:
            raise InputProcessingError(f"不支持的文件格式：{extension or '无扩展名'}")

        bid_id = str(uuid.uuid4())
        bid_root = self.storage_root / bid_id
        original_dir = bid_root / "original"
        extracted_dir = bid_root / "extracted"
        original_dir.mkdir(parents=True, exist_ok=True)
        extracted_dir.mkdir(parents=True, exist_ok=True)

        original_path = unique_path(original_dir / original_name)
        with original_path.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                destination.write(chunk)

        context = ProcessingContext(max_depth=self.max_depth, max_files=self.max_files)
        if extension in ARCHIVE_EXTENSIONS:
            self._extract_archive(original_path, extracted_dir, depth=0, context=context)
        else:
            target = unique_path(extracted_dir / original_name)
            shutil.copy2(original_path, target)
            context.count_file()

        return {
            "bid_id": bid_id,
            "status": "completed",
            "tree_url": f"/api/interpretation/v1/bids/{bid_id}/tree",
            "files_url": f"/api/interpretation/v1/bids/{bid_id}/files",
            "warnings": context.warnings,
        }

    def build_tree(self, bid_id: str) -> dict[str, Any]:
        extracted_dir = self._get_extracted_dir(bid_id)
        return self._build_tree_node(bid_id, extracted_dir, extracted_dir)

    def list_files(self, bid_id: str) -> list[dict[str, Any]]:
        extracted_dir = self._get_extracted_dir(bid_id)
        files: list[dict[str, Any]] = []
        for path in sorted(extracted_dir.rglob("*"), key=lambda item: item.relative_to(extracted_dir).as_posix()):
            relative_path = path.relative_to(extracted_dir).as_posix()
            if path.is_file() and not should_hide_extracted_path(path, extracted_dir, relative_path):
                files.append(self._file_payload(bid_id, extracted_dir, path, include_absolute=True))
        return files

    def resolve_extracted_file(self, bid_id: str, relative_path: str) -> Path:
        extracted_dir = self._get_extracted_dir(bid_id)
        target = (extracted_dir / relative_path).resolve()
        ensure_within_directory(extracted_dir, target)
        if not target.is_file() or should_hide_extracted_path(target, extracted_dir, relative_path):
            raise BidNotFoundError("文件不存在")
        return target

    def _get_extracted_dir(self, bid_id: str) -> Path:
        extracted_dir = (self.storage_root / bid_id / "extracted").resolve()
        if not extracted_dir.exists() or not extracted_dir.is_dir():
            raise BidNotFoundError("bid_id 不存在")
        return extracted_dir

    def _extract_archive(
        self,
        archive_path: Path,
        target_dir: Path,
        depth: int,
        context: ProcessingContext,
    ) -> None:
        if depth >= context.max_depth:
            raise InputProcessingError(f"压缩包嵌套层级超过限制：最多允许 {context.max_depth} 层")

        extension = archive_path.suffix.lower()
        if extension == ".zip":
            self._extract_zip(archive_path, target_dir, context)
        elif extension == ".rar":
            self._extract_rar(archive_path, target_dir, context)
        elif extension == ".7z":
            self._extract_7z(archive_path, target_dir, context)
        else:
            raise InputProcessingError(f"不支持的压缩包格式：{extension}")

        for nested_archive in sorted(target_dir.rglob("*"), key=lambda item: item.as_posix()):
            if nested_archive.is_file() and nested_archive.suffix.lower() in ARCHIVE_EXTENSIONS:
                nested_target = unique_path(nested_archive.with_suffix(""))
                nested_target.mkdir(parents=True, exist_ok=True)
                self._extract_archive(nested_archive, nested_target, depth + 1, context)

    def _extract_zip(self, archive_path: Path, target_dir: Path, context: ProcessingContext) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    member_name = decode_zip_member_name(info)
                    if should_skip_archive_member(member_name):
                        continue

                    member_path = safe_member_path(target_dir, member_name)
                    if info.is_dir():
                        member_path.mkdir(parents=True, exist_ok=True)
                        continue

                    context.count_file()
                    target_path = unique_path(member_path)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target_path.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        except zipfile.BadZipFile as exc:
            raise InputProcessingError(f"ZIP 文件损坏或无法读取：{archive_path.name}") from exc

    def _extract_rar(self, archive_path: Path, target_dir: Path, context: ProcessingContext) -> None:
        tool = find_rar_tool()
        if not tool:
            raise InputProcessingError("RAR 解压需要安装 unar、unrar 或 7z/7zz；当前环境缺少可用工具")

        with tempfile.TemporaryDirectory(dir=target_dir) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            run_rar_tool(tool, archive_path, temp_dir)
            move_extracted_files(temp_dir, target_dir, context)

    def _extract_7z(self, archive_path: Path, target_dir: Path, context: ProcessingContext) -> None:
        try:
            import py7zr
        except ImportError as exc:
            raise InputProcessingError("7Z 支持需要安装 Python 包 py7zr") from exc

        try:
            with py7zr.SevenZipFile(archive_path) as archive:
                names = archive.getnames()
                targets = []
                for name in names:
                    if should_skip_archive_member(name):
                        continue

                    safe_member_path(target_dir, name)
                    if not name.endswith("/"):
                        context.count_file()
                    targets.append(name)
                archive.extract(path=target_dir, targets=targets)
        except py7zr.Bad7zFile as exc:
            raise InputProcessingError(f"7Z 文件损坏或无法读取：{archive_path.name}") from exc

    def _build_tree_node(self, bid_id: str, root: Path, path: Path) -> dict[str, Any]:
        if path.is_file():
            return self._file_payload(bid_id, root, path, include_absolute=False)

        children = [
            self._build_tree_node(bid_id, root, child)
            for child in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            if not should_hide_extracted_path(child, root, child.relative_to(root).as_posix())
        ]
        return {
            "name": path.name,
            "type": "directory",
            "children": children,
        }

    def _file_payload(self, bid_id: str, root: Path, path: Path, include_absolute: bool) -> dict[str, Any]:
        relative_path = path.relative_to(root).as_posix()
        payload: dict[str, Any] = {
            "name": path.name,
            "type": "file",
            "relative_path": relative_path,
            "size": path.stat().st_size,
            "extension": path.suffix.lower(),
            "download_url": (
                f"/api/interpretation/v1/bids/{bid_id}/files/"
                f"{quote(relative_path, safe='/')}/download"
            ),
        }
        if include_absolute:
            payload["absolute_path"] = path.as_posix()
        return payload


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}__{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def safe_member_path(base_dir: Path, member_name: str) -> Path:
    if not member_name or member_name.startswith(("/", "\\")):
        raise UnsafeArchivePathError(f"压缩包包含不安全路径：{member_name}")

    normalized = member_name.replace("\\", "/")
    if any(part == ".." for part in Path(normalized).parts):
        raise UnsafeArchivePathError(f"压缩包包含不安全路径：{member_name}")

    target = (base_dir / normalized).resolve()
    ensure_within_directory(base_dir, target)
    return target


def decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & ZIP_UTF8_FLAG:
        return info.filename

    try:
        raw_name = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename

    for encoding in ("gb18030", "gbk", "utf-8"):
        try:
            return raw_name.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def should_skip_archive_member(member_name: str) -> bool:
    parts = [part for part in member_name.replace("\\", "/").split("/") if part]
    if not parts:
        return True

    return (
        parts[0] == "__MACOSX"
        or parts[-1] == ".DS_Store"
        or parts[-1].startswith("._")
        or parts[-1].startswith("~$")
    )


def should_hide_extracted_path(path: Path, root: Path, relative_path: str) -> bool:
    if should_skip_archive_member(relative_path):
        return True

    if path.is_file() and path.suffix.lower() in ARCHIVE_EXTENSIONS:
        expanded_dir = root / Path(relative_path).with_suffix("")
        return expanded_dir.is_dir()

    return False


def find_rar_tool() -> str | None:
    for tool in ("unar", "unrar", "7zz", "7z", "7za"):
        if shutil.which(tool):
            return tool
    return None


def run_rar_tool(tool: str, archive_path: Path, target_dir: Path) -> None:
    if tool == "unar":
        command = [
            tool,
            "-quiet",
            "-force-skip",
            "-output-directory",
            target_dir.as_posix(),
            archive_path.as_posix(),
        ]
    elif tool == "unrar":
        command = [tool, "x", "-idq", "-o-", archive_path.as_posix(), target_dir.as_posix()]
    else:
        command = [tool, "x", "-y", f"-o{target_dir.as_posix()}", archive_path.as_posix()]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"RAR 文件解压失败：{archive_path.name}"
        if detail:
            message = f"{message}：{detail[:300]}"
        raise InputProcessingError(message) from exc


def move_extracted_files(source_dir: Path, target_dir: Path, context: ProcessingContext) -> None:
    for path in sorted(source_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue

        relative_path = path.relative_to(source_dir).as_posix()
        if should_skip_archive_member(relative_path):
            continue

        safe_member_path(target_dir, relative_path)
        context.count_file()
        target_path = unique_path(target_dir / relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path.as_posix(), target_path.as_posix())


def ensure_within_directory(base_dir: Path, target: Path) -> None:
    base = base_dir.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise UnsafeArchivePathError(f"路径越界：{target}") from exc
